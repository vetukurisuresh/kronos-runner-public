"""30-minute market speed and institutional whale-price footprints.

The day is split into the 13 NSE cash-session windows from 09:15 to 15:30 IST.
Every observed print contributes volume to a 5-point NIFTY bucket (and to the
active option strike bucket) with a weight built from actual futures DVTT,
5-level OBI absorption and OI change.  A window with no verified prints has no
whale zone; it is never filled with a default price.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)
BUCKET_SIZE = 5
SESSION_START_MINUTE = 9 * 60 + 15
SESSION_END_MINUTE = 15 * 60 + 30
# The final bucket is the closing 15-minute tail (15:15–15:30), not a fabricated
# 15:45 close.  Every other bucket is a full 30-minute institutional window.
WINDOWS: tuple[tuple[int, int, str], ...] = tuple(
    (
        SESSION_START_MINUTE + i * 30,
        min(SESSION_START_MINUTE + (i + 1) * 30, SESSION_END_MINUTE),
        f"{(SESSION_START_MINUTE + i * 30) // 60:02d}:{(SESSION_START_MINUTE + i * 30) % 60:02d}–"
        f"{min(SESSION_START_MINUTE + (i + 1) * 30, SESSION_END_MINUTE) // 60:02d}:"
        f"{min(SESSION_START_MINUTE + (i + 1) * 30, SESSION_END_MINUTE) % 60:02d}",
    )
    for i in range(13)
)


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


def as_ist(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(tz=IST)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(tz=IST)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    try:
        return datetime.fromtimestamp(float(value), tz=IST)
    except (TypeError, ValueError, OSError):
        return datetime.now(tz=IST)


def minutes_of(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def window_index(moment: datetime | float | str | None = None) -> int | None:
    """Zero-based 30-minute window index, or None outside the cash session."""
    value = as_ist(moment)
    minute = minutes_of(value)
    if minute < 9 * 60 + 15 or minute > 15 * 60 + 30:
        return None
    return min(12, (minute - (9 * 60 + 15)) // 30)


def window_label(index: int) -> str:
    return WINDOWS[max(0, min(12, int(index)))][2]


def bucket_price(price: float) -> float:
    return math.floor(float(price) / BUCKET_SIZE) * BUCKET_SIZE


def _regime(
    *,
    tick_count: int,
    first_price: float,
    last_price: float,
    low: float,
    high: float,
    atr: float,
    obi: float,
    top_bucket: Mapping[str, Any] | None,
    top_option: Mapping[str, Any] | None,
) -> str:
    if tick_count <= 0 or first_price <= 0 or last_price <= 0:
        return "AWAITING_VERIFIED_FLOW"
    span = max(0.0, high - low)
    move = abs(last_price - first_price)
    if span >= max(atr * 1.8, 18.0) and move >= max(atr, 7.0):
        return "HIGH_VELOCITY_TREND"
    if top_bucket and obi >= 1.12 and last_price >= low:
        return "INSTITUTIONAL_ACCUMULATION"
    if span <= max(atr * 0.9, 7.0):
        return "CHOPPY_COMPRESSION"
    if top_bucket and top_option and move >= span * 0.55:
        return "REVERSAL_SWEEP"
    return "CHOPPY_COMPRESSION"


@dataclass
class WindowState:
    index: int
    prices: list[float] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    buckets: dict[float, dict[str, float]] = field(default_factory=lambda: defaultdict(lambda: {
        "volume": 0.0, "weight": 0.0, "oi_delta": 0.0, "futures_ratio": 0.0,
        "obi": 0.0, "tbq_tsq": 0.0, "prints": 0.0,
    }))
    option_buckets: dict[str, dict[str, Any]] = field(default_factory=dict)
    futures_ratios: list[float] = field(default_factory=list)
    obis: list[float] = field(default_factory=list)
    tbqs: list[float] = field(default_factory=list)
    vix_values: list[float] = field(default_factory=list)
    atr_values: list[float] = field(default_factory=list)
    oi_values: list[float] = field(default_factory=list)
    tick_count: int = 0
    reported_tick_rate: float = 0.0
    snapshot_low: float | None = None
    snapshot_high: float | None = None
    first_epoch: float | None = None
    last_epoch: float | None = None

    def add_tick(
        self,
        *,
        price: float,
        volume: float,
        ts: float,
        futures_ratio: float,
        obi: float,
        tbq_tsq: float,
        oi_delta: float,
        vix: float,
        atr: float,
        option_strike: str | None = None,
        option_volume: float = 0.0,
    ) -> None:
        self.tick_count += 1
        self.prices.append(price)
        self.timestamps.append(ts)
        self.volumes.append(max(0.0, volume))
        self.futures_ratios.append(max(0.0, futures_ratio))
        self.obis.append(obi)
        self.tbqs.append(tbq_tsq)
        if vix > 0:
            self.vix_values.append(vix)
        if atr > 0:
            self.atr_values.append(atr)
        if oi_delta:
            self.oi_values.append(oi_delta)
        self.first_epoch = ts if self.first_epoch is None else min(self.first_epoch, ts)
        self.last_epoch = ts if self.last_epoch is None else max(self.last_epoch, ts)
        if volume > 0:
            key = bucket_price(price)
            bucket = self.buckets[key]
            bucket["volume"] += volume
            bucket["weight"] += volume * max(1.0, futures_ratio) * max(0.25, obi)
            bucket["futures_ratio"] = max(bucket["futures_ratio"], futures_ratio)
            bucket["obi"] = obi
            bucket["tbq_tsq"] = tbq_tsq
            bucket["oi_delta"] += oi_delta
            bucket["prints"] += 1
            if option_strike and option_volume > 0:
                option = self.option_buckets.setdefault(
                    str(option_strike),
                    {"volume": 0.0, "weight": 0.0, "prints": 0.0},
                )
                option["volume"] += option_volume
                option["weight"] += option_volume * max(1.0, futures_ratio) * max(0.25, obi)
                option["prints"] += 1

    def observe_snapshot(self, *, price: float, low: float, high: float, tick_rate: float,
                         vix: float, atr: float) -> None:
        """Attach real broker snapshot metrics without inventing a volume print."""
        if price > 0:
            self.prices.append(price)
        if low > 0:
            self.snapshot_low = low if self.snapshot_low is None else min(self.snapshot_low, low)
        if high > 0:
            self.snapshot_high = high if self.snapshot_high is None else max(self.snapshot_high, high)
        if tick_rate > 0:
            self.reported_tick_rate = max(self.reported_tick_rate, tick_rate)
        if vix > 0:
            self.vix_values.append(vix)
        if atr > 0:
            self.atr_values.append(atr)

    def _top_bucket(self) -> tuple[float, dict[str, Any]] | None:
        if not self.buckets:
            return None
        key, row = max(self.buckets.items(), key=lambda item: (item[1]["weight"], item[1]["volume"]))
        return key, row

    def _top_option(self) -> tuple[str, dict[str, Any]] | None:
        if not self.option_buckets:
            return None
        key, row = max(self.option_buckets.items(), key=lambda item: (item[1]["weight"], item[1]["volume"]))
        return key, row

    def metrics(self) -> dict[str, Any]:
        prices = [p for p in self.prices if p > 0]
        first = prices[0] if prices else 0.0
        last = prices[-1] if prices else 0.0
        low = min([*prices, self.snapshot_low or 0.0]) if (prices or self.snapshot_low) else 0.0
        high = max([*prices, self.snapshot_high or 0.0]) if (prices or self.snapshot_high) else 0.0
        if self.first_epoch and self.last_epoch:
            elapsed = max(1.0, self.last_epoch - self.first_epoch)
            ticks_per_sec = self.tick_count / elapsed
        else:
            ticks_per_sec = self.reported_tick_rate
        atr = self.atr_values[-1] if self.atr_values else 0.0
        vix_delta = (self.vix_values[-1] - self.vix_values[0]) if len(self.vix_values) >= 2 else 0.0
        top = self._top_bucket()
        top_option = self._top_option()
        obi = self.obis[-1] if self.obis else 0.0
        if top is None and self.buckets:
            top = self._top_bucket()
        regime = _regime(
            tick_count=self.tick_count, first_price=first, last_price=last,
            low=low or first, high=high or last, atr=atr, obi=obi,
            top_bucket=top[1] if top else None,
            top_option=top_option[1] if top_option else None,
        )
        return {
            "tick_count": self.tick_count,
            "avg_ticks_per_sec": round(ticks_per_sec, 2),
            "spot_range_pts": round(max(0.0, high - low), 2) if prices else None,
            "option_atr": round(atr, 2) if atr > 0 else None,
            "vix_delta": round(vix_delta, 2) if len(self.vix_values) >= 2 else None,
            "regime": regime,
            "first_price": round(first, 2) if first else None,
            "last_price": round(last, 2) if last else None,
            "low": round(low, 2) if low else None,
            "high": round(high, 2) if high else None,
            "obi_5": round(obi, 3) if self.obis else None,
        }

    def whale_zone(self) -> dict[str, Any] | None:
        top = self._top_bucket()
        if top is None:
            return None
        key, row = top
        low, high = key, key + BUCKET_SIZE
        option = self._top_option()
        oi_delta = _float(row.get("oi_delta"))
        obi = _float(row.get("obi"))
        futures_ratio = _float(row.get("futures_ratio"))
        bias = "Strong Bullish Base" if oi_delta >= 0 and obi >= 1.0 else (
            "Strong Bearish Supply" if oi_delta < 0 and obi < 1.0 else "Absorption / Two-Way"
        )
        option_text = ""
        if option:
            option_text = f", {option[0]} OI active"
        text = (
            f"NIFTY {low:,.0f}–{high:,.0f} · Futures Vol {futures_ratio:.1f}x · "
            f"OI Δ {oi_delta:+.0f} · OBI {obi:.2f}{option_text} ➔ {bias}"
        )
        text_te = (
            f"🐋 పెద్ద ప్లేయర్ల ఎంట్రీ ధర: NIFTY {low:,.0f}–{high:,.0f} "
            f"(Futures Vol {futures_ratio:.1f}x, OI Δ {oi_delta:+.0f}, OBI {obi:.2f}) "
            f"➔ {bias}"
        )
        return {
            "price_low": low,
            "price_high": high,
            "bucket_label": f"NIFTY {low:,.0f}–{high:,.0f}",
            "weighted_volume": round(_float(row.get("weight")), 2),
            "raw_volume": round(_float(row.get("volume")), 2),
            "futures_volume_ratio": round(futures_ratio, 2),
            "oi_delta": round(oi_delta, 2),
            "obi_5": round(obi, 3),
            "tbq_tsq": round(_float(row.get("tbq_tsq")), 2),
            "prints": int(_float(row.get("prints"))),
            "option_strike": option[0] if option else None,
            "option_volume": round(_float(option[1].get("volume")), 2) if option else None,
            "bias": bias,
            "text": text,
            "text_te": text_te,
        }


class InstitutionalFootprintTracker:
    """In-memory tracker; the dashboard/runner can serialise snapshots freely."""

    def __init__(self) -> None:
        self._days: dict[str, dict[int, WindowState]] = defaultdict(dict)
        self._last_marker: dict[str, str] = {}

    def reset(self) -> None:
        self._days.clear()
        self._last_marker.clear()

    def _state(self, day: str, index: int) -> WindowState:
        return self._days[day].setdefault(index, WindowState(index=index))

    def add_tick(
        self,
        price: Any,
        volume: Any = 0.0,
        *,
        ts: Any = None,
        symbol: str = "NIFTY",
        futures_ratio: Any = 1.0,
        obi_5: Any = 1.0,
        tbq_tsq: Any = 1.0,
        oi_delta: Any = 0.0,
        vix: Any = 0.0,
        option_atr: Any = 0.0,
        option_strike: str | None = None,
        option_volume: Any = 0.0,
    ) -> dict[str, Any] | None:
        moment = as_ist(ts)
        index = window_index(moment)
        if index is None:
            return None
        day = moment.strftime("%Y-%m-%d")
        state = self._state(day, index)
        state.add_tick(
            price=_float(price), volume=_float(volume), ts=moment.timestamp(),
            futures_ratio=_float(futures_ratio, 1.0), obi=_float(obi_5, 1.0),
            tbq_tsq=_float(tbq_tsq, 1.0), oi_delta=_float(oi_delta),
            vix=_float(vix), atr=_float(option_atr),
            option_strike=option_strike, option_volume=_float(option_volume),
        )
        return state.metrics()

    def observe_feed(
        self,
        feed: Mapping[str, Any] | None,
        *,
        symbol: str = "NIFTY",
        now: Any = None,
    ) -> list[dict[str, Any]]:
        """Consume actual tick_tape rows, or attach a real broker snapshot."""
        feed = feed or {}
        moment = as_ist(now)
        spectrum = _mapping(_mapping(feed.get("full_spectrum")).get(symbol))
        spot = _mapping(_mapping(feed.get("spot")).get(symbol))
        tape = feed.get("tick_tape") or feed.get("verified_ticks") or []
        marker = f"{feed.get('socket_frames')}:{feed.get('updated_epoch')}:{len(tape) if isinstance(tape, list) else 0}"
        price = _float(spot.get("ltp")) or _float(spectrum.get("spot"))
        tick_rate = _float(feed.get("tick_rate"))
        current = _mapping(spot.get("current_candle"))
        futures = _mapping(spectrum.get("futures"))
        depth = _mapping(spectrum.get("depth"))
        greeks = _mapping(spectrum.get("greeks"))
        vix = _float(_mapping(spectrum.get("vix")).get("ltp"))
        atr = _float(spot.get("atr")) or _float(_mapping(spectrum.get("option")).get("atr"))
        index = window_index(moment)
        if index is None:
            return self.snapshot(moment.strftime("%Y-%m-%d"), now=now)
        state = self._state(moment.strftime("%Y-%m-%d"), index)
        if self._last_marker.get(symbol) != marker:
            self._last_marker[symbol] = marker
            if isinstance(tape, list) and tape:
                for row in tape[-5000:]:
                    row = _mapping(row)
                    self.add_tick(
                        row.get("price") or row.get("ltp"),
                        row.get("volume") or row.get("vtt") or 0.0,
                        ts=row.get("ts") or row.get("ltt") or moment,
                        symbol=symbol,
                        futures_ratio=row.get("futures_ratio") or futures.get("vtt_ratio") or 1.0,
                        obi_5=row.get("obi_5") or depth.get("obi_5") or 1.0,
                        tbq_tsq=row.get("tbq_tsq") or depth.get("tbq_tsq") or 1.0,
                        oi_delta=row.get("oi_delta") or 0.0,
                        vix=row.get("vix") or vix,
                        option_atr=row.get("option_atr") or atr,
                        option_strike=row.get("option_strike"),
                        option_volume=row.get("option_volume") or 0.0,
                    )
            elif price > 0:
                state.observe_snapshot(
                    price=price,
                    low=_float(current.get("low")),
                    high=_float(current.get("high")),
                    tick_rate=tick_rate, vix=vix, atr=atr,
                )
        return self.snapshot(moment.strftime("%Y-%m-%d"), now=now)

    def snapshot(self, date: str, *, now: Any = None) -> list[dict[str, Any]]:
        day = as_ist(date).strftime("%Y-%m-%d") if not isinstance(date, str) else date[:10]
        moment = as_ist(now)
        today = moment.strftime("%Y-%m-%d")
        day_state = self._days.get(day, {})
        rows: list[dict[str, Any]] = []
        for index, (start_minute, end_minute, label) in enumerate(WINDOWS):
            state = day_state.get(index)
            if state:
                metrics = state.metrics()
                whale = state.whale_zone()
            else:
                metrics = {
                    "tick_count": 0, "avg_ticks_per_sec": 0.0, "spot_range_pts": None,
                    "option_atr": None, "vix_delta": None,
                    "regime": "AWAITING_VERIFIED_FLOW", "first_price": None,
                    "last_price": None, "low": None, "high": None, "obi_5": None,
                }
                whale = None
            if day == today and start_minute <= minutes_of(moment) < end_minute:
                status = "LIVE"
            elif day == today and minutes_of(moment) < start_minute:
                status = "UPCOMING"
            else:
                status = "COMPLETED"
            rows.append({
                "index": index,
                "window": label,
                "start_ist": f"{start_minute // 60:02d}:{start_minute % 60:02d}",
                "end_ist": f"{end_minute // 60:02d}:{end_minute % 60:02d}",
                "status": status,
                "metrics": metrics,
                "whale_zone": whale,
            })
        return rows


__all__ = [
    "BUCKET_SIZE",
    "InstitutionalFootprintTracker",
    "SESSION_END",
    "SESSION_START",
    "WINDOWS",
    "WindowState",
    "as_ist",
    "bucket_price",
    "minutes_of",
    "window_index",
    "window_label",
]
