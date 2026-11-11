"""Institutional mathematical filters used by the four-angle playbook desk.

The functions here are deliberately pure: no clock, no network, no broker.
That keeps the safety-critical book/EV maths deterministic in replay and lets
the dashboard show exactly how a score was produced.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

OBI_DECAY = 0.35
EV_RATIO_FLOOR = 1.80
MIN_THETA_DENOMINATOR = 0.25


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _quantity(level: Any) -> float:
    if isinstance(level, Mapping):
        for key in ("quantity", "qty", "q", "size"):
            if key in level:
                return max(0.0, _f(level.get(key)))
        return 0.0
    if isinstance(level, (tuple, list)) and len(level) >= 2:
        # Common Upstox shape: (price, quantity).
        return max(0.0, _f(level[1]))
    return max(0.0, _f(level))


def exponential_weights(levels: int = 5, decay: float = OBI_DECAY) -> list[float]:
    """Weights ``e^-0.35(k-1)`` for k=1..levels (top of book first)."""
    count = max(1, int(levels))
    return [math.exp(-float(decay) * idx) for idx in range(count)]


def weighted_book_imbalance(
    bids: Iterable[Any],
    asks: Iterable[Any],
    *,
    levels: int = 5,
    decay: float = OBI_DECAY,
) -> float:
    """Exponentially weighted five-level bid/ask quantity imbalance.

    A ratio of 1.0 is balanced; values above 1.0 mean buyers dominate the top
    of the book.  The top level carries the most weight, which is what makes a
    genuine institutional wall different from five equal-sized retail rows.
    """
    weights = exponential_weights(levels, decay)
    bid_rows = list(bids or [])[:levels]
    ask_rows = list(asks or [])[:levels]
    bid_qty = sum(weight * _quantity(row) for weight, row in zip(weights, bid_rows))
    ask_qty = sum(weight * _quantity(row) for weight, row in zip(weights, ask_rows))
    if ask_qty <= 0:
        return 0.0 if bid_qty > 0 else 1.0
    return round(bid_qty / ask_qty, 4)


def book_imbalance_from_depth(
    depth: Mapping[str, Any] | None,
    *,
    levels: int = 5,
    decay: float = OBI_DECAY,
) -> float:
    """Convenience wrapper around the canonical live-feed depth shape."""
    book = depth or {}
    return weighted_book_imbalance(
        book.get("bids") or book.get("bid") or [],
        book.get("asks") or book.get("ask") or [],
        levels=levels,
        decay=decay,
    )


def greek_ev_ratio(
    *,
    delta: Any,
    gamma: Any,
    expected_abs_move: Any,
    theta_15m: Any,
    spread_inr: Any,
) -> dict[str, float]:
    """Greek-adjusted convexity-to-theta ratio from the institutional spec.

    ``EV = (|delta| * E|dS| + 0.5 * gamma * E[dS]^2) / max(0.25, |theta| + spread)``
    """
    move = max(0.0, _f(expected_abs_move))
    convexity = abs(_f(delta)) * move + 0.5 * max(0.0, _f(gamma)) * move * move
    decay = abs(_f(theta_15m)) + max(0.0, _f(spread_inr))
    denominator = max(MIN_THETA_DENOMINATOR, decay)
    ratio = convexity / denominator
    return {
        "ev_ratio": round(ratio, 4),
        "convexity_inr": round(convexity, 4),
        "theta_spread_denominator": round(denominator, 4),
        "expected_abs_move": round(move, 4),
        "approved": bool(ratio >= EV_RATIO_FLOOR),
    }


def directional_conviction_score(probability_pct: Any) -> float:
    """Map directional probability into a 0..1 conviction component."""
    return _clamp((_f(probability_pct) - 50.0) / 50.0)


def flow_score(obi_5: Any, tbq_tsq: Any, futures_ratio: Any) -> float:
    """Blend book, trade-book and futures participation into 0..1.

    This is a scoring helper for the radar only; the hard entry thresholds are
    applied separately by :mod:`strategy.multi_angle_playbook_engine`.
    """
    obi = _clamp((max(0.0, _f(obi_5, 1.0)) - 1.0) / 2.0)
    tbq = _clamp((max(0.0, _f(tbq_tsq, 1.0)) - 1.0) / 1.5)
    fut = _clamp((max(0.0, _f(futures_ratio, 1.0)) - 1.0) / 1.0)
    return round((obi * 0.45) + (tbq * 0.35) + (fut * 0.20), 4)


__all__ = [
    "EV_RATIO_FLOOR",
    "OBI_DECAY",
    "book_imbalance_from_depth",
    "directional_conviction_score",
    "exponential_weights",
    "flow_score",
    "greek_ev_ratio",
    "weighted_book_imbalance",
]
