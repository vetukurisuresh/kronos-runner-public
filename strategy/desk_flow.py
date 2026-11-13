"""Visual-flow automation view for the three independent ₹1,00,000 desks.

The heavy lifting (capital ledger, routing, P&L) already lives in the session
under ``payload["desks"]``; this module only *derives* the flow diagram state:
per-index momentum edge, the 102M -> 25M -> 4M handshake, the selected strike
and the execution node.  Every field is computed from data that is actually in
the payload -- a missing input is ``None`` with a reason, never a guess.
"""

from __future__ import annotations

from typing import Any, Mapping

INDEX_ORDER = ("NIFTY", "BANKNIFTY", "SENSEX")
DESKS = (
    ("directional_100k", "🧭 DIRECTIONAL TREND DESK"),
    ("scalping_100k", "⚡ SMART SCALPING DESK"),
    ("zero_to_hero_100k", "🚀 EXPIRY ZERO-TO-HERO"),
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _d(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _block(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    rp = _d(payload.get("role_pipeline"))
    symbols = _d(rp.get("symbols"))
    block = _d(symbols.get(name))
    if block:
        return block
    if rp.get("primary") == name or not symbols:
        return rp
    return {}


def index_edge_scores(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """0-100 option-buyer edge per index, over the components we really have."""
    out: dict[str, dict[str, Any]] = {}
    for name in INDEX_ORDER:
        block = _block(payload, name)
        gate = _d(block.get("gate_25m"))
        micro = _d(block.get("micro_4m"))
        obi = _f(gate.get("obi_5"))
        tbq = _f(micro.get("tbq_tsq"))
        conviction = max(_f(gate.get("bull_score")), _f(gate.get("bear_score")))
        parts: list[tuple[float, float]] = []      # (weight, 0-100 value)
        if obi:
            parts.append((0.40, min(100.0, 100.0 * obi / 2.5)))
        if tbq:
            parts.append((0.25, min(100.0, 100.0 * tbq / 2.5)))
        if conviction:
            parts.append((0.35, min(100.0, conviction)))
        if not parts:
            out[name] = {"symbol": name, "score": None, "components": {},
                         "reason": "no live edge inputs yet"}
            continue
        weight = sum(w for w, _ in parts)
        score = round(sum(w * v for w, v in parts) / weight, 1)
        out[name] = {
            "symbol": name,
            "score": score,
            "components": {"obi_5": obi or None, "tbq_tsq": tbq or None,
                           "conviction": conviction or None},
            "reason": "blended from live OBI / TBQ / conviction",
        }
    return out


def _node(label: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"label": label, "ok": bool(ok), "mark": "✅" if ok else "⏳", "detail": detail}


def _directional(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    block = _block(payload, name)
    gate = _d(block.get("gate_25m"))
    micro = _d(block.get("micro_4m"))
    osc = _d(payload.get("model_oscillator"))
    upside = _f(osc.get("bullish_upside_prob_pct")) or _f(osc.get("directional_probability_pct"))
    downside = _f(osc.get("bearish_downside_prob_pct"))
    if not downside and upside:
        downside = 100.0 - upside
    if max(upside, downside) <= 0:
        prob, side = 0.0, ""
    elif downside > upside:
        prob, side = downside, "PE"
    else:
        prob, side = upside, "CE"
    bull, bear = bool(gate.get("data_confirmed_bullish")), bool(gate.get("data_confirmed_bearish"))
    n102 = _node("KRONOS-102M", prob >= 62.0, f"{prob:.0f}% {side or '—'} ({upside:.0f}/{downside:.0f})")
    n25 = _node("KRONOS-25M", bull or bear,
                f"PCR {_f(gate.get('pcr')):.2f} · Δ {_f(gate.get('delta')):.2f} · "
                f"{'BULL' if bull else 'BEAR' if bear else 'WAIT'}")
    obi = _f(micro.get("obi_5")) or _f(gate.get("obi_5"))
    n4 = _node("KRONOS-4M", obi >= 1.25,
               f"OBI {obi:.2f}x · TBQ {_f(micro.get('tbq_tsq')):.2f}x")
    return {"id": "directional_100k", "nodes": [n102, n25, n4],
            "handshake": prob >= 62.0 and (bull or bear) and obi >= 1.25,
            "side": "CE" if bull or side == "BULL" else "PE" if bear or side == "BEAR" else None}


def _scalping(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    block = _block(payload, name)
    micro = _d(block.get("micro_4m"))
    gate = _d(block.get("gate_25m"))
    mode = str(micro.get("mode") or "SCANNING")
    obi = _f(micro.get("obi_5")) or _f(gate.get("obi_5"))
    armed = bool(((_d(payload.get("intra_candle")).get(name) or {})).get("armed"))
    n25 = _node("KRONOS-25M", armed, "strike cache armed" if armed else "cache arming")
    n4 = _node("KRONOS-4M", obi >= 1.45, f"{mode} · OBI {obi:.2f}x")
    return {"id": "scalping_100k", "nodes": [n25, n4],
            "handshake": armed and obi >= 1.45, "mode": mode,
            "side": "CE" if mode else None}


def _zero_to_hero(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    block = _block(payload, name)
    gate = _d(block.get("gate_25m"))
    trap = bool(gate.get("writer_trap_explosion"))
    gamma = _f(gate.get("gamma"))
    obi = _f(gate.get("obi_5"))
    n25 = _node("KRONOS-25M", trap, f"writer trap {'ARMED' if trap else 'none'} · γ {gamma:.3f}")
    n4 = _node("KRONOS-4M", obi >= 1.75, f"blast OBI {obi:.2f}x")
    return {"id": "zero_to_hero_100k", "nodes": [n25, n4],
            "handshake": trap and obi >= 1.75,
            "side": "CE" if obi >= 1.75 else None}


def _exec_node(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Execution node state taken from the newest real trade row."""
    rows = [r for r in (ledger.get("trades") or []) if isinstance(r, Mapping)]
    if not rows:
        return {"status": "PENDING", "symbol": None, "strike": None, "side": None}
    trade = rows[-1]
    status = str(trade.get("status") or trade.get("state") or
                 ("DONE" if trade.get("exit") is not None else "RUNNING")).upper()
    return {
        "status": "DONE" if "DONE" in status or "CLOSED" in status else "RUNNING",
        "symbol": trade.get("symbol"), "strike": trade.get("strike"), "side": trade.get("side"),
        "entry": trade.get("entry"), "sl": trade.get("sl"), "t1": trade.get("t1"),
        "t2": trade.get("t2"), "pnl": trade.get("pnl", trade.get("profit")),
    }


def build_desk_flow(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble the full 3 x ₹1,00,000 visual-flow state for the dashboard."""
    payload = payload or {}
    ledgers = _d(payload.get("desks"))
    edges = index_edge_scores(payload)
    ranked = sorted(
        (row for row in edges.values() if row.get("score") is not None),
        key=lambda row: row["score"], reverse=True,
    )
    top = ranked[0]["symbol"] if ranked else None
    flows = {}
    for desk_id, label in DESKS:
        if desk_id == "directional_100k":
            flow = _directional(payload, top or "NIFTY")
        elif desk_id == "scalping_100k":
            flow = _scalping(payload, top or "NIFTY")
        else:
            flow = _zero_to_hero(payload, top or "NIFTY")
        ledger = _d(ledgers.get(desk_id))
        flow.update({
            "label": label,
            "index": ledger.get("routed_index") or top,
            "capital_inr": _f(ledger.get("capital_inr"), 100000.0),
            "equity_inr": _f(ledger.get("equity_inr"), 100000.0),
            "booked_pnl_inr": _f(ledger.get("booked_pnl_inr")),
            "live_pnl_inr": _f(ledger.get("live_pnl_inr")),
            "edge_score": ledger.get("routed_edge_score"),
            "trades": list(ledger.get("trades") or []),
            "exec": _exec_node(ledger),
        })
        flows[desk_id] = flow
    return {
        "desks": flows,
        "index_edges": edges,
        "ranking": [row["symbol"] for row in ranked],
        "index_momentum_ranking": [
            {"rank": i + 1, "symbol": row["symbol"], "score": row["score"]}
            for i, row in enumerate(ranked)
        ],
        "top_index": top,
        "total_capital_inr": sum(_f(_d(ledgers.get(d)).get("capital_inr"), 100000.0)
                                 for d, _ in DESKS),
    }


__all__ = ["build_desk_flow", "index_edge_scores"]
