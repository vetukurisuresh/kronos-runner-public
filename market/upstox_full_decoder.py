"""Lossless Upstox V3 ``full`` market-frame decoder.

The live session used to hand-pick six fields out of the protobuf.  This module
normalises the complete ``FeedResponse`` payload instead: both feed variants
(``marketFF`` and ``indexFF``), all five depth levels, the complete order-flow
block, every Greek, IV, and the native ``marketOHLC`` intervals.  JSON frames
used by tests/replay are accepted through the same path.

The decoder is deliberately broker-transport agnostic.  ``decode_frame`` can be
fed bytes from the websocket, a JSON string, or an already-decoded dict.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from typing import Any, Iterable

from google.protobuf.json_format import MessageToDict

try:  # The generated module is bundled in the repository.
    from market.proto import MarketDataFeedV3_pb2 as pb
except Exception:  # pragma: no cover - proto is a hard requirement in production
    pb = None  # type: ignore[assignment]

IST = timezone(timedelta(hours=5, minutes=30))
DEPTH_LEVELS = 5
INTERVAL_ALIASES = {
    "I1": "1m",
    "1M": "1m",
    "I30": "30m",
    "30M": "30m",
    "1D": "1d",
    "D1": "1d",
    "DAY": "1d",
}
REQUIRED_FULL_FIELDS = (
    "ltp", "ltt", "ltq", "cp", "atp", "vtt", "oi", "tbq", "tsq",
    "iv", "delta", "theta", "gamma", "vega", "rho", "bids", "asks",
    "market_ohlc",
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _normalise_interval(value: Any) -> str:
    text = str(value or "").strip()
    return INTERVAL_ALIASES.get(text.upper(), text.lower())


def _normalise_ohlc(payload: Any) -> dict[str, dict[str, Any]]:
    """Return native exchange OHLC bars keyed by ``1m`` / ``30m`` / ``1d``."""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("ohlc") or payload.get("marketOHLC") or []
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        row = _dict(row)
        interval = _normalise_interval(row.get("interval"))
        if not interval:
            continue
        ts = _int(_first(row, "ts", "time", "timestamp"))
        if ts > 100_000_000_000:
            ts //= 1000
        out[interval] = {
            "interval": interval,
            "open": round(_float(row.get("open")), 4),
            "high": round(_float(row.get("high")), 4),
            "low": round(_float(row.get("low")), 4),
            "close": round(_float(row.get("close")), 4),
            "volume": round(_float(_first(row, "vol", "volume")), 2),
            "time": ts,
        }
    return out


def _depth(market: dict[str, Any]) -> tuple[list[dict[str, float]], list[dict[str, float]], dict[str, Any]]:
    level = _dict(market.get("marketLevel") or market.get("market_level"))
    quotes = level.get("bidAskQuote") or level.get("bid_ask_quote") or []
    bids: list[dict[str, float]] = []
    asks: list[dict[str, float]] = []
    for row in quotes[:DEPTH_LEVELS]:
        row = _dict(row)
        bid_p = _float(_first(row, "bidP", "bid_p", "bid"))
        bid_q = _float(_first(row, "bidQ", "bid_q", "bid_qty"))
        ask_p = _float(_first(row, "askP", "ask_p", "ask"))
        ask_q = _float(_first(row, "askQ", "ask_q", "ask_qty"))
        if bid_p > 0 and bid_q > 0:
            bids.append({"price": round(bid_p, 2), "qty": round(bid_q, 2)})
        if ask_p > 0 and ask_q > 0:
            asks.append({"price": round(ask_p, 2), "qty": round(ask_q, 2)})
    bid_qty = sum(row["qty"] for row in bids)
    ask_qty = sum(row["qty"] for row in asks)
    ratio = bid_qty / ask_qty if ask_qty else (999.0 if bid_qty else 1.0)
    total = bid_qty + ask_qty
    top_bid = bids[0] if bids else {"price": 0.0, "qty": 0.0}
    top_ask = asks[0] if asks else {"price": 0.0, "qty": 0.0}
    return bids, asks, {
        "bids": bids,
        "asks": asks,
        "bid_qty": round(bid_qty, 2),
        "ask_qty": round(ask_qty, 2),
        "bid_qty_5": round(bid_qty, 2),
        "ask_qty_5": round(ask_qty, 2),
        "obi_5": round(ratio, 4),
        "imbalance": round((bid_qty - ask_qty) / total, 4) if total else 0.0,
        "levels": min(len(bids), len(asks)),
        "bid": round(top_bid["price"], 2),
        "bid_qty_top": round(top_bid["qty"], 2),
        "ask": round(top_ask["price"], 2),
        "ask_qty_top": round(top_ask["qty"], 2),
    }


def _extract_feed(feed: dict[str, Any]) -> dict[str, Any] | None:
    feed = _dict(feed)
    full = _dict(feed.get("fullFeed") or feed.get("ff"))
    market = _dict(full.get("marketFF") or full.get("market_ff"))
    index = _dict(full.get("indexFF") or full.get("index_ff"))
    first = _dict(feed.get("firstLevelWithGreeks") or feed.get("first_level_with_greeks"))
    source = market or index or first
    ltpc = _dict(source.get("ltpc") or feed.get("ltpc"))
    price = _float(_first(ltpc, "ltp", "price") or _first(source, "ltp", "price"))
    if price <= 0:
        return None

    details = _dict(source.get("eFeedDetails") or source.get("e_feed_details"))
    greeks = _dict(source.get("optionGreeks") or source.get("option_greeks"))
    bids, asks, depth = _depth(market or source)
    atp = _float(_first(details, "atp") or _first(source, "atp"))
    vtt = _int(_first(details, "vtt") or _first(source, "vtt"))
    oi = _float(_first(details, "oi") or _first(source, "oi"))
    tbq = _float(_first(details, "tbq") or _first(source, "tbq"))
    tsq = _float(_first(details, "tsq") or _first(source, "tsq"))
    iv = _float(_first(greeks, "iv") or _first(source, "iv") or _first(details, "iv"))
    ts_ms = _int(_first(ltpc, "ltt", "ts", "timestamp"))
    if ts_ms and ts_ms < 100_000_000_000:
        ts_ms *= 1000
    if not ts_ms:
        ts_ms = int(time.time() * 1000)
    native = _normalise_ohlc(source.get("marketOHLC") or source.get("market_ohlc") or {})
    tick: dict[str, Any] = {
        "price": price,
        "ltp": price,
        "ltt": ts_ms,
        "ts_ms": ts_ms,
        "ltq": _int(ltpc.get("ltq")),
        "cp": _float(ltpc.get("cp")),
        "atp": atp,
        "vtt": vtt,
        "volume": vtt,
        "oi": oi,
        "tbq": tbq,
        "tsq": tsq,
        "tbq_tsq": round(tbq / tsq, 4) if tsq else (999.0 if tbq else 0.0),
        "iep": _float(_first(source, "iep", "ieP")),
        "rp": _float(source.get("rp")),
        "ieq": _int(source.get("ieq")),
        "iiqTotal": _int(source.get("iiqTotal")),
        "iiqM": _int(source.get("iiqM")),
        "casEligible": _truth(source.get("casEligible")),
        "iv": iv,
        "delta": _float(greeks.get("delta")),
        "theta": _float(greeks.get("theta")),
        "gamma": _float(greeks.get("gamma")),
        "vega": _float(greeks.get("vega")),
        "rho": _float(greeks.get("rho")),
        **depth,
        "market_ohlc": native,
        "feed_kind": "market_full" if market else "index_full" if index else "first_level" if first else "ltpc",
        "has_full_feed": bool(full),
        "raw_fields": sorted(source.keys()),
    }
    return tick


@dataclass
class DecodedFrame:
    type: str | int = "live_feed"
    current_ts: int = 0
    ticks: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    market_info: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _frame_from_dict(payload: dict[str, Any]) -> DecodedFrame:
    feeds = _dict(payload.get("feeds"))
    frame_type = payload.get("type") or "live_feed"
    frame = DecodedFrame(
        type=frame_type if isinstance(frame_type, int) else str(frame_type),
        current_ts=_int(payload.get("currentTs") or payload.get("current_ts")),
        market_info=_dict(payload.get("marketInfo") or payload.get("market_info")),
        raw=payload,
    )
    for key, feed in feeds.items():
        tick = _extract_feed(feed)
        if tick is not None:
            frame.ticks.append((str(key), tick))
    return frame


def decode_frame(raw: bytes | str | dict[str, Any]) -> DecodedFrame:
    """Decode a proto/JSON frame into every available feed field."""
    if isinstance(raw, dict):
        return _frame_from_dict(raw)
    if isinstance(raw, str):
        try:
            return _frame_from_dict(json.loads(raw))
        except (TypeError, ValueError):
            return DecodedFrame(raw={"error": "invalid-json"})
    if isinstance(raw, bytes) and raw.lstrip().startswith((b"{", b"[")):
        try:
            return _frame_from_dict(json.loads(raw.decode("utf-8")))
        except (TypeError, ValueError, UnicodeDecodeError):
            return DecodedFrame(raw={"error": "invalid-json"})
    if pb is None:
        return DecodedFrame(raw={"error": "protobuf-unavailable"})
    try:
        response = pb.FeedResponse()
        response.ParseFromString(raw if isinstance(raw, bytes) else bytes(raw))
        payload = MessageToDict(
            response,
            preserving_proto_field_name=True,
            use_integers_for_enums=True,
        )
        return _frame_from_dict(payload)
    except Exception:  # noqa: BLE001 - malformed broker frame is not a crash
        return DecodedFrame(raw={"error": "invalid-protobuf"})


def decode_full_frame(raw: bytes | str | dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Convenience wrapper used by the tick engine and live session."""
    return decode_frame(raw).ticks


class FullSpectrumBook:
    """Latest full-frame snapshot plus short IV/gamma histories per instrument."""

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = int(capacity)
        self.ticks: dict[str, dict[str, Any]] = {}
        self.iv_history: dict[str, deque[tuple[float, float]]] = {}
        self.gamma_history: dict[str, deque[tuple[float, float]]] = {}
        self._last_identity: dict[str, tuple[int, float]] = {}
        self.frames = 0

    def update(self, key: str, tick: dict[str, Any]) -> dict[str, Any]:
        key = str(key)
        # Identity dedupe: a replayed V3 frame must not append a second IV or
        # gamma sample.  The tuple is the broker's own (key, LTT, VTT) identity.
        identity = (int(_float(tick.get("ltt") or tick.get("ts_ms"))),
                    _float(tick.get("vtt")))
        if identity[0] > 0 and identity[1] > 0 and self._last_identity.get(key) == identity:
            return dict(self.ticks.get(key) or {})
        if identity[0] > 0 and identity[1] > 0:
            self._last_identity[key] = identity
        moment = _float(tick.get("ts_ms") or tick.get("ltt")) / 1000.0
        if moment > 100_000_000_000:
            moment /= 1000.0
        if moment <= 0:
            moment = time.time()
        self.frames += 1
        self.ticks[key] = dict(tick)
        if _float(tick.get("iv")) > 0:
            self.iv_history.setdefault(key, deque(maxlen=self.capacity)).append((moment, _float(tick["iv"])))
        if _float(tick.get("gamma")) != 0:
            self.gamma_history.setdefault(key, deque(maxlen=self.capacity)).append((moment, _float(tick["gamma"])))
        return self.ticks[key]

    def ingest(self, frame: bytes | str | dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        rows = decode_frame(frame).ticks
        return [(key, self.update(key, tick)) for key, tick in rows]

    def get(self, key: str) -> dict[str, Any]:
        return dict(self.ticks.get(key) or {})

    def _velocity(self, history: deque[tuple[float, float]], window: float) -> float:
        if len(history) < 2:
            return 0.0
        latest_t, latest_v = history[-1]
        reference = next((v for t, v in history if latest_t - t <= window), history[0][1])
        if not reference:
            return 0.0
        return round((latest_v / reference - 1.0) * 100.0, 3)

    def iv_velocity(self, key: str, window: float = 300.0) -> float:
        return self._velocity(self.iv_history.get(key) or deque(), window)

    def gamma_velocity(self, key: str, window: float = 300.0) -> float:
        return self._velocity(self.gamma_history.get(key) or deque(), window)


def _expiry_date(value: Any) -> date | None:
    """Parse the master's expiry, which is epoch milliseconds (or ISO in tests)."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        stamp = float(value)
        if stamp > 100_000_000_000:  # milliseconds
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=IST).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def select_front_month_futures(
    rows: Iterable[dict[str, Any]], underlying: str, today: date | None = None
) -> dict[str, Any]:
    """Choose the nearest unexpired futures row from the instruments master."""
    target = str(underlying or "").upper()
    current = today or datetime.now(tz=IST).date()
    candidates: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        row = _dict(row)
        kind = str(row.get("instrument_type") or row.get("instrumentType") or "").upper()
        if kind != "FUT":
            continue
        # ``name`` can be the issuer ("VEDANTA LIMITED"); the index futures
        # carry the index in asset_symbol/underlying_symbol and the trading
        # symbol.  Match those exactly so NIFTYNXT50 never shadows NIFTY.
        name = str(row.get("name") or "").upper()
        asset = str(row.get("asset_symbol") or row.get("underlying_symbol")
                    or row.get("underlying") or "").upper()
        symbol = str(row.get("trading_symbol") or row.get("tradingsymbol") or "").upper()
        if target not in {name, asset} and not symbol.startswith(f"{target} FUT"):
            continue
        expiry = _expiry_date(row.get("expiry"))
        if expiry is None:
            continue
        if expiry >= current:
            candidates.append((expiry, row))
    candidates.sort(key=lambda item: item[0])
    return dict(candidates[0][1]) if candidates else {}


def select_option_window(
    rows: Iterable[dict[str, Any]], atm_strike: float, width: int = 5
) -> list[dict[str, Any]]:
    """Return the ATM +/- ``width`` strike window used by the chain inspector."""
    ordered = sorted(
        (dict(row) for row in rows if _float(row.get("strike_price")) > 0),
        key=lambda row: _float(row.get("strike_price")),
    )
    if not ordered:
        return []
    index = min(range(len(ordered)), key=lambda i: abs(_float(ordered[i].get("strike_price")) - float(atm_strike)))
    start = max(0, index - int(width))
    return ordered[start: start + (2 * int(width) + 1)]


def atm_strike_window(
    rows: Iterable[dict[str, Any]], atm_strike: float, width: int = 5
) -> list[dict[str, Any]]:
    """Canonical ATM +/- width selector; callers must treat len != 11 as degraded."""
    return select_option_window(rows, atm_strike, width=width)


def option_window_keys(rows: Iterable[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for side in ("call_options", "put_options"):
            leg = _dict(row.get(side))
            key = leg.get("instrument_key")
            if key:
                keys.append(str(key))
    return list(dict.fromkeys(keys))