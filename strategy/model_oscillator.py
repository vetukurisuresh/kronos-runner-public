"""Bull/bear confirmation-border and data-vs-model oscillator.

The oscillator is an advisory fusion layer, not a substitute for the
fail-closed full-spectrum gate.  It fuses the three model roles with the exact
weights requested by the desk:

* Kronos-base 102M upside paths: 40%
* Kronos-small 25M option-chain/regime: 25%
* Kronos-mini 4M order-book/execution: 35%

When a model input is absent the missing leg is neutral and the payload is
labelled ``DEGRADED``; a missing feed can therefore never cross a trade border
from fabricated confidence alone.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MODEL_WEIGHTS = {"base": 0.40, "small": 0.25, "mini": 0.35}
SNIPER_BORDER_PCT = 78.0
SCALP_BORDER_PCT = 72.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _score(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return _clamp(_f(value))


def _score_from_paths(paths: Any, samples: Any) -> float | None:
    up, total = _f(paths), _f(samples)
    if total <= 0:
        return None
    return _clamp(up / total * 100.0)


def _small_from_regime(
    pcr: Any,
    vix_change_pct: Any,
    breadth_aligned: Any,
    breadth_total: Any,
    trend_slope: Any,
) -> float | None:
    if pcr is None and vix_change_pct is None and breadth_total is None and trend_slope is None:
        return None
    score = 50.0
    if pcr is not None:
        score += (_f(pcr, 1.0) - 1.0) * 30.0
    if vix_change_pct is not None:
        score += -4.0 if _f(vix_change_pct) > 0 else 4.0
    total = _f(breadth_total)
    if total > 0:
        score += ((_f(breadth_aligned) / total) - 0.50) * 24.0
    score += _clamp(_f(trend_slope) * 2.0, -8.0, 8.0)
    return _clamp(score)


def _mini_from_flow(
    obi_5: Any,
    tbq_tsq: Any,
    futures_volume_ratio: Any,
    trend_slope: Any,
) -> float | None:
    if obi_5 is None and tbq_tsq is None and futures_volume_ratio is None and trend_slope is None:
        return None
    score = 50.0
    if obi_5 is not None:
        score += (_f(obi_5, 1.0) - 1.0) * 22.0
    if tbq_tsq is not None:
        score += (_f(tbq_tsq, 1.0) - 1.0) * 24.0
    if futures_volume_ratio is not None:
        score += (_f(futures_volume_ratio, 1.0) - 1.0) * 8.0
    score += _clamp(_f(trend_slope) * 2.0, -8.0, 8.0)
    return _clamp(score)


def _border_te(bull: float, bear: float, threshold: float) -> str:
    if bull >= threshold:
        return (
            f"🔥 బుల్లిష్ బార్డర్ దాటింది ({bull:.1f}% Upside Chance)! "
            "3 మోడల్స్ గ్రీన్ సిగ్నల్ ఇచ్చాయి!"
        )
    if bear >= threshold:
        return (
            f"🔥 బేరిష్ బార్డర్ దాటింది ({bear:.1f}% Downside Chance)! "
            "PE ట్రేడ్ కన్ఫర్మ్ అయింది!"
        )
    if bull >= 70.0:
        return (
            f"⚡ బుల్లిష్ కన్ఫర్మేషన్ బార్డర్ ({threshold:.0f}%) దగ్గరికి వస్తోంది "
            f"(ప్రస్తుతం {bull:.1f}%) — CE ట్రేడ్ కోసం సిద్ధంగా ఉండండి!"
        )
    if bear >= 70.0:
        return (
            f"⚡ బేరిష్ కన్ఫర్మేషన్ బార్డర్ ({threshold:.0f}%) దగ్గరికి వస్తోంది "
            f"(ప్రస్తుతం {bear:.1f}%) — PE ట్రేడ్ కోసం సిద్ధంగా ఉండండి!"
        )
    return (
        f"⏳ బార్డర్ కోసం వెయిట్ చేస్తోంది · Upside {bull:.1f}% / "
        f"Downside {bear:.1f}% · ట్రిగ్గర్ {threshold:.0f}%"
    )


def _war_state(flow_force: float, model_force: float) -> str:
    if flow_force > 12 and model_force > 12:
        return "AGREE_BULL"
    if flow_force < -12 and model_force < -12:
        return "AGREE_BEAR"
    if flow_force > 12 and model_force < -12:
        return "FLOW_FIGHTS_MODEL"
    if flow_force < -12 and model_force > 12:
        return "MODEL_FIGHTS_FLOW"
    return "BALANCED_WAIT"


def build_model_oscillator(
    *,
    base_upside_pct: Any = None,
    base_paths_up: Any = None,
    base_samples: Any = None,
    small_probability_pct: Any = None,
    pcr: Any = None,
    vix_change_pct: Any = None,
    breadth_aligned: Any = None,
    breadth_total: Any = None,
    mini_probability_pct: Any = None,
    mini_paths_up: Any = None,
    mini_samples: Any = None,
    obi_5: Any = None,
    tbq_tsq: Any = None,
    futures_volume_ratio: Any = None,
    trend_slope: Any = None,
    border_threshold_pct: Any = SNIPER_BORDER_PCT,
    scalp_border_threshold_pct: Any = SCALP_BORDER_PCT,
    previous_bullish_pct: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fuse the three KRONOS probability streams into a live oscillator.

    Aliases are accepted for integration convenience (``base_pct``,
    ``option_paths_up``, ``fut_vol_ratio``, etc.).  Returned probabilities are
    rounded to one decimal place and always sum to exactly 100.0.
    """
    aliases = {
        "base_pct": "base_upside_pct",
        "kronos_base_pct": "base_upside_pct",
        "small_pct": "small_probability_pct",
        "kronos_small_pct": "small_probability_pct",
        "mini_pct": "mini_probability_pct",
        "kronos_mini_pct": "mini_probability_pct",
        "option_paths_up": "mini_paths_up",
        "option_samples": "mini_samples",
        "futures_vol_ratio": "futures_volume_ratio",
        "bullish_pct": "base_upside_pct",
    }
    values = locals()
    for alias, canonical in aliases.items():
        if alias in kwargs and values.get(canonical) is None:
            if canonical == "base_upside_pct":
                base_upside_pct = kwargs[alias]
            elif canonical == "small_probability_pct":
                small_probability_pct = kwargs[alias]
            elif canonical == "mini_probability_pct":
                mini_probability_pct = kwargs[alias]
            elif canonical == "mini_paths_up":
                mini_paths_up = kwargs[alias]
            elif canonical == "mini_samples":
                mini_samples = kwargs[alias]
            elif canonical == "futures_volume_ratio":
                futures_volume_ratio = kwargs[alias]

    base = _score(base_upside_pct)
    if base is None:
        base = _score_from_paths(base_paths_up, base_samples)
    small = _score(small_probability_pct)
    if small is None:
        small = _small_from_regime(pcr, vix_change_pct, breadth_aligned, breadth_total, trend_slope)
    mini = _score(mini_probability_pct)
    if mini is None:
        mini = _score_from_paths(mini_paths_up, mini_samples)
    if mini is None:
        mini = _mini_from_flow(obi_5, tbq_tsq, futures_volume_ratio, trend_slope)

    available = [base is not None, small is not None, mini is not None]
    missing = [name for name, ok in zip(("base", "small", "mini"), available) if not ok]
    filled = {
        "base": base if base is not None else 50.0,
        "small": small if small is not None else 50.0,
        "mini": mini if mini is not None else 50.0,
    }
    bullish = round(
        sum(filled[name] * weight for name, weight in MODEL_WEIGHTS.items()), 1
    )
    bullish = round(_clamp(bullish), 1)
    bearish = round(max(0.0, min(100.0, 100.0 - bullish)), 1)
    neutral = round(_clamp(100.0 - abs(bullish - 50.0) * 2.0), 1)
    threshold = round(_clamp(_f(border_threshold_pct, SNIPER_BORDER_PCT)), 1)
    scalp_threshold = round(_clamp(_f(scalp_border_threshold_pct, SCALP_BORDER_PCT)), 1)
    bull_reached = bool(bullish >= threshold)
    bear_reached = bool(bearish >= threshold)
    scalp_reached = bool(max(bullish, bearish) >= scalp_threshold)

    flow_values = [value for value in (obi_5, tbq_tsq, futures_volume_ratio) if value is not None]
    if flow_values:
        flow_ratio = 0.5
        if obi_5 is not None:
            flow_ratio += (_f(obi_5, 1.0) - 1.0) * 0.22
        if tbq_tsq is not None:
            flow_ratio += (_f(tbq_tsq, 1.0) - 1.0) * 0.22
        if futures_volume_ratio is not None:
            flow_ratio += (_f(futures_volume_ratio, 1.0) - 1.0) * 0.08
        if trend_slope is not None:
            flow_ratio += _clamp(_f(trend_slope) * 0.01, -0.10, 0.10)
        order_flow_force = round(_clamp((flow_ratio - 0.5) * 200.0, -100.0, 100.0), 1)
    else:
        order_flow_force = 0.0
    model_force = round(_clamp((bullish - 50.0) * 2.0, -100.0, 100.0), 1)
    oscillator = round(_clamp(model_force * 0.55 + order_flow_force * 0.45, -100.0, 100.0), 1)
    previous = _score(previous_bullish_pct)
    delta = round(bullish - previous, 1) if previous is not None else 0.0
    data_quality = "LIVE" if not missing else ("WAITING" if len(missing) == 3 else "DEGRADED")
    border_status = _border_te(bullish, bearish, threshold)
    return {
        "bullish_upside_prob_pct": bullish,
        "bearish_downside_prob_pct": bearish,
        "chop_neutral_pressure_pct": neutral,
        "probability_sum_pct": round(bullish + bearish, 1),
        "probability_delta_pct": delta,
        "border_threshold_pct": threshold,
        "scalp_border_threshold_pct": scalp_threshold,
        "bullish_border_reached": bull_reached,
        "bearish_border_reached": bear_reached,
        "scalp_border_reached": scalp_reached,
        "border_side": "BULL" if bull_reached else "BEAR" if bear_reached else "NONE",
        "border_distance_pct": round(
            max(0.0, threshold - bullish) if bullish >= bearish else max(0.0, threshold - bearish),
            1,
        ),
        "border_status_te": border_status,
        "order_flow_force": order_flow_force,
        "model_forecast_force": model_force,
        "oscillator_value": oscillator,
        "oscillator_side": "BULL" if oscillator >= 15 else "BEAR" if oscillator <= -15 else "NEUTRAL",
        "war_state": _war_state(order_flow_force, model_force),
        "data_quality": data_quality,
        "inputs_available": not missing,
        "missing_models": missing,
        "base_score": None if base is None else round(base, 1),
        "small_score": None if small is None else round(small, 1),
        "mini_score": None if mini is None else round(mini, 1),
        "weights": dict(MODEL_WEIGHTS),
        "input_snapshot": {
            "base_paths_up": base_paths_up,
            "base_samples": base_samples,
            "pcr": pcr,
            "vix_change_pct": vix_change_pct,
            "breadth_aligned": breadth_aligned,
            "breadth_total": breadth_total,
            "mini_paths_up": mini_paths_up,
            "mini_samples": mini_samples,
            "obi_5": obi_5,
            "tbq_tsq": tbq_tsq,
            "futures_volume_ratio": futures_volume_ratio,
            "trend_slope": trend_slope,
        },
        "generated_at": datetime.now(tz=IST).isoformat(timespec="milliseconds"),
    }


def build_oscillator_from_feed(
    feed: Mapping[str, Any] | None,
    symbol: str = "NIFTY",
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract live inputs from the saved feed; never invent missing model data."""
    feed = feed or {}
    symbol = str(symbol or "NIFTY").upper()
    plan = plan or {}
    if not plan:
        brain = feed.get("model_brain") or {}
        plan = brain.get(symbol) or {} if isinstance(brain, Mapping) else {}
        if not plan:
            plan = feed.get("plan") or {}
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    depth = spectrum.get("depth") or {}
    pcr_block = spectrum.get("pcr") or {}
    vix = spectrum.get("vix") or {}
    breadth = spectrum.get("breadth") or {}
    base_paths = plan.get("paths_up")
    base_samples = plan.get("samples")
    mini_paths = plan.get("mini_paths_up")
    mini_samples = plan.get("mini_samples")
    if mini_paths is None:
        for row in (feed.get("scanner") or {}).get(symbol, []) if isinstance(feed.get("scanner"), Mapping) else []:
            if not isinstance(row, Mapping):
                continue
            key = str(row.get("key") or "")
            if key == "kronos_base_paths":
                match = re.search(r"(\d+)\s*/\s*(\d+)", str(row.get("value") or ""))
                if match:
                    base_paths, base_samples = int(match.group(1)), int(match.group(2))
            elif key == "kronos_mini_opt_paths":
                match = re.search(r"(\d+)\s*/\s*(\d+)", str(row.get("value") or ""))
                if match:
                    mini_paths, mini_samples = int(match.group(1)), int(match.group(2))
    return build_model_oscillator(
        base_paths_up=base_paths,
        base_samples=base_samples,
        pcr=pcr_block.get("pcr"),
        vix_change_pct=vix.get("change_pct"),
        breadth_aligned=breadth.get("aligned"),
        breadth_total=breadth.get("total"),
        mini_paths_up=mini_paths,
        mini_samples=mini_samples,
        obi_5=depth.get("obi_5"),
        tbq_tsq=depth.get("tbq_tsq"),
        futures_volume_ratio=plan.get("futures_volume_ratio"),
        trend_slope=plan.get("trend_slope"),
    )


# Public aliases for dashboards/agents that use the shorter name.
compute_model_oscillator = build_model_oscillator
build_oscillator = build_oscillator_from_feed

__all__ = [
    "MODEL_WEIGHTS", "SNIPER_BORDER_PCT", "SCALP_BORDER_PCT",
    "build_model_oscillator", "compute_model_oscillator",
    "build_oscillator_from_feed", "build_oscillator",
]
