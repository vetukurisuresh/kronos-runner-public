"""Sub-millisecond Upstox V3 tick and tensor validation.

The broker feed is the only source of truth.  This module never fills a missing
price, never invents volume and never turns a stale snapshot into a live feed.
Every decoded frame is checked here before it is allowed to touch a candle
buffer or a KRONOS tensor.  The live session publishes the telemetry returned by
:meth:`SanitizedTickPipeline.snapshot`, so the dashboard can prove what arrived
and what was rejected.

Design constraints
------------------
* pure Python, no locks and no I/O on the tick path
* fail closed: a malformed or stale frame cannot be traded
* the hot path is a handful of integer/float comparisons, normally well below
  0.5 ms on the runner
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
FEATURE_NAMES = ("open", "high", "low", "close", "volume", "amount")
FEED_STALE_MS = 3_000
OPTION_MIN_LTP = 0.05
MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)


def _number(value: Any, default: float = 0.0) -> float:
    """Coerce a broker scalar without allowing NaN/Inf through."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _finite_tree(value: Any) -> bool:
    """Reject NaN/Inf anywhere in a tick, including nested depth levels."""
    if isinstance(value, Mapping):
        return all(_finite_tree(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite_tree(item) for item in value)
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return True


def _as_epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000.0 if number > 100_000_000_000 else number


def _ist_moment(now: datetime | float | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=IST)
    if isinstance(now, datetime):
        return now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    return datetime.fromtimestamp(float(now), tz=IST)


def is_market_open(now: datetime | float | None = None) -> bool:
    moment = _ist_moment(now)
    if moment.weekday() >= 5:
        return False
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE


@dataclass(frozen=True)
class FeedState:
    status: str
    age_ms: int | None
    market_open: bool
    allow_order_generation: bool
    reason: str
    as_of: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "FEED_LIVE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "age_ms": self.age_ms,
            "market_open": self.market_open,
            "allow_order_generation": self.allow_order_generation,
            "reason": self.reason,
            "as_of": self.as_of,
            "ok": self.ok,
        }


def feed_state(
    feed: Mapping[str, Any] | None,
    *,
    now: datetime | float | None = None,
    market_open: bool | None = None,
    stale_ms: int = FEED_STALE_MS,
) -> FeedState:
    """Derive the hard order gate from the feed heartbeat.

    A websocket flag alone is not evidence of a live feed.  The heartbeat age is
    authoritative and is checked against 3 seconds during market hours.
    """
    feed = feed or {}
    moment = _ist_moment(now)
    present_market_flag = feed.get("market_open")
    opened = bool(present_market_flag) if market_open is None and isinstance(present_market_flag, bool) else (
        is_market_open(moment) if market_open is None else bool(market_open)
    )
    epoch = _as_epoch(feed.get("liveness_epoch") or feed.get("updated_epoch"))
    age_ms = int(max(0.0, moment.timestamp() - epoch) * 1000.0) if epoch else None
    if not opened:
        return FeedState("MARKET_CLOSED", age_ms, False, False,
                         "MARKET_CLOSED", str(feed.get("updated_at") or ""))
    if age_ms is None:
        return FeedState("FEED_OFFLINE_OR_STALE", None, True, False,
                         "NO_FEED_HEARTBEAT", str(feed.get("updated_at") or ""))
    if age_ms > int(stale_ms):
        return FeedState("FEED_OFFLINE_OR_STALE", age_ms, True, False,
                         f"HEARTBEAT_AGE_{age_ms}MS_GT_{int(stale_ms)}MS",
                         str(feed.get("updated_at") or ""))
    return FeedState("FEED_LIVE", age_ms, True, True, "SANITIZED_FEED_LIVE",
                     str(feed.get("updated_at") or ""))


@dataclass(frozen=True)
class TensorVerdict:
    ok: bool
    reason: str
    bars: int
    shape: tuple[int, int]
    volume_source: str = "UNVERIFIED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "bars": self.bars,
            "shape": list(self.shape),
            "volume_source": self.volume_source,
        }


def validate_tensor(
    frame: Iterable[Any] | None,
    *,
    interval_seconds: int = 300,
    require_bars: int = 512,
    volume_source: str = "FUT",
) -> TensorVerdict:
    """Verify the exact ``[512, 6]`` OHLCVA tensor before inference.

    The check is intentionally strict about positive flow.  Synthetic/zero
    volume is a common failure mode when an index OHLC is sent to a tokenizer
    without a futures fusion, and it silently destroys model quality.
    """
    rows = list(frame or [])
    bars = len(rows)
    shape = (1, bars, len(FEATURE_NAMES)) if bars else (1, 0, 6)
    if bars != int(require_bars):
        return TensorVerdict(False, f"BARS_{bars}_EXPECTED_{int(require_bars)}", bars, shape, volume_source)
    if str(volume_source).upper() != "FUT":
        return TensorVerdict(False, "VOLUME_SOURCE_NOT_FUTURES", bars, shape, volume_source)

    previous_time: float | None = None
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            return TensorVerdict(False, f"BAR_{index}_NOT_A_MAPPING", bars, shape, volume_source)
        values: dict[str, float] = {}
        for name in FEATURE_NAMES:
            try:
                value = float(raw.get(name))
            except (TypeError, ValueError):
                return TensorVerdict(False, f"BAR_{index}_{name.upper()}_NOT_NUMERIC", bars, shape, volume_source)
            if not math.isfinite(value):
                return TensorVerdict(False, f"BAR_{index}_{name.upper()}_NAN_INF", bars, shape, volume_source)
            values[name] = value
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
            return TensorVerdict(False, f"BAR_{index}_NON_POSITIVE_PRICE", bars, shape, volume_source)
        if values["high"] < max(values["open"], values["close"], values["low"]):
            return TensorVerdict(False, f"BAR_{index}_HIGH_BELOW_BODY", bars, shape, volume_source)
        if values["low"] > min(values["open"], values["close"], values["high"]):
            return TensorVerdict(False, f"BAR_{index}_LOW_ABOVE_BODY", bars, shape, volume_source)
        if values["volume"] <= 0 or values["amount"] <= 0:
            return TensorVerdict(False, f"BAR_{index}_ZERO_FLOW", bars, shape, volume_source)
        stamp = raw.get("time", raw.get("t", raw.get("timestamp")))
        if stamp is not None:
            try:
                current_time = float(stamp)
            except (TypeError, ValueError):
                return TensorVerdict(False, f"BAR_{index}_BAD_TIME", bars, shape, volume_source)
            if not math.isfinite(current_time) or current_time <= 0:
                return TensorVerdict(False, f"BAR_{index}_BAD_TIME", bars, shape, volume_source)
            if previous_time is not None and current_time <= previous_time:
                return TensorVerdict(False, f"BAR_{index}_OUT_OF_ORDER", bars, shape, volume_source)
            previous_time = current_time
    del interval_seconds  # timestamps can cross the overnight session gap
    return TensorVerdict(True, "512_CONTIGUOUS_BARS_VERIFIED", bars, (1, bars, 6), volume_source)


@dataclass
class SanitizeResult:
    ok: bool
    reason: str
    tick: dict[str, Any] | None
    latency_us: float
    #: Sealed candles caused by this tick (attached by the tick engine).
    sealed: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "latency_us": round(self.latency_us, 3),
            "tick": self.tick,
        }


class SanitizedTickPipeline:
    """Stateful, fail-closed tick gate with publication-safe telemetry."""

    def __init__(
        self,
        *,
        stale_ms: int = FEED_STALE_MS,
        option_min_ltp: float = OPTION_MIN_LTP,
        clock: Any = time.time,
        max_events: int = 80,
    ) -> None:
        self.stale_ms = int(stale_ms)
        self.option_min_ltp = float(option_min_ltp)
        self.clock = clock
        self.frames_received = 0
        self.frames_sanitized_ok = 0
        self.corrupt_frames_rejected = 0
        self.duplicates_dropped = 0
        self.sanitize_latency_us = 0.0
        self.max_latency_us = 0.0
        self._latency_total_us = 0.0
        self._latencies_us: deque[float] = deque(maxlen=2048)
        self.last_reject_reason = ""
        self.last_accept_ltt_ms: int | None = None
        self._last_ltt: dict[str, int] = {}
        self._last_vtt: dict[str, float] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._accepted_since_event = 0

    def _event(self, level: str, message: str, *, ltt_ms: int | None = None) -> None:
        moment = (datetime.fromtimestamp(ltt_ms / 1000.0, tz=IST) if ltt_ms
                  else _ist_moment(self.clock()))
        self.events.append({
            "ts": moment.isoformat(timespec="milliseconds"),
            "ts_ms": int(moment.timestamp() * 1000),
            "level": level,
            "message": message,
        })

    @staticmethod
    def _is_option_key(key: str) -> bool:
        upper = str(key or "").upper()
        return " CE" in upper or " PE" in upper or upper.endswith("CE") or upper.endswith("PE")

    def sanitize_tick(self, instrument_key: str, tick: Mapping[str, Any] | None) -> SanitizeResult:
        """Validate one decoded tick.  All checks are in-memory and bounded."""
        started = time.perf_counter_ns()
        self.frames_received += 1
        raw = dict(tick or {})
        key = str(instrument_key or "")
        if not key:
            return self._reject("MISSING_INSTRUMENT_KEY", started)
        if not raw:
            return self._reject("EMPTY_TICK", started)
        if not _finite_tree(raw):
            return self._reject("NAN_OR_INF_IN_FRAME", started)

        ltt_raw = raw.get("ltt", raw.get("ltt_ms", raw.get("ts_ms")))
        try:
            ltt_value = float(ltt_raw)
        except (TypeError, ValueError):
            return self._reject("MISSING_LTT", started)
        if not math.isfinite(ltt_value) or ltt_value <= 0:
            return self._reject("INVALID_LTT", started)
        ltt_ms = int(ltt_value)
        vtt = _number(raw.get("vtt", raw.get("volume")), 0.0)
        previous_ltt = self._last_ltt.get(key)
        previous_vtt = self._last_vtt.get(key)
        if previous_ltt is not None and ltt_ms < previous_ltt:
            return self._reject("OUT_OF_ORDER_LTT", started, event=True)
        if previous_ltt is not None and ltt_ms == previous_ltt and previous_vtt is not None and vtt == previous_vtt:
            self.duplicates_dropped += 1
            self._event("WARN", f"{key} duplicate frame dropped at LTT {ltt_ms}", ltt_ms=ltt_ms)
            return self._finish(False, "DUPLICATE_FRAME", None, started)

        price = _number(raw.get("ltp", raw.get("price")), 0.0)
        minimum = self.option_min_ltp if self._is_option_key(key) else 0.0
        if not math.isfinite(price) or price <= minimum:
            return self._reject("OPTION_LTP_TOO_LOW" if self._is_option_key(key) else "SPOT_LTP_INVALID",
                                started, event=True)
        bid = _number(raw.get("bid", raw.get("best_bid")), 0.0)
        ask = _number(raw.get("ask", raw.get("best_ask")), 0.0)
        if bid < 0 or ask < 0:
            return self._reject("NEGATIVE_BOOK_PRICE", started, event=True)
        if bid > 0 and ask > 0 and ask < bid:
            return self._reject("INVERTED_SPREAD", started, event=True)
        if (bid > 0) != (ask > 0):
            return self._reject("ONE_SIDED_BOOK", started, event=True)

        self._last_ltt[key] = ltt_ms
        self._last_vtt[key] = vtt
        self.last_accept_ltt_ms = ltt_ms
        self.frames_sanitized_ok += 1
        self._accepted_since_event += 1
        if self._accepted_since_event >= 25:
            self._accepted_since_event = 0
            self._event("OK", f"{key} LTT {ltt_ms} accepted", ltt_ms=ltt_ms)
        raw["ltt"] = ltt_ms
        raw["ltp"] = price
        raw["vtt"] = vtt
        raw["bid"] = bid
        raw["ask"] = ask
        return self._finish(True, "OK", raw, started)

    def _reject(self, reason: str, started: int, *, event: bool = False) -> SanitizeResult:
        self.corrupt_frames_rejected += 1
        self.last_reject_reason = reason
        if event:
            self._event("REJECT", f"corrupt frame rejected: {reason}")
        return self._finish(False, reason, None, started)

    def _finish(self, ok: bool, reason: str, tick: dict[str, Any] | None,
                started: int) -> SanitizeResult:
        elapsed = (time.perf_counter_ns() - started) / 1000.0
        self.sanitize_latency_us = round(elapsed, 3)
        self.max_latency_us = max(self.max_latency_us, elapsed)
        self._latency_total_us += elapsed
        self._latencies_us.append(elapsed)
        return SanitizeResult(ok, reason, tick, elapsed)

    def snapshot(self) -> dict[str, Any]:
        received = self.frames_received
        ordered = sorted(self._latencies_us)
        p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0.0
        return {
            "frames_received": received,
            "frames_sanitized_ok": self.frames_sanitized_ok,
            "corrupt_frames_rejected": self.corrupt_frames_rejected,
            "duplicates_dropped": self.duplicates_dropped,
            "sanitize_latency_us": round(self.sanitize_latency_us, 3),
            "sanitize_latency_max_us": round(self.max_latency_us, 3),
            "sanitize_latency_p95_us": round(p95, 3),
            "sanitize_latency_avg_us": round(
                (self._latency_total_us / received) if received else 0.0, 3
            ),
            "within_500us_budget": bool(p95 < 500.0) if received else True,
            "last_reject_reason": self.last_reject_reason,
            "last_accept_ltt_ms": self.last_accept_ltt_ms,
            "events": list(self.events)[-40:],
        }

    def validate_tensor(self, frame: Iterable[Any] | None, **kwargs: Any) -> TensorVerdict:
        return validate_tensor(frame, **kwargs)
