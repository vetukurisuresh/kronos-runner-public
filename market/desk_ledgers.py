"""Three isolated ₹1,00,000 desk ledgers and a 0-100 index momentum selector.

Each desk owns its own capital ledger, so the desks never share a mutex.  The
selector ranks NIFTY / BANKNIFTY / SENSEX by a live edge score built only from
real feed fields (5m ATR expansion, futures volume impulse, order-book
``obi_5`` and ``tbq_tsq``).  Missing inputs score 0 instead of being invented.
"""

from __future__ import annotations

from typing import Any, Mapping

DESK_CAPITAL_INR = 100_000.0
DESKS: dict[str, dict[str, Any]] = {
    "directional_100k": {
        "label": "Directional Trend Desk",
        "strategy": "102M>=75% -> 25M -> 4M handshake, ITM delta 0.55-0.65",
        "small_sl_pct": -7.0,
        "cost_lock_r": 1.0,
        "exits": {"t1_pct": 35, "t2_pct": 35, "runner_pct": 30},
    },
    "scalping_100k": {
        "label": "Smart Scalping Desk",
        "strategy": "25M + 4M sideways ping-pong / shockwave, peak-tick ratchet",
        "small_sl_pct": -4.0,
        "cost_lock_r": 0.5,
        "exits": {"t1_pct": 50, "t2_pct": 30, "runner_pct": 20},
    },
    "zero_to_hero_100k": {
        "label": "Expiry Zero-to-Hero Desk",
        "strategy": "multi-index momentum + 25M writer trap + 4M gamma blast",
        "small_sl_pct": -15.0,
        "cost_lock_r": 1.0,
        "exits": {"t1_pct": 40, "t2_pct": 30, "runner_pct": 30},
    },
}
INDEXES = ("NIFTY", "BANKNIFTY", "SENSEX")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN guard


def _atr_expansion_score(candles_5m: list[Mapping[str, Any]]) -> float:
    """Recent 5m true-range vs. the prior 20-bar mean, mapped to 0-1."""
    if len(candles_5m) < 22:
        return 0.0
    ranges = []
    for row in candles_5m:
        high, low = _f(row.get("high")), _f(row.get("low"))
        if high > 0 and low > 0:
            ranges.append(max(high - low, 0.0))
    if len(ranges) < 22:
        return 0.0
    recent = sum(ranges[-3:]) / 3.0
    base = sum(ranges[-23:-3]) / 20.0
    if base <= 0:
        return 0.0
    ratio = recent / base
    return max(0.0, min((ratio - 1.0) / 1.0, 1.0))


def edge_score(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Score one index 0-100 from real feed inputs.

    Components (each 0-1, weighted to 100):
      atr 40 · futures volume impulse 20 · obi_5 20 · tbq_tsq 20
    """
    atr = _atr_expansion_score(list(inputs.get("candles_5m") or []))
    fut_ratio = _f(inputs.get("futures_volume_ratio"), 1.0)
    fut = max(0.0, min((fut_ratio - 1.0) / 1.0, 1.0))
    obi = _f(inputs.get("obi_5"), 1.0)
    obi_score = max(0.0, min((obi - 1.0) / 0.75, 1.0))
    tbq = _f(inputs.get("tbq_tsq"), 1.0)
    tbq_score = max(0.0, min((tbq - 1.0) / 0.75, 1.0))
    score = round(100.0 * (0.40 * atr + 0.20 * fut + 0.20 * obi_score + 0.20 * tbq_score), 1)
    return {
        "score": score,
        "atr_expansion": round(atr, 3),
        "futures_impulse": round(fut, 3),
        "obi_5_score": round(obi_score, 3),
        "tbq_tsq_score": round(tbq_score, 3),
    }


def index_momentum_ranking(feed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rank NIFTY / BANKNIFTY / SENSEX by live edge, strongest first."""
    spot = feed.get("spot") or {}
    option = feed.get("option") or {}
    full = feed.get("full_spectrum") or {}
    rows = []
    for name in INDEXES:
        block = spot.get(name) or {}
        depth = (full.get(name) or {}).get("depth") or {}
        futures = (full.get(name) or {}).get("futures") or {}
        fut_vol = _f(futures.get("volume"))
        fut_mean = _f(futures.get("volume_20bar_mean"), 0.0)
        fut_ratio = fut_vol / fut_mean if fut_mean > 0 else 1.0
        obi = _f((option.get(name) or {}).get("obi_5"), 0.0) or _f(depth.get("obi_5"), 1.0)
        result = edge_score({
            "candles_5m": block.get("candles_5m") or [],
            "futures_volume_ratio": fut_ratio,
            "obi_5": obi,
            "tbq_tsq": _f(depth.get("tbq_tsq"), 1.0),
        })
        rows.append({"index": name, **result})
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows


def pick_index_for_desk(ranking: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The strongest index.  Returns None when no index has any live edge."""
    if not ranking or ranking[0]["score"] <= 0:
        return None
    return ranking[0]


def build_desks(feed: Mapping[str, Any], trades: Mapping[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Each desk's isolated ledger, its routed index and its trade rows."""
    trades = trades or {}
    ranking = index_momentum_ranking(feed)
    best = pick_index_for_desk(ranking)
    desks: dict[str, Any] = {}
    for key, spec in DESKS.items():
        rows = list(trades.get(key) or [])
        booked = round(sum(_f(row.get("booked_pnl_inr")) for row in rows), 2)
        live = round(sum(_f(row.get("live_pnl_inr")) for row in rows
                         if str(row.get("status") or "").upper() == "RUNNING"), 2)
        desks[key] = {
            **spec,
            "capital_inr": DESK_CAPITAL_INR,
            "equity_inr": round(DESK_CAPITAL_INR + booked + live, 2),
            "booked_pnl_inr": booked,
            "live_pnl_inr": live,
            "routed_index": best["index"] if best else None,
            "routed_edge_score": best["score"] if best else 0.0,
            "trades": rows,
        }
    return {"desks": desks, "index_momentum": ranking}
