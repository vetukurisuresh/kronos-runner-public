"""Hollow-candle and early-reversal visual classification.

The browser must never guess a candle colour from a screenshot or an arbitrary
threshold.  This module turns the same sanitized OHLC + p10/p90 + order-book
inputs used by the trading engines into an explicit visual contract:

``HOLLOW_EMERALD``  bullish continuation, body unfilled
``FILLED_EMERALD``  higher close than open but weaker than the previous close
``HOLLOW_CORAL``    bearish continuation, body unfilled
``FILLED_CORAL``    lower close than open but stronger than the previous close
``BULL_REVERSAL``   p10 / sweep / OBI absorption candle
``BEAR_REVERSAL``   p90 / OBI flip / seller-wall candle

The classifier is pure, deterministic and fail-closed: without a live feed the
``reversal_armed`` flag is false, so an offline fixture can never paint an
actionable buy/sell candle.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

BULL_BODY = "#10B981"
BEAR_BODY = "#F43F5E"
BULL_REVERSAL_BODY = "#00E5FF"
BULL_REVERSAL_BORDER = "#FFD700"
BEAR_REVERSAL_BODY = "#D946EF"
BEAR_REVERSAL_BORDER = "#F43F5E"
DEFAULT_LIMIT = 180


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _raw(row: Any, names: tuple[str, ...], default: float = 0.0) -> float:
    if isinstance(row, Mapping):
        for name in names:
            if row.get(name) is not None:
                return _f(row.get(name), default)
        return default
    for name in names:
        value = getattr(row, name, None)
        if value is not None:
            return _f(value, default)
    return default


def _time(row: Any) -> int:
    if isinstance(row, Mapping):
        raw = row.get("time", row.get("t", row.get("timestamp", row.get("start"))))
    else:
        raw = getattr(row, "time", getattr(row, "start", getattr(row, "t", None)))
    if raw is None:
        return 0
    if isinstance(raw, str) and not raw.strip().replace(".", "", 1).isdigit():
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return 0
    return int(_f(raw))


def _normalise(rows: Iterable[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        time_value = _time(row)
        open_ = _raw(row, ("open", "o"))
        high = _raw(row, ("high", "h"))
        low = _raw(row, ("low", "l"))
        close = _raw(row, ("close", "c"))
        if time_value <= 0 or min(open_, high, low, close) <= 0 or high < low:
            continue
        out.append({
            "time": time_value, "open": open_, "high": high, "low": low,
            "close": close, "volume": _raw(row, ("volume", "v", "vtt")),
        })
    out.sort(key=lambda item: item["time"])
    deduped: list[dict[str, Any]] = []
    for row in out:
        if deduped and deduped[-1]["time"] == row["time"]:
            deduped[-1] = row
        else:
            deduped.append(row)
    return deduped


def _smc_flags(smc: Mapping[str, Any] | None) -> tuple[bool, bool]:
    if not isinstance(smc, Mapping):
        return False, False
    structure = smc.get("structure") or {}
    sweeps = smc.get("sweeps") or []
    bottom = bool(
        structure.get("bos")
        and str(structure.get("active_bos") or "").upper().startswith("BULL")
    )
    bottom = bottom or any(
        str((row or {}).get("direction") or "").upper() == "SELL_SIDE_SWEEP"
        and bool((row or {}).get("absorbed"))
        for row in sweeps
    )
    top = bool(
        structure.get("choch")
        and str(structure.get("active_choch") or "").upper().startswith("BEAR")
    )
    top = top or any(
        str((row or {}).get("direction") or "").upper() == "BUY_SIDE_SWEEP"
        for row in sweeps
    )
    return bottom, top


def classify_candle_visuals(
    candles: Iterable[Any] | None,
    *,
    p10: Any = None,
    p90: Any = None,
    obi_5: Any = None,
    prev_obi_5: Any = None,
    tbq_tsq: Any = None,
    smc: Mapping[str, Any] | None = None,
    feed_live: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return the visual contract for the most recent ``limit`` candles."""
    rows = _normalise(candles)
    if limit > 0:
        rows = rows[-int(limit):]
    p10_value = _f(p10)
    p90_value = _f(p90)
    obi = _f(obi_5)
    previous_obi = _f(prev_obi_5, obi)
    flow = _f(tbq_tsq)
    smc_bottom, smc_top = _smc_flags(smc)
    action_lines: list[dict[str, Any]] = []
    previous_close = 0.0

    for index, row in enumerate(rows):
        bullish = row["close"] > row["open"]
        continuation = bool(previous_close and row["close"] > previous_close)
        bear_continuation = bool(previous_close and row["close"] < previous_close)
        if bullish and continuation:
            style = "HOLLOW_EMERALD"
            fill = "hollow"
            color = BULL_BODY
            border = BULL_BODY
        elif bullish:
            style = "FILLED_EMERALD"
            fill = "filled"
            color = BULL_BODY
            border = BULL_BODY
        elif bear_continuation:
            style = "HOLLOW_CORAL"
            fill = "hollow"
            color = BEAR_BODY
            border = BEAR_BODY
        else:
            style = "FILLED_CORAL"
            fill = "filled"
            color = BEAR_BODY
            border = BEAR_BODY

        # Reversal evidence is deliberately conjunctive.  A wick through p10
        # alone is not a buy; it needs absorption or an SMC sweep, and a live
        # feed.  The exact same rule is used by the UI and the audit report.
        near_p10 = bool(p10_value > 0 and row["low"] <= p10_value * 1.0005)
        near_p90 = bool(p90_value > 0 and row["high"] >= p90_value * 0.9995)
        bottom_evidence = bool(
            feed_live
            and near_p10
            and bullish
            and (obi >= 1.10 or flow >= 1.20 or smc_bottom)
        )
        top_evidence = bool(
            feed_live
            and near_p90
            and (not bullish or obi < 0.90 or smc_top)
            and (previous_obi < 1.10 or obi < 0.90 or smc_top)
        )
        reversal = ""
        marker = ""
        marker_position = ""
        if bottom_evidence:
            style = "BULL_REVERSAL"
            fill = "glow"
            color = BULL_REVERSAL_BODY
            border = BULL_REVERSAL_BORDER
            reversal = "BULL"
            marker = "▲ EARLY BULL REVERSAL (BUY CE)"
            marker_position = "belowBar"
            action_lines.append({"time": row["time"], "side": "CE", "price": row["low"],
                                 "label": "EARLY BULL REVERSAL"})
        elif top_evidence:
            style = "BEAR_REVERSAL"
            fill = "glow"
            color = BEAR_REVERSAL_BODY
            border = BEAR_REVERSAL_BORDER
            reversal = "BEAR"
            marker = "▼ EARLY PEAK REVERSAL (EXIT / BUY PE)"
            marker_position = "aboveBar"
            action_lines.append({"time": row["time"], "side": "PE", "price": row["high"],
                                 "label": "EARLY PEAK REVERSAL"})

        row.update({
            "index": index,
            "bullish": bullish,
            "style": style,
            "fill": fill,
            "color": color,
            "border_color": border,
            "is_reversal": bool(reversal),
            "reversal": reversal,
            "marker": marker,
            "marker_position": marker_position,
        })
        previous_close = row["close"]

    last = rows[-1] if rows else {}
    reversal_rows = [row for row in rows if row["is_reversal"]]
    return {
        "ok": bool(rows),
        "candles": rows,
        "count": len(rows),
        "p10": p10_value or None,
        "p90": p90_value or None,
        "forecast_corridor": {
            "p10_floor": p10_value or None,
            "median": None,
            "p90_ceiling": p90_value or None,
            "opacity": 0.12,
        },
        "reversal_candles": reversal_rows[-4:],
        "last_reversal": reversal_rows[-1] if reversal_rows else None,
        "bull_reversal_armed": bool(reversal_rows and reversal_rows[-1]["reversal"] == "BULL"),
        "bear_reversal_armed": bool(reversal_rows and reversal_rows[-1]["reversal"] == "BEAR"),
        "action_lines": action_lines[-4:],
        "last": last,
        "feed_live": bool(feed_live),
        "reason": (
            "CANDLE_VISUALS_READY" if rows and feed_live
            else "CANDLE_VISUALS_STALE" if rows
            else "NO_CANDLES"
        ),
    }


__all__ = [
    "BEAR_BODY",
    "BEAR_REVERSAL_BODY",
    "BEAR_REVERSAL_BORDER",
    "BULL_BODY",
    "BULL_REVERSAL_BODY",
    "BULL_REVERSAL_BORDER",
    "classify_candle_visuals",
]
