"""Automatic scalping-versus-directional regime switch.

The switch is a pure 0-100 measurement of market speed and trendiness.  It does
not overrule the 2-of-3 quorum; it only decides *how* an approved trade is
managed:

* ``TRENDY_DIRECTIONAL_HOLD`` -- 40% T1, 30% T2, 30% runner and a +Rs1.50 stop.
* ``HIGH_ACCURACY_SCALPING`` -- 70% T1 at an 8-12% option pop, stop to cost,
  30% T2 so a range reversal cannot give the profit back.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

TREND_SCORE_THRESHOLD = 65.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _bars(rows: Any) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        t = _f(raw.get("t") if raw.get("t") is not None else raw.get("time"))
        o = _f(raw.get("o") if raw.get("o") is not None else raw.get("open"))
        h = _f(raw.get("h") if raw.get("h") is not None else raw.get("high"))
        l = _f(raw.get("l") if raw.get("l") is not None else raw.get("low"))
        c = _f(raw.get("c") if raw.get("c") is not None else raw.get("close"))
        if t > 0 and c > 0:
            out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    return out


def _ema(values: list[float], period: int = 9) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _atr(bars: list[dict[str, float]], period: int = 14) -> float:
    window = bars[-period - 1:]
    if len(window) < 2:
        return 0.0
    ranges: list[float] = []
    for prev, cur in zip(window, window[1:]):
        ranges.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    return sum(ranges) / max(1, len(ranges))


def evaluate_market_regime(
    feed: Mapping[str, Any],
    *,
    symbol: str = "NIFTY",
    quorum: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the live speed/trend score and the exact execution policy."""
    feed = feed or {}
    quorum = quorum or {}
    spots = feed.get("spot") or {}
    spot_block = spots.get(symbol) if isinstance(spots, Mapping) else {}
    spot_block = spot_block if isinstance(spot_block, Mapping) else {}
    bars = _bars(spot_block.get("candles_5m"))
    current_raw = spot_block.get("current_candle_5m")
    current = _bars([current_raw])[0] if isinstance(current_raw, Mapping) else {}
    if current:
        bars = [bar for bar in bars if bar["t"] != current["t"]] + [current]
    tick_rate = _f(feed.get("tick_rate"), 0.0)
    if tick_rate <= 0:
        frames = _f(feed.get("socket_frames"), 0.0)
        tick_rate = min(25.0, frames / 240.0) if frames > 0 else 0.0

    closes = [bar["c"] for bar in bars[-40:]]
    ema_now = _ema(closes, 9)
    ema_prev = _ema(closes[:-3], 9) if len(closes) > 3 else ema_now
    atr = _atr(bars, 14)
    body = abs(current.get("c", 0.0) - current.get("o", 0.0)) if current else 0.0
    body_atr = body / atr if atr > 0 else 0.0
    slope_points = ema_now - ema_prev
    slope_atr = abs(slope_points) / atr if atr > 0 else 0.0

    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    futures = spectrum.get("futures") or {}
    fut_ltp = _f(futures.get("ltp"))
    fut_atp = _f(futures.get("atp"))
    futures_move_pct = abs(fut_ltp - fut_atp) / fut_atp * 100.0 if fut_atp > 0 else 0.0
    futures_dvtt = futures_move_pct

    oscillator = feed.get("model_oscillator") or {}
    bull = _f(oscillator.get("bullish_upside_prob_pct"), 50.0)
    bear = _f(oscillator.get("bearish_downside_prob_pct"), 50.0)
    skew = abs(bull - bear) / 100.0
    if not quorum and oscillator:
        quorum = {"side": "CE" if bull > bear else "PE", "confidence": max(bull, bear)}

    speed_component = _clamp(tick_rate / 10.0)
    body_component = _clamp(body_atr / 1.2)
    slope_component = _clamp(slope_atr / 0.8)
    futures_component = _clamp(futures_move_pct / 0.6)
    skew_component = _clamp(skew)
    trend_score = 100.0 * (
        0.20 * speed_component
        + 0.25 * body_component
        + 0.25 * slope_component
        + 0.15 * futures_component
        + 0.15 * skew_component
    )
    trend_score = round(_clamp(trend_score, 0.0, 100.0), 1)
    trendy = trend_score >= TREND_SCORE_THRESHOLD
    side = str(quorum.get("side") or ("CE" if slope_points > 0 else "PE")).upper()
    mode = "TRENDY_DIRECTIONAL_HOLD" if trendy else "HIGH_ACCURACY_SCALPING"
    label = "🚀 TRENDY DIRECTIONAL MARKET" if trendy else "⚡ FAST SCALPING MARKET"
    badge = (
        f"🔄 AUTO REGIME: {label} "
        f"(Speed {tick_rate:.1f} ticks/s · ATR {body_atr:.1f}x)"
    )
    plan = (
        {
            "t1_book_fraction": 0.40,
            "t2_book_fraction": 0.30,
            "runner_fraction": 0.30,
            "t1_option_pct": None,
            "stop_to": "ENTRY_PLUS_1_50",
            "runner_hold": "5m EMA9 + OBI_5 >= 0.80",
            "runner_hold_obi": 0.80,
            "hold_style": "MULTI_CANDLE_DIRECTIONAL_HOLD",
        }
        if trendy else
        {
            "t1_book_fraction": 0.70,
            "t2_book_fraction": 0.30,
            "runner_fraction": 0.00,
            "t1_option_pct": [8.0, 12.0],
            "stop_to": "COST",
            "runner_hold": "NONE",
            "runner_hold_obi": 0.70,
            "hold_style": "QUICK_MOMENTUM_BURST",
        }
    )
    return {
        "ok": True,
        "symbol": symbol,
        "score": trend_score,
        "threshold": TREND_SCORE_THRESHOLD,
        "mode": mode,
        "mode_label": label,
        "badge": badge,
        "speed_ticks_per_sec": round(tick_rate, 2),
        "body_atr_ratio": round(body_atr, 3),
        "ema9_slope_points": round(slope_points, 3),
        "ema9_slope_atr": round(slope_atr, 3),
        "futures_dvtt_pct": round(futures_dvtt, 3),
        "directional_skew_pct": round(skew * 100.0, 1),
        "quorum_side": side,
        "quorum_confidence": round(_f(quorum.get("confidence")), 2),
        "book_plan": plan,
        "reason_te": (
            "మార్కెట్ వేగం మరియు ట్రెండ్ బలం ఎక్కువగా ఉన్నాయి; లాభాలను ట్రెండ్ వెంట పరుగెత్తించండి."
            if trendy else
            "మార్కెట్ రేంజ్‌లో ఉంది; T1 వద్ద ఎక్కువ లాట్లు బుక్ చేసి స్టాప్‌ను కాస్ట్‌కు తగ్గించండి."
        ),
        "reason_en": (
            "High speed and trendiness: hold the runner across 5m candles."
            if trendy else
            "Range conditions: bank 70% at T1, move the stop to cost and harvest T2."
        ),
        "components": {
            "speed": round(speed_component, 3),
            "body_atr": round(body_component, 3),
            "ema9_slope": round(slope_component, 3),
            "futures_dvtt": round(futures_component, 3),
            "directional_skew": round(skew_component, 3),
        },
    }


__all__ = ["TREND_SCORE_THRESHOLD", "evaluate_market_regime"]
