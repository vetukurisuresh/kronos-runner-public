"""Upstox V3 tick-to-candle engine with 512-bar ring buffers.

The transport (websocket) lives in :mod:`market.upstox_stream`. This module only
accepts decoded frames, so replay, the sandbox bridge and the live feed share a
single code path and every rule is unit-testable without a broker connection.

A frame looks like::

    {"feeds": {"NSE_INDEX|Nifty 50": {
        "ltpc": {"ltp": 24986.4, "ltt": 1760000000000},
        "ff": {"marketFF": {
            "ltpc": {"ltp": 24986.4, "ltt": 1760000000000},
            "eFeedDetails": {"atp": 24970.1, "oi": 1245000, "vtt": 980000},
            "marketLevel": {"bidAskQuote": [{"bidP": 24986.3, "bidQ": 75,
                                             "askP": 24986.5, "askQ": 150}]},
            "optionGreeks": {"delta": 0.52, "iv": 14.2}}}}}}

Every instrument keeps two rolling deques capped at ``capacity`` bars (default
512): one for 1-minute candles and one for 5-minute candles. When a tick crosses
a minute boundary the open bucket is sealed, appended to the ring and handed to
the registered callback, which is what drives the 1m and 5m scan loops.
"""

from __future__ import annotations

import functools
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import requests

from market.upstox_stream import Candle

RING_CAPACITY = 512
INTERVALS = (1, 5)
TICK_SCAN_MS = 100
MINUTE_SCAN_SEC = 60
MASTER_SCAN_SEC = 300
TELUGU_RECONNECT = "⚠️ సిగ్నల్ కట్ అయింది, మళ్లీ కనెక్ట్ చేస్తున్నాను..."
RETRY_STATUSES = (502, 503, 504)


def retry(
    tries: int = 3,
    delay: float = 1.5,
    backoff: float = 2.0,
    statuses: tuple[int, ...] = RETRY_STATUSES,
):
    """Retry transient broker/network failures transparently.

    HTTP responses that carry a retryable status (502/504, plus 503) are
    re-attempted; connection/timeout exceptions are re-attempted; other 4xx
    answers fail immediately so a rejected token is not hammered.
    """
    if tries < 1:
        raise ValueError("tries must be at least 1")

    def decorate(operation: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(operation)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(tries):
                try:
                    return operation(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - classified below
                    response = getattr(exc, "response", None)
                    status = getattr(response, "status_code", None)
                    retryable = (
                        status in statuses
                        if status is not None
                        else isinstance(exc, requests.RequestException)
                    )
                    if not retryable or attempt + 1 >= tries:
                        raise
                    time.sleep(delay * (backoff ** attempt))
            raise RuntimeError("retry loop exhausted")

        return wrapper

    return decorate


def reconnect_forever(
    connect: Callable[[], Any],
    consume: Callable[[Any], None],
    stop: Any,
    *,
    delay: float = 2.0,
    max_delay: float = 30.0,
    on_drop: Callable[[Exception], None] | None = None,
    on_reconnect: Callable[[], None] | None = None,
    log: Callable[[str], None] = print,
) -> None:
    """Keep a broker socket alive for as long as ``stop`` is not set.

    Every disconnect is logged in Telugu, then retried after an exponential
    backoff (2 s, 4 s, 8 s ... capped at ``max_delay``). There is no retry
    ceiling: the market feed is allowed to recover for the whole session.
    """
    failures = 0
    while not stop.is_set():
        recovering = failures > 0
        try:
            session = connect()
            if recovering and on_reconnect is not None:
                on_reconnect()
            consume(session)
            if stop.is_set():
                return
            raise ConnectionError("feed closed")
        except Exception as exc:  # noqa: BLE001 - every failure is a reconnect
            failures += 1
            if on_drop is not None:
                on_drop(exc)
            log(TELUGU_RECONNECT)
            wait = min(delay * (2 ** min(failures - 1, 4)), max_delay)
            stop.wait(wait)

INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}
HEAVYWEIGHT_KEYS = {
    name: f"NSE_EQ|{name}" for name in ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS")
}


@dataclass
class Quote:
    """Latest telemetry for one instrument (ribbon fields)."""

    ltp: float = 0.0
    atp: float = 0.0  # running VWAP published by eFeedDetails
    oi: float = 0.0
    vtt: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    delta: float = 0.0
    iv: float = 0.0
    ts: float = 0.0

    def merge(self, **fields: float) -> "Quote":
        for name, value in fields.items():
            if value is None or name not in self.__dataclass_fields__:
                continue
            if name in {"ts", "bid_qty", "ask_qty"}:
                if value:
                    setattr(self, name, int(value))
            elif float(value) > 0:
                setattr(self, name, float(value))
        return self

    @property
    def spread(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return round(max(self.ask - self.bid, 0.0), 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ltp": round(self.ltp, 2),
            "atp": round(self.atp, 2),
            "oi": int(self.oi),
            "vtt": int(self.vtt),
            "bid": round(self.bid, 2),
            "ask": round(self.ask, 2),
            "bid_qty": self.bid_qty,
            "ask_qty": self.ask_qty,
            "delta": round(self.delta, 3),
            "iv": round(self.iv, 2),
            "spread": self.spread,
            "ts": round(self.ts, 3),
        }


class RingBuffer:
    """Fixed-capacity candle history; oldest bars fall off automatically."""

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        if capacity < 2:
            raise ValueError("capacity must be at least 2")
        self.capacity = int(capacity)
        self._items: deque[Candle] = deque(maxlen=self.capacity)

    def append(self, candle: Candle) -> Candle:
        self._items.append(candle)
        return candle

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def last(self, count: int) -> list[Candle]:
        if count <= 0:
            return []
        return list(self._items)[-count:]

    @property
    def latest(self) -> Candle | None:
        return self._items[-1] if self._items else None

    def closes(self, count: int = 0) -> list[float]:
        items = self.last(count) if count else list(self._items)
        return [candle.close for candle in items]

    def to_list(self, count: int = 0) -> list[dict[str, Any]]:
        items = self.last(count) if count else list(self._items)
        return [candle.as_dict() for candle in items]

    @property
    def full(self) -> bool:
        return len(self._items) == self.capacity


@dataclass
class InstrumentState:
    key: str
    label: str = ""
    kind: str = "index"  # index | stock | option
    quote: Quote = field(default_factory=Quote)
    buffers: dict[int, RingBuffer] = field(
        default_factory=lambda: {minutes: RingBuffer() for minutes in INTERVALS}
    )
    _open: dict[int, dict[str, float]] = field(default_factory=dict)
    #: (epoch, open interest) samples used for the 5-minute unwinding velocity
    oi_history: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=RING_CAPACITY)
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "quote": self.quote.as_dict(),
            "bars": {f"{minutes}m": len(self.buffers[minutes]) for minutes in INTERVALS},
            "ring_full": all(self.buffers[minutes].full for minutes in INTERVALS),
        }


def _align(epoch: float, minutes: int) -> int:
    step = minutes * 60
    return int(epoch) - (int(epoch) % step)


def _depth(payload: dict[str, Any]) -> dict[str, float]:
    quotes = ((payload.get("marketLevel") or {}).get("bidAskQuote") or [])
    if not quotes:
        return {}
    top = quotes[0] or {}
    return {
        "bid": top.get("bidP") or top.get("bid") or 0.0,
        "bid_qty": top.get("bidQ") or top.get("bidQty") or 0,
        "ask": top.get("askP") or top.get("ask") or 0.0,
        "ask_qty": top.get("askQ") or top.get("askQty") or 0,
    }


def parse_v3_frames(raw: str | bytes | dict) -> list[tuple[str, dict[str, Any]]]:
    """Decode one V3 frame into ``(instrument_key, tick_fields)`` pairs."""
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
    else:
        payload = raw
    feeds = (payload or {}).get("feeds") or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for key, feed in feeds.items():
        feed = feed or {}
        market = ((feed.get("ff") or {}).get("marketFF")) or feed.get("ltpc") or {}
        ltpc = market.get("ltpc") or feed.get("ltpc") or market
        price = ltpc.get("ltp", market.get("ltp"))
        if price is None:
            continue
        details = market.get("eFeedDetails") or {}
        greeks = market.get("optionGreeks") or {}
        tick: dict[str, Any] = {
            "price": float(price),
            "volume": float(details.get("vtt") or ltpc.get("ltq") or 0.0),
            "ts_ms": float(ltpc.get("ltt") or time.time() * 1000),
            "atp": details.get("atp"),
            "oi": details.get("oi"),
            "vtt": details.get("vtt"),
            "delta": greeks.get("delta"),
            "iv": greeks.get("iv"),
            **_depth(market),
        }
        out.append((key, tick))
    return out


class UpstoxTickEngine:
    """Tick -> 1m/5m candles -> three scan speeds."""

    def __init__(
        self,
        capacity: int = RING_CAPACITY,
        on_minute: Callable[[str, Candle], None] | None = None,
        on_five_minute: Callable[[str, Candle], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.capacity = int(capacity)
        self.on_minute = on_minute
        self.on_five_minute = on_five_minute
        self.clock = clock
        self.instruments: dict[str, InstrumentState] = {}
        self.ticks = 0
        self.sealed: dict[str, int] = {"1m": 0, "5m": 0}
        self.last_tick_scan = 0.0
        self.last_minute_scan = 0.0
        self.last_master_scan = 0.0

    # -- subscription ------------------------------------------------------
    def register(self, key: str, label: str = "", kind: str = "index") -> InstrumentState:
        state = self.instruments.get(key)
        if state is None:
            state = InstrumentState(
                key=key,
                label=label or key,
                kind=kind,
                buffers={minutes: RingBuffer(self.capacity) for minutes in INTERVALS},
            )
            self.instruments[key] = state
        return state

    def register_universe(
        self, spot: dict[str, float] | None = None, strike_step: int = 50
    ) -> list[str]:
        """Indices + top-5 heavyweights + ATM/ATM+50/ATM-50 CE & PE."""
        keys: list[str] = []
        for name, key in INDEX_KEYS.items():
            self.register(key, name, "index")
            keys.append(key)
        for name, key in HEAVYWEIGHT_KEYS.items():
            self.register(key, name, "stock")
            keys.append(key)
        for index, price in (spot or {}).items():
            if price <= 0:
                continue
            step = strike_step if price > 20_000 else max(strike_step, 100)
            atm = int(round(price / step) * step)
            for strike in (atm - step, atm, atm + step):
                for side in ("CE", "PE"):
                    label = f"{index} {strike} {side}"
                    self.register(label, label, "option")
                    keys.append(label)
        return keys

    # -- ingest ------------------------------------------------------------
    def on_frame(self, raw: str | bytes | dict) -> dict[str, Any]:
        """Consume one frame; returns the candles sealed by it."""
        closed: list[dict[str, Any]] = []
        for key, tick in parse_v3_frames(raw):
            closed.extend(self.on_tick(key, **tick))
        return {"sealed": closed, "ticks": self.ticks}

    def on_tick(
        self,
        key: str,
        price: float,
        volume: float = 0.0,
        ts_ms: float | None = None,
        **fields: float,
    ) -> list[dict[str, Any]]:
        if price <= 0:
            return []
        epoch = float(ts_ms) / 1000.0 if ts_ms else self.clock()
        state = self.register(key)
        state.quote.merge(ltp=price, ts=epoch, **{k: v for k, v in fields.items() if v is not None})
        oi_value = float(fields.get("oi") or 0.0)
        if oi_value > 0:
            state.oi_history.append((epoch, oi_value))
        self.ticks += 1

        closed: list[dict[str, Any]] = []
        for minutes in INTERVALS:
            bucket_key = _align(epoch, minutes)
            current = state._open.get(minutes)
            if current is None:
                state._open[minutes] = {
                    "key": bucket_key,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                }
                continue
            if current["key"] != bucket_key:
                closed.append(self._seal(state, minutes))
                state._open[minutes] = {
                    "key": bucket_key,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                }
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
                current["volume"] += volume
        return closed

    def _seal(self, state: InstrumentState, minutes: int) -> dict[str, Any]:
        bucket = state._open[minutes]
        candle = Candle(
            start=int(bucket["key"]),
            minutes=minutes,
            open=float(bucket["open"]),
            high=float(bucket["high"]),
            low=float(bucket["low"]),
            close=float(bucket["close"]),
            volume=float(bucket["volume"]),
            ticks=1,
        )
        state.buffers[minutes].append(candle)
        name = f"{minutes}m"
        self.sealed[name] = self.sealed.get(name, 0) + 1
        callback = self.on_minute if minutes == 1 else self.on_five_minute
        if callback is not None:
            callback(state.key, candle)
        return {"key": state.key, "minutes": minutes, "candle": candle.as_dict()}

    def flush(self) -> list[dict[str, Any]]:
        """Force-close open buckets (called at session close)."""
        return [
            self._seal(state, minutes)
            for state in self.instruments.values()
            for minutes in list(state._open)
        ]

    # -- scans -------------------------------------------------------------
    def due_scans(self, now: float | None = None) -> list[str]:
        """Which of the three speeds are due at ``now`` (epoch seconds)."""
        moment = self.clock() if now is None else float(now)
        due: list[str] = []
        if (moment - self.last_tick_scan) * 1000 >= TICK_SCAN_MS:
            due.append("tick")
            self.last_tick_scan = moment
        if moment - self.last_minute_scan >= MINUTE_SCAN_SEC:
            due.append("minute")
            self.last_minute_scan = moment
        if moment - self.last_master_scan >= MASTER_SCAN_SEC:
            due.append("master")
            self.last_master_scan = moment
        return due

    def windows(self, key: str, minutes: int, count: int = RING_CAPACITY) -> list[Candle]:
        state = self.instruments.get(key)
        if state is None:
            return []
        return state.buffers[minutes].last(count)

    def change_in_oi(self, key: str, bars: int = 5) -> float:
        """Percent OI change over the last ``bars`` minutes (unwinding is negative)."""
        state = self.instruments.get(key)
        if state is None:
            return 0.0
        history = state.oi_history
        if len(history) < 2:
            return 0.0
        reference = history[0][1] if len(history) <= bars else history[-bars][1]
        if reference <= 0:
            return 0.0
        return round((state.quote.oi - reference) / reference * 100.0, 2)

    def telemetry(self, keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Ribbon rows for the dashboard."""
        wanted = list(keys) if keys else list(self.instruments)
        rows: list[dict[str, Any]] = []
        for key in wanted:
            state = self.instruments.get(key)
            if state is None:
                continue
            rows.append(
                {
                    "key": key,
                    "label": state.label,
                    "kind": state.kind,
                    **state.quote.as_dict(),
                    "oi_change_pct": self.change_in_oi(key),
                }
            )
        return rows

    def snapshot(self) -> dict[str, Any]:
        return {
            "generated_at": self.clock(),
            "ticks": self.ticks,
            "sealed": dict(self.sealed),
            "instruments": [state.as_dict() for state in self.instruments.values()],
            "ribbon": self.telemetry(),
        }
