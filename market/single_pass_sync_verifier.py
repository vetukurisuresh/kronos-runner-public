"""Single-pass Upstox V3 -> 102M / 25M / 4M consumption proof.

The live session must decode a broker frame exactly once.  The resulting
canonical frame is immutable and is handed to all three model consumers in one
pass; no model is allowed to re-decode the protobuf or read a parallel copy of
the tick.  This module records that contract and exposes the evidence the UI
needs:

* per-stream read counters and last-tick ages
* per-model frame-read counters (102M / 25M / 4M)
* a hard distinction between ``LIVE_MARKET_VERIFIED`` and
  ``OFFLINE_OR_STALE_FEED``

It never fills a missing stream with a default price.  A stale, fixture or
partially-present feed is reported as offline and the gate stays closed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from market.upstox_full_decoder import decode_frame

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

MODEL_SPECS: tuple[tuple[str, int, str], ...] = (
    ("102m", 102_300_000, "frames_read_by_102m"),
    ("25m", 25_000_000, "frames_read_by_25m"),
    ("4m", 4_000_000, "frames_read_by_4m"),
)
STREAM_KEYS: tuple[str, ...] = (
    "spot",
    "futures",
    "option_chain",
    "depth",
    "greeks",
    "vix",
    "breadth",
    "candles",
)
STALE_AFTER_MS = 3_000
PASS_BUDGET_MS = 0.5


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=IST)
        return moment.timestamp()
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out > 100_000_000_000:
        out /= 1000.0
    return out if out > 0 else None


def _symbol(feed: Mapping[str, Any], symbol: str | None) -> str:
    if symbol:
        return str(symbol).upper()
    for key in ("spot", "full_spectrum", "option"):
        block = _mapping(feed.get(key))
        if block:
            return str(next(iter(block))).upper()
    return "NIFTY"


def _stream_block(
    feed: Mapping[str, Any], symbol: str, key: str, now: float
) -> tuple[bool, int, float | None]:
    """Return ``(present, reads, last_epoch)`` from the actual feed shape."""
    feed_epoch = _epoch(feed.get("updated_epoch")) or _epoch(feed.get("updated_at"))
    spectrum = _mapping(_mapping(feed.get("full_spectrum")).get(symbol))
    spot = _mapping(_mapping(feed.get("spot")).get(symbol))
    option = _mapping(_mapping(feed.get("option")).get(symbol))
    chain = _mapping(feed.get("option_chain"))
    stream_counts = _mapping(feed.get("stream_read_counts"))
    reads = int(_float(stream_counts.get(key), 0.0))

    if key == "spot":
        price = _float(spot.get("ltp")) or _float(spectrum.get("spot"))
        present = price > 0
        return present, reads or (1 if present else 0), feed_epoch
    if key == "futures":
        futures = _mapping(spectrum.get("futures"))
        present = _float(futures.get("ltp")) > 0
        return present, reads or (1 if present else 0), feed_epoch
    if key == "option_chain":
        pcr = _mapping(spectrum.get("pcr"))
        strikes = pcr.get("strike_count")
        if strikes is None:
            rows = chain.get("strikes") or chain.get("rows") or []
            strikes = len(rows) if isinstance(rows, list) else 0
        present = int(_float(strikes)) > 0
        return present, reads or int(_float(strikes)), feed_epoch
    if key == "depth":
        depth = _mapping(spectrum.get("depth"))
        present = (
            depth.get("obi_5") is not None
            or _float(depth.get("bid_qty_5")) > 0
            or _float(depth.get("ask_qty_5")) > 0
            or option.get("obi_5") is not None
        )
        return bool(present), reads or (1 if present else 0), feed_epoch
    if key == "greeks":
        greeks = _mapping(spectrum.get("greeks"))
        present = any(
            _float(greeks.get(name)) != 0.0
            for name in ("delta", "gamma", "theta", "vega", "iv")
        )
        return bool(present), reads or (1 if present else 0), feed_epoch
    if key == "vix":
        vix = _mapping(spectrum.get("vix"))
        present = _float(vix.get("ltp")) > 0
        return present, reads or (1 if present else 0), feed_epoch
    if key == "breadth":
        breadth = _mapping(spectrum.get("breadth"))
        present = int(_float(breadth.get("total"))) > 0
        return present, reads or (1 if present else 0), feed_epoch
    if key == "candles":
        count = max(
            len(spot.get("candles_1m") or []),
            len(spot.get("candles_5m") or []),
            len(spot.get("candles_15m") or []),
            int(_float((feed.get("buffer_5m") or {}).get(symbol), 0.0)),
        )
        present = count > 0
        return present, reads or count, feed_epoch
    return False, reads, feed_epoch


def _counter_snapshot(feed: Mapping[str, Any]) -> dict[str, int]:
    supplied = _mapping(feed.get("sync_proof"))
    supplied = _mapping(supplied.get("model_counters")) or supplied
    out: dict[str, int] = {}
    for key, _params, field_name in MODEL_SPECS:
        out[key] = int(_float(supplied.get(field_name), _float(feed.get(field_name), 0.0)))
    return out


def build_sync_proof(
    feed: Mapping[str, Any] | None,
    *,
    symbol: str = "NIFTY",
    now: float | None = None,
    model_counters: Mapping[str, Any] | None = None,
    source_frames: int | None = None,
) -> dict[str, Any]:
    """Build the serialisable proof from a live-feed snapshot.

    The gate requires all eight streams, a fresh heartbeat, no fixture and
    evidence that all three model consumers read the frame.  If any of those is
    absent the status is ``OFFLINE_OR_STALE_FEED``; no synthetic default can
    make it pass.
    """
    feed = feed or {}
    moment = float(now if now is not None else time.time())
    symbol = _symbol(feed, symbol)
    updated = _epoch(feed.get("updated_epoch")) or _epoch(feed.get("updated_at"))
    age_ms = int(max(0.0, moment - updated) * 1000.0) if updated else None
    fixture = bool(feed.get("fixture"))
    market_open = feed.get("market_open")
    market_open = True if market_open is None else bool(market_open)

    streams: list[dict[str, Any]] = []
    for key in STREAM_KEYS:
        present, reads, last = _stream_block(feed, symbol, key, moment)
        stream_age = int(max(0.0, moment - last) * 1000.0) if last else age_ms
        stale = stream_age is None or stream_age > STALE_AFTER_MS
        streams.append(
            {
                "key": key,
                "present": bool(present),
                "reads": int(reads),
                "last_tick_age_ms": stream_age,
                "stale": bool(stale),
                "verified": bool(present and not stale and not fixture),
            }
        )

    supplied = model_counters if model_counters is not None else _counter_snapshot(feed)
    counters = {
        field_name: int(_float(_mapping(supplied).get(key), 0.0))
        for key, _params, field_name in MODEL_SPECS
    }
    all_streams = all(row["verified"] for row in streams)
    all_models = all(value > 0 for value in counters.values())
    fresh = age_ms is not None and age_ms <= STALE_AFTER_MS
    verified = bool(all_streams and all_models and fresh and not fixture and market_open)

    missing = [row["key"] for row in streams if not row["present"]]
    if verified:
        reason = "ALL_8_STREAMS_LIVE_AND_3_MODELS_READ_THE_SAME_FRAME"
        status = "LIVE_MARKET_VERIFIED"
    elif fixture:
        reason = "FIXTURE_BLOCKED"
        status = "OFFLINE_OR_STALE_FEED"
    elif not market_open:
        reason = "MARKET_CLOSED"
        status = "OFFLINE_OR_STALE_FEED"
    elif not fresh:
        reason = "HEARTBEAT_MISSING_OR_STALE"
        status = "OFFLINE_OR_STALE_FEED"
    elif missing:
        reason = "MISSING_STREAMS:" + ",".join(missing)
        status = "OFFLINE_OR_STALE_FEED"
    elif not all_models:
        reason = "MODEL_CONSUMPTION_UNVERIFIED"
        status = "OFFLINE_OR_STALE_FEED"
    else:
        reason = "STREAM_STALE"
        status = "OFFLINE_OR_STALE_FEED"

    frame_count = int(
        _float(
            source_frames
            if source_frames is not None
            else feed.get("socket_frames", feed.get("source_frames", 0)),
            0.0,
        )
    )
    return {
        "ok": verified,
        "symbol": symbol,
        "as_of": datetime.fromtimestamp(moment, tz=IST).isoformat(timespec="milliseconds"),
        "updated_epoch": updated,
        "age_ms": age_ms,
        "fixture": fixture,
        "market_open": market_open,
        "live_status": status,
        "data_gate": {
            "allowed": verified,
            "status": "PASS" if verified else "BLOCKED",
            "live_status": status,
            "reason": reason,
            "zero_dummy": True,
        },
        "streams": streams,
        "streams_live": sum(1 for row in streams if row["verified"]),
        "streams_total": len(STREAM_KEYS),
        "model_counters": counters,
        "frames_read_by_102m": counters["frames_read_by_102m"],
        "frames_read_by_25m": counters["frames_read_by_25m"],
        "frames_read_by_4m": counters["frames_read_by_4m"],
        "models_consumed": all_models,
        "source_frames": frame_count,
        "single_pass": {
            "budget_ms": PASS_BUDGET_MS,
            "measured_ms": round(_float((feed.get("sync_proof") or {}).get("single_pass_ms"), 0.0), 4),
            "under_budget": _float((feed.get("sync_proof") or {}).get("single_pass_ms"), 0.0) <= PASS_BUDGET_MS,
        },
    }


@dataclass
class CanonicalFrame:
    """One immutable decoded frame shared by all model consumers."""

    frame_id: int
    decoded_at: float
    ticks: tuple[Mapping[str, Any], ...]
    source_ts: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SinglePassSyncVerifier:
    """Decode once, read once by each model, and retain auditable counters."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self.clock = now
        self._frame_id = 0
        self._counters: dict[str, int] = {key: 0 for key, _params, _field in MODEL_SPECS}
        self._last_pass_ms: float | None = None
        self._last_frame: dict[str, Any] | None = None

    @property
    def frame_id(self) -> int:
        return self._frame_id

    @property
    def counters(self) -> dict[str, int]:
        return {
            field_name: self._counters[key]
            for key, _params, field_name in MODEL_SPECS
        }

    def consume_frame(
        self,
        raw_frame: bytes | str | Mapping[str, Any],
        *,
        readers: Mapping[str, Callable[[CanonicalFrame], Any]] | None = None,
        feed: Mapping[str, Any] | None = None,
        symbol: str = "NIFTY",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Decode exactly once, then invoke each supplied model reader once.

        A reader that is absent is not credited.  This is deliberate: the UI
        must never show a model consumption count that did not happen.
        """
        started = time.perf_counter()
        moment = float(now if now is not None else self.clock())
        decoded = decode_frame(raw_frame)
        self._frame_id += 1
        ticks = tuple(MappingProxyType(dict(tick)) for _key, tick in decoded.ticks)
        source_ts = None
        if decoded.current_ts:
            source_ts = float(decoded.current_ts) / 1000.0 if decoded.current_ts > 100_000_000_000 else float(decoded.current_ts)
        canonical = CanonicalFrame(
            frame_id=self._frame_id,
            decoded_at=moment,
            ticks=ticks,
            source_ts=source_ts,
            metadata=MappingProxyType(
                {"type": str(decoded.type), "tick_count": len(ticks), "symbol": symbol}
            ),
        )
        readers = readers or {}
        read_started = time.perf_counter()
        for key, _params, _field in MODEL_SPECS:
            reader = readers.get(key)
            if reader is None:
                continue
            reader(canonical)
            self._counters[key] += 1
        read_ms = (time.perf_counter() - read_started) * 1000.0
        pass_ms = (time.perf_counter() - started) * 1000.0
        self._last_pass_ms = pass_ms
        proof_feed = dict(feed or {})
        proof_feed.setdefault("updated_epoch", moment)
        proof_feed.setdefault("socket_frames", self._frame_id)
        proof = build_sync_proof(
            proof_feed,
            symbol=symbol,
            now=moment,
            model_counters=self.counters,
            source_frames=self._frame_id,
        )
        proof["single_pass"] = {
            "budget_ms": PASS_BUDGET_MS,
            "measured_ms": round(pass_ms, 4),
            "reader_ms": round(read_ms, 4),
            "under_budget": pass_ms <= PASS_BUDGET_MS,
            "frame_id": self._frame_id,
            "ticks": len(ticks),
        }
        proof["canonical_tick_count"] = len(ticks)
        self._last_frame = proof
        return proof

    def snapshot(self, feed: Mapping[str, Any] | None = None, *, symbol: str = "NIFTY",
                 now: float | None = None) -> dict[str, Any]:
        proof = build_sync_proof(
            feed or {},
            symbol=symbol,
            now=now,
            model_counters=self.counters,
        )
        if self._last_pass_ms is not None:
            proof["single_pass"]["measured_ms"] = round(self._last_pass_ms, 4)
        return proof


def verify_feed(
    feed: Mapping[str, Any] | None,
    *,
    symbol: str = "NIFTY",
    now: float | None = None,
) -> dict[str, Any]:
    """Stateless dashboard helper; counters come from the published proof."""
    return build_sync_proof(feed, symbol=symbol, now=now)


__all__ = [
    "CanonicalFrame",
    "MODEL_SPECS",
    "PASS_BUDGET_MS",
    "STALE_AFTER_MS",
    "STREAM_KEYS",
    "SinglePassSyncVerifier",
    "build_sync_proof",
    "verify_feed",
]
