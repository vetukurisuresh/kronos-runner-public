from __future__ import annotations

import pytest

from scripts.upstox_live_session import bar_range


@pytest.mark.parametrize(
    ("bar", "expected"),
    [
        ({"high": 101.5, "low": 99.0}, 2.5),
        ({"h": 101.5, "l": 99.0}, 2.5),
        ({"high": 100.0, "low": 100.0}, 0.0),
    ],
)
def test_bar_range_accepts_rest_and_ui_keys(bar: dict, expected: float) -> None:
    assert bar_range(bar) == pytest.approx(expected)


@pytest.mark.parametrize(
    "bar",
    [
        {},
        {"high": 100.0},
        {"h": None, "l": 99.0},
        {"high": 99.0, "low": 101.0},
        {"high": 0.0, "low": 0.0},
    ],
)
def test_bar_range_rejects_incomplete_or_invalid_candles(bar: dict) -> None:
    assert bar_range(bar) is None
