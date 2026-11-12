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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

import requests

from market.upstox_stream import Candle
from market.upstox_full_decoder import decode_frame
from market.data_sanitizer import SanitizedTickPipeline, SanitizeResult

RING_CAPACITY = 512
INTERVALS = (1, 5, 15)
TICK_SCAN_MS = 100
MINUTE_SCAN_SEC = 60
MASTER_SCAN_SEC = 300
IST = timezone(timedelta(hours=5, minutes=30))
V3_INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/{minutes}"
V3_HISTORY_URL = (
    "https://api.upstox.com/v3/historical-candle/{key}/minutes/{minutes}/{to_date}/{from_date}"
)
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
            try:
                log(TELUGU_RECONNECT)
            except UnicodeEncodeError:
                # A cp1252 console must not take down the reconnect thread.
                print("feed disconnected - reconnecting...", flush=True)
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
    bid_qty: int = 0  # aggregate across all five levels
    ask_qty: int = 0  # aggregate across all five levels
    bid_qty_5lvl: int = 0
    ask_qty_5lvl: int = 0
    bid_qty_top: int = 0
    ask_qty_top: int = 0
    delta: float = 0.0
    theta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    iv: float = 0.0
    tbq: float = 0.0
    tsq: float = 0.0
    ltq: int = 0
    cp: float = 0.0
    bids: list[dict[str, float]] = field(default_factory=list)
    asks: list[dict[str, float]] = field(default_factory=list)
    market_ohlc: dict[str, dict[str, float]] = field(default_factory=dict)
    ts: float = 0.0

    def merge(self, **fields: float) -> "Quote":
        for name, value in fields.items():
            if value is None or name not in self.__dataclass_fields__:
                continue
            if name in {"bids", "asks"}:
                if value:
                    setattr(self, name, value)
                    top = "bid_qty_top" if name == "bids" else "ask_qty_top"
                    if not getattr(self, top):
                        try:
                            setattr(self, top, int(float(value[0].get("qty") or 0)))
                        except (AttributeError, IndexError, TypeError, ValueError):
                            pass
            elif name == "market_ohlc":
                if value:
                    setattr(self, name, value)
            elif name in {"theta", "gamma", "vega", "rho", "delta", "cp"}:
                if value is not None:
                    setattr(self, name, float(value))
            elif name == "bid_qty_5":
                if value:
                    self.bid_qty_5lvl = int(float(value))
            elif name == "ask_qty_5":
                if value:
                    self.ask_qty_5lvl = int(float(value))
            elif name in {"bid_qty", "ask_qty"}:
                if value:
                    setattr(self, name, int(value))
                    alias = "bid_qty_5lvl" if name == "bid_qty" else "ask_qty_5lvl"
                    if not getattr(self, alias):
                        setattr(self, alias, int(value))
            elif name in {"ts", "bid_qty_5lvl", "ask_qty_5lvl",
                         "bid_qty_top", "ask_qty_top", "ltq"}:
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
            "bid_qty_5lvl": self.bid_qty_5lvl or self.bid_qty,
            "ask_qty_5lvl": self.ask_qty_5lvl or self.ask_qty,
            "bid_qty_top": self.bid_qty_top,
            "ask_qty_top": self.ask_qty_top,
            "delta": round(self.delta, 3),
            "theta": round(self.theta, 4),
            "gamma": round(self.gamma, 6),
            "vega": round(self.vega, 4),
            "rho": round(self.rho, 4),
            "tbq": round(self.tbq, 2),
            "tsq": round(self.tsq, 2),
            "tbq_tsq": round(self.tbq / self.tsq, 4) if self.tsq else 0.0,
            "ltq": self.ltq,
            "cp": round(self.cp, 2),
            "bids": list(self.bids),
            "asks": list(self.asks),
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

    def clear(self) -> None:
        self._items.clear()

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
    market_ohlc: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "quote": self.quote.as_dict(),
            "bars": {f"{minutes}m": len(self.buffers[minutes]) for minutes in INTERVALS},
            "ring_full": all(self.buffers[minutes].full for minutes in INTERVALS),
            "market_ohlc": self.market_ohlc,
        }


def _align(epoch: float, minutes: int) -> int:
    step = minutes * 60
    return int(epoch) - (int(epoch) % step)


def _depth(payload: dict[str, Any]) -> dict[str, Any]:
    """Legacy five-level depth: aggregate quantities, expose the top level."""
    quotes = ((payload.get("marketLevel") or {}).get("bidAskQuote") or [])
    bids: list[dict[str, float]] = []
    asks: list[dict[str, float]] = []
    for raw in quotes[:5]:
        row = raw or {}
        bid_p = float(row.get("bidP") or row.get("bid") or 0.0)
        bid_q = float(row.get("bidQ") or row.get("bidQty") or 0.0)
        ask_p = float(row.get("askP") or row.get("ask") or 0.0)
        ask_q = float(row.get("askQ") or row.get("askQty") or 0.0)
        if bid_p > 0 and bid_q > 0:
            bids.append({"price": round(bid_p, 2), "qty": round(bid_q, 2)})
        if ask_p > 0 and ask_q > 0:
            asks.append({"price": round(ask_p, 2), "qty": round(ask_q, 2)})
    if not bids and not asks:
        return {}
    return {
        "bid": bids[0]["price"] if bids else 0.0,
        "bid_qty": sum(row["qty"] for row in bids),
        "bid_qty_top": bids[0]["qty"] if bids else 0,
        "ask": asks[0]["price"] if asks else 0.0,
        "ask_qty": sum(row["qty"] for row in asks),
        "ask_qty_top": asks[0]["qty"] if asks else 0,
        "bids": bids,
        "asks": asks,
    }


def parse_v3_frames(raw: str | bytes | dict) -> list[tuple[str, dict[str, Any]]]:
    """Decode one V3 frame into ``(instrument_key, tick_fields)`` pairs."""
    return decode_frame(raw).ticks


def _candle_epoch(value: Any) -> int:
    """Upstox returns ISO-8601 timestamps; replay fixtures sometimes use epoch."""
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1000.0) if number > 100_000_000_000 else int(number)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=IST)
        return int(moment.timestamp())
    except ValueError:
        return 0


def parse_upstox_candle_row(row: list[Any] | tuple[Any, ...], minutes: int) -> Candle | None:
    """Convert one official V3 candle row into the engine's sealed bar shape."""
    try:
        start = _candle_epoch(row[0])
        open_, high, low, close = (float(row[index]) for index in (1, 2, 3, 4))
        volume = float(row[5] or 0.0)
    except (IndexError, TypeError, ValueError):
        return None
    if start <= 0 or min(open_, high, low, close) <= 0:
        return None
    return Candle(
        start=start,
        minutes=minutes,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ticks=0,
    )


def _candles_from_payload(payload: dict[str, Any], minutes: int) -> list[Candle]:
    rows = ((payload or {}).get("data") or {}).get("candles") or []
    parsed = [parse_upstox_candle_row(row, minutes) for row in rows]
    return [candle for candle in parsed if candle is not None]


def fetch_upstox_candles(
    instrument_key: str,
    minutes: int,
    *,
    access_token: str,
    intraday: bool = True,
    to_date: str = "",
    from_date: str = "",
    timeout: float = 15.0,
    session: Any = None,
) -> list[Candle]:
    """Fetch one official Upstox V3 candle endpoint.

    ``intraday=True`` maps to the V3 intraday path. Historical requests require
    both dates (``YYYY-MM-DD``) and are used to fill the 512-bar ring. The HTTP
    session is injectable so parity tests never touch the broker.
    """
    if not instrument_key:
        raise ValueError("instrument_key is required")
    if minutes not in INTERVALS:
        raise ValueError(f"minutes must be one of {INTERVALS}")
    if not access_token:
        raise ValueError("UPSTOX_ACCESS_TOKEN is required")
    key = quote(str(instrument_key), safe="")
    if intraday:
        url = V3_INTRADAY_URL.format(key=key, minutes=minutes)
        params: dict[str, str] = {}
    else:
        if not to_date or not from_date:
            raise ValueError("to_date and from_date are required for historical candles")
        url = V3_HISTORY_URL.format(
            key=key, minutes=minutes, to_date=to_date, from_date=from_date
        )
        params = {}
    client = session or requests
    response = client.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return sorted(_candles_from_payload(response.json(), minutes), key=lambda candle: candle.start)


def fetch_official_backfill(
    instrument_key: str,
    minutes: int,
    *,
    access_token: str,
    days: int = 30,
    timeout: float = 15.0,
    session: Any = None,
    today: datetime | None = None,
) -> list[Candle]:
    """Seed 512 bars from official prior-day + today intraday candles.

    Prior sessions are requested first, then today's intraday endpoint is merged
    and de-duplicated. A failure on one leg does not silently fabricate data; the
    remaining official leg is returned, and an empty result is raised to the
    caller as an unavailable broker archive.
    """
    current = today or datetime.now(tz=IST)
    end = current.date() - timedelta(days=1)
    start = end - timedelta(days=max(int(days), 1))
    bars: list[Candle] = []
    # Upstox rejects very wide intraday ranges (a 90-day 15m request returns
    # only today), so walk the archive in 30-day windows and merge.  One failed
    # window must not discard the others.
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=29), end)
        try:
            bars.extend(
                fetch_upstox_candles(
                    instrument_key, minutes, access_token=access_token, intraday=False,
                    to_date=window_end.isoformat(), from_date=window_start.isoformat(),
                    timeout=timeout, session=session,
                )
            )
        except Exception:  # noqa: BLE001 - keep every window that did load
            pass
        window_start = window_end + timedelta(days=1)
    try:
        bars.extend(
            fetch_upstox_candles(
                instrument_key, minutes, access_token=access_token, intraday=True,
                timeout=timeout, session=session,
            )
        )
    except Exception:  # noqa: BLE001 - caller may still use historical bars
        pass
    unique = {candle.start: candle for candle in bars}
    ordered = sorted(unique.values(), key=lambda candle: candle.start)
    if not ordered:
        raise RuntimeError(f"Upstox returned no official {minutes}m candles for {instrument_key}")
    return ordered[-RING_CAPACITY:]


class UpstoxTickEngine:
    """Tick -> 1m/5m/15m candles -> three scan speeds."""

    def __init__(
        self,
        capacity: int = RING_CAPACITY,
        on_minute: Callable[[str, Candle], None] | None = None,
        on_five_minute: Callable[[str, Candle], None] | None = None,
        on_fifteen_minute: Callable[[str, Candle], None] | None = None,
        clock: Callable[[], float] = time.time,
        sanitizer: SanitizedTickPipeline | None = None,
    ) -> None:
        self.capacity = int(capacity)
        self.on_minute = on_minute
        self.on_five_minute = on_five_minute
        self.on_fifteen_minute = on_fifteen_minute
        self.clock = clock
        self.sanitizer = sanitizer or SanitizedTickPipeline(clock=clock)
        self.instruments: dict[str, InstrumentState] = {}
        self.ticks = 0
        self.sealed: dict[str, int] = {"1m": 0, "5m": 0, "15m": 0}
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
        """Consume one protobuf frame through the sub-ms sanitizer."""
        closed: list[dict[str, Any]] = []
        rejected = 0
        for key, tick in parse_v3_frames(raw):
            result = self.ingest_tick(key, tick)
            if not result.ok:
                rejected += 1
                continue
            closed.extend(result.sealed)
        return {
            "sealed": closed,
            "ticks": self.ticks,
            "rejected": rejected,
            "sanitizer": self.sanitizer.snapshot(),
        }

    def ingest_tick(self, key: str, tick: Any = None, **fields: Any) -> SanitizeResult:
        """Sanitize a decoded socket tick, then update the candle rings.

        Accepts either ``ingest_tick(key, decoded_tick)`` or the live session's
        ``ingest_tick(key, price, volume=delta, ts_ms=..., **tick_fields)``.
        The live path uses this method, not :meth:`on_tick`, so a corrupt
        protobuf frame can never reach the 512-bar buffers or KRONOS models.
        """
        if isinstance(tick, Mapping):
            payload = dict(tick)
            payload.update(fields)
        else:
            payload = {"price": tick, "ltp": tick, **fields}
        result = self.sanitizer.sanitize_tick(key, payload)
        if not result.ok or not result.tick:
            return result
        clean = dict(result.tick)
        try:
            price = float(clean.pop("ltp", clean.pop("price", 0.0)))
        except (TypeError, ValueError):
            return result
        volume = float(clean.pop("volume", 0.0) or 0.0)
        amount = float(clean.pop("amount", 0.0) or 0.0)
        ts_ms = clean.pop("ts_ms", clean.pop("ltt", None))
        clean.pop("timestamp", None)
        result.sealed = self.on_tick(
            key, price, volume=volume, ts_ms=ts_ms, amount=amount, **clean
        )
        return result

    def on_tick(
        self,
        key: str,
        price: float,
        volume: float = 0.0,
        ts_ms: float | None = None,
        amount: float = 0.0,
        **fields: float,
    ) -> list[dict[str, Any]]:
        if price <= 0:
            return []
        epoch = float(ts_ms) / 1000.0 if ts_ms else self.clock()
        state = self.register(key)
        clean_fields = {
            key: value for key, value in fields.items()
            if value is not None and key not in {"ltp", "price", "ts_ms", "ltt"}
        }
        state.quote.merge(ltp=price, ts=epoch, **clean_fields)
        if fields.get("market_ohlc"):
            state.market_ohlc = dict(fields["market_ohlc"])
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
                    "amount": amount,
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
                    "amount": amount,
                }
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
                current["volume"] += volume
                current["amount"] = current.get("amount", 0.0) + amount
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
            amount=float(bucket.get("amount") or 0.0),
        )
        state.buffers[minutes].append(candle)
        name = f"{minutes}m"
        self.sealed[name] = self.sealed.get(name, 0) + 1
        callback = {
            1: self.on_minute,
            5: self.on_five_minute,
            15: self.on_fifteen_minute,
        }.get(minutes)
        if callback is not None:
            callback(state.key, candle)
        return {"key": state.key, "minutes": minutes, "candle": candle.as_dict()}

    def seed(self, key: str, minutes: int, candles: Iterable[Candle]) -> int:
        """Replace a buffer with official REST candles (oldest first).

        This is the only supported way to pre-fill the 512-bar window. It never
        fabricates a bar, so a chart seeded from Upstox is byte-for-byte the
        broker's OHLC instead of a local approximation.
        """
        if minutes not in INTERVALS:
            raise ValueError(f"unsupported interval {minutes}; use one of {INTERVALS}")
        state = self.register(key)
        ring = state.buffers[minutes]
        ring.clear()
        rows = sorted(
            {int(candle.start): candle for candle in candles}.values(),
            key=lambda candle: candle.start,
        )
        for candle in rows[-self.capacity:]:
            ring.append(candle)
        return len(ring)

    def current(self, key: str, minutes: int) -> dict[str, Any] | None:
        """The forming bucket using the exchange ``ltt`` timestamp."""
        state = self.instruments.get(key)
        if state is None:
            return None
        bucket = state._open.get(minutes)
        if not bucket:
            return None
        return {
            "time": int(bucket["key"]),
            "open": round(float(bucket["open"]), 2),
            "high": round(float(bucket["high"]), 2),
            "low": round(float(bucket["low"]), 2),
            "close": round(float(bucket["close"]), 2),
            "volume": round(float(bucket["volume"]), 2),
            "amount": round(float(bucket.get("amount") or 0.0), 2),
        }

    def reconcile(self, key: str, minutes: int, candle: Candle) -> bool:
        """Replace the matching official bar (or append) without inventing data.

        The live session calls this after comparing the last sealed bar with the
        broker's newest intraday candle. Returns ``True`` when the ring changed.
        """
        if minutes not in INTERVALS:
            return False
        ring = self.register(key).buffers[minutes]
        rows = list(ring)
        if not rows or candle.start > rows[-1].start:
            ring.append(candle)
            return True
        matched = False
        for index in range(len(rows) - 1, -1, -1):
            if rows[index].start == candle.start:
                rows[index] = candle
                matched = True
                break
            if rows[index].start < candle.start:
                break
        if not matched:
            return False
        ring.clear()
        for row in rows:
            ring.append(row)
        return True

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
