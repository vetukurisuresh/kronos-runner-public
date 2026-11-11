"""Multi-timeframe confluence for the KRONOS terminal.

The UI can display any interval, but the decision layer always reads all three
official buffers in parallel:

* ``15m`` — macro EMA-20 / VWAP structure
* ``5m``  — Kronos direction plus EMA/VWAP trend
* ``1m``  — execution momentum for the pre-emptive entry

The module is deliberately pure: it accepts ``Candle`` objects or the dicts
published in ``state/live_feed.json`` and never touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"
MIXED = "MIXED"
EMA_PERIOD = 20
MIN_BARS = 3
BULL_ICON = "\U0001F7E2"
BEAR_ICON = "\U0001F534"
NEUTRAL_ICON = "\U0001F7E1"


def _value(candle: Any, names: tuple[str, ...], default: float = 0.0) -> float:
    for name in names:
        if isinstance(candle, dict) and candle.get(name) is not None:
            try:
                return float(candle[name])
            except (TypeError, ValueError):
                pass
        if hasattr(candle, name):
            try:
                return float(getattr(candle, name))
            except (TypeError, ValueError):
                pass
    return default


def _rows(candles: Iterable[Any] | None) -> list[Any]:
    return [row for row in (candles or []) if row is not None]


def closes(candles: Iterable[Any] | None) -> list[float]:
    return [_value(row, ("close", "c")) for row in _rows(candles)]


def highs(candles: Iterable[Any] | None) -> list[float]:
    return [_value(row, ("high", "h")) for row in _rows(candles)]


def lows(candles: Iterable[Any] | None) -> list[float]:
    return [_value(row, ("low", "l")) for row in _rows(candles)]


def volumes(candles: Iterable[Any] | None) -> list[float]:
    return [_value(row, ("volume", "v")) for row in _rows(candles)]


def ema(values: Iterable[float], period: int = EMA_PERIOD) -> float:
    """Final EMA value; returns 0 for an empty series."""
    series = [float(value) for value in values if value is not None]
    if not series:
        return 0.0
    period = max(1, int(period))
    alpha = 2.0 / (period + 1.0)
    value = series[0]
    for price in series[1:]:
        value = alpha * price + (1.0 - alpha) * value
    return round(value, 4)


def vwap(candles: Iterable[Any] | None) -> float:
    rows = _rows(candles)
    total_pv = 0.0
    total_volume = 0.0
    for row in rows:
        high = _value(row, ("high", "h"))
        low = _value(row, ("low", "l"))
        close = _value(row, ("close", "c"))
        volume = max(_value(row, ("volume", "v")), 1.0)
        total_pv += ((high + low + close) / 3.0) * volume
        total_volume += volume
    if total_volume <= 0:
        return 0.0
    return round(total_pv / total_volume, 4)


def _trend(close: float, avg: float, vwap_value: float) -> str:
    if close <= 0 or avg <= 0:
        return NEUTRAL
    above_ema = close >= avg
    above_vwap = vwap_value <= 0 or close >= vwap_value
    if above_ema and above_vwap:
        return BULL
    if not above_ema and not above_vwap:
        return BEAR
    return NEUTRAL


def analyze_timeframe(
    candles: Iterable[Any] | None,
    *,
    label: str = "",
    forecast_up_pct: float | None = None,
) -> dict[str, Any]:
    """Classify one interval and expose the exact values used in the pill."""
    rows = _rows(candles)
    close_series = closes(rows)
    close = close_series[-1] if close_series else 0.0
    avg = ema(close_series[-EMA_PERIOD:])
    vwap_value = vwap(rows)
    trend = _trend(close, avg, vwap_value)
    momentum = 0.0
    if len(close_series) >= MIN_BARS and close_series[-MIN_BARS]:
        momentum = (close / close_series[-MIN_BARS] - 1.0) * 100.0
    if forecast_up_pct is not None:
        if forecast_up_pct >= 65:
            trend = BULL
        elif forecast_up_pct <= 35:
            trend = BEAR
        elif label == "5m":
            trend = NEUTRAL
    icon = BULL_ICON if trend == BULL else BEAR_ICON if trend == BEAR else NEUTRAL_ICON
    return {
        "label": label,
        "trend": trend,
        "icon": icon,
        "close": round(close, 2),
        "ema20": round(avg, 2),
        "vwap": round(vwap_value, 2),
        "momentum_pct": round(momentum, 3),
        "bars": len(rows),
        "forecast_up_pct": round(forecast_up_pct, 1) if forecast_up_pct is not None else None,
    }


def analyze(
    candles_1m: Iterable[Any] | None,
    candles_5m: Iterable[Any] | None,
    candles_15m: Iterable[Any] | None,
    *,
    paths_up: int | None = None,
    samples: int | None = None,
) -> dict[str, Any]:
    """Return alignment, score and the exact on-chart ``MTF SYNC`` pill."""
    forecast_pct = (
        (float(paths_up) / float(samples) * 100.0)
        if paths_up is not None and samples
        else None
    )
    frames = {
        "1m": analyze_timeframe(candles_1m, label="1m"),
        "5m": analyze_timeframe(candles_5m, label="5m", forecast_up_pct=forecast_pct),
        "15m": analyze_timeframe(candles_15m, label="15m"),
    }
    directions = [frames[key]["trend"] for key in ("1m", "5m", "15m")]
    if all(direction == BULL for direction in directions):
        alignment = BULL
    elif all(direction == BEAR for direction in directions):
        alignment = BEAR
    elif any(direction == NEUTRAL for direction in directions) and len(set(directions)) == 1:
        alignment = NEUTRAL
    else:
        alignment = MIXED
    bull = sum(1 for direction in directions if direction == BULL)
    bear = sum(1 for direction in directions if direction == BEAR)
    aligned = alignment in (BULL, BEAR)
    icon = BULL_ICON if alignment == BULL else BEAR_ICON if alignment == BEAR else NEUTRAL_ICON
    pill = "MTF SYNC: " + " ".join(
        f"[{key}: {frames[key]['icon']} {frames[key]['trend']}]"
        for key in ("1m", "5m", "15m")
    )
    return {
        "alignment": alignment,
        "aligned": aligned,
        "bull": bull,
        "bear": bear,
        "score": bull - bear,
        "icon": icon,
        "pill": pill,
        "frames": frames,
        "forecast_up_pct": round(forecast_pct, 1) if forecast_pct is not None else None,
        "actionable": aligned,
        "side": "CE" if alignment == BULL else "PE" if alignment == BEAR else "",
    }
