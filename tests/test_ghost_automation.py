"""Offline tests for the zero-touch automation primitives."""

from __future__ import annotations

import time

import pytest
import requests

from market.upstox_tick_engine import TELUGU_RECONNECT, reconnect_forever, retry
from scripts.auto_upstox_morning_login import extract_code


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = _Response(status)


def test_retry_retries_502_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    calls: list[int] = []

    @retry(tries=3, delay=1.5)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _StatusError(502)
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3
    assert sleeps == [1.5, 3.0]


def test_retry_does_not_retry_a_rejected_request(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    calls: list[int] = []

    @retry(tries=3, delay=1.5)
    def rejected():
        calls.append(1)
        raise _StatusError(400)

    with pytest.raises(_StatusError):
        rejected()
    assert len(calls) == 1
    assert sleeps == []


def test_retry_retries_network_errors_then_raises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    calls: list[int] = []

    @retry(tries=3, delay=1.5)
    def offline():
        calls.append(1)
        raise requests.ConnectionError("offline")

    with pytest.raises(requests.ConnectionError):
        offline()
    assert len(calls) == 3
    assert sleeps == [1.5, 3.0]


class _FakeStop:
    def __init__(self, after: int) -> None:
        self.after = after
        self.waits: list[float] = []
        self.done = False

    def is_set(self) -> bool:
        return self.done or len(self.waits) >= self.after

    def wait(self, timeout: float) -> None:
        self.waits.append(timeout)


def test_reconnect_forever_warns_in_telugu_and_backs_off():
    stop = _FakeStop(3)
    connects: list[int] = []
    logs: list[str] = []

    def connect():
        connects.append(1)
        return object()

    def consume(_session):
        raise ConnectionError("drop")

    reconnect_forever(connect, consume, stop, delay=2.0, max_delay=8.0, log=logs.append)

    assert len(connects) == 3
    assert logs == [TELUGU_RECONNECT] * 3
    assert stop.waits == [2.0, 4.0, 8.0]


def test_reconnect_forever_calls_recovery_callback():
    stop = _FakeStop(99)
    connects: list[int] = []
    recovered: list[int] = []

    def connect():
        connects.append(1)
        return object()

    def consume(_session):
        if len(connects) < 3:
            raise ConnectionError("drop")
        stop.done = True

    reconnect_forever(
        connect,
        consume,
        stop,
        delay=0.0,
        log=lambda _message: None,
        on_reconnect=lambda: recovered.append(1),
    )

    assert len(connects) == 3
    assert recovered == [1, 1]


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://vetukurisuresh.github.io/kronos-runner-public/?code=abc123&state=x", "abc123"),
        ("https://vetukurisuresh.github.io/kronos-runner-public/?state=x", ""),
        ("not a url", ""),
    ],
)
def test_extract_code_never_logs_the_redirect(url: str, code: str):
    assert extract_code(url) == code
