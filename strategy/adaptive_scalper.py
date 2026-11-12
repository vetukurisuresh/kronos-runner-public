"""All-weather adaptive scalper: regimes, net-edge triggers and fast harvest.

The module is pure.  The live session supplies the current IST clock, the
Kronos p10/median/p90 cone and the 5-level option books; every decision can be
replayed offline.

Four actionable states are emitted:

* ``OPENING_VELOCITY``      fast momentum burst
* ``SIDEWAYS_PING_PONG``    p10/p90 elastic range scalps toward MED/VWAP
* ``TREND_PULLBACK_SCALP``  ATP reclaim continuation scalps
* ``DEAD_COMPRESSION``      strict brokerage/theta shield

Every scalp must clear the Rs 6.50 expected-move and 1:1.7 reward/risk gates
after a Rs 1.00 friction estimate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

REGIME_OPENING_VELOCITY = "OPENING_VELOCITY"
REGIME_SIDEWAYS_PING_PONG = "SIDEWAYS_PING_PONG"
REGIME_TREND_PULLBACK_SCALP = "TREND_PULLBACK_SCALP"
REGIME_DEAD_COMPRESSION = "DEAD_COMPRESSION"
REGIME_CLOSED = "CLOSED"

MODE_AUTO = "AUTO"
MODE_PING_PONG = "PING_PONG"
MODE_VELOCITY = "VELOCITY"
MODE_TREND_ONLY = "TREND_ONLY"
SCALPER_MODES = (MODE_AUTO, MODE_PING_PONG, MODE_VELOCITY, MODE_TREND_ONLY)

MIN_EXPECTED_MOVE = 6.50
MIN_REWARD_RISK = 1.70
FRICTION_PER_POINT = 1.00
MAX_SCALPS_PER_DAY = 6
COOLDOWN_SECONDS = 15

REGIME_LABELS = {
    REGIME_OPENING_VELOCITY: "🚀 OPENING VELOCITY",
    REGIME_SIDEWAYS_PING_PONG: "🏓 SIDEWAYS PING-PONG",
    REGIME_TREND_PULLBACK_SCALP: "📈 TREND PULLBACK SCALP",
    REGIME_DEAD_COMPRESSION: "🛡️ DEAD COMPRESSION",
    REGIME_CLOSED: "⏸ MARKET CLOSED",
}

STRATEGY_LABELS = {
    REGIME_OPENING_VELOCITY: "FAST_MOMENTUM_BURST",
    REGIME_SIDEWAYS_PING_PONG: "MEAN_REVERSION_PING_PONG",
    REGIME_TREND_PULLBACK_SCALP: "ATP_PULLBACK_CONTINUATION",
    REGIME_DEAD_COMPRESSION: "NO_TRADE",
    REGIME_CLOSED: "NO_TRADE",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_ist(moment: datetime | str | None) -> datetime:
    if moment is None:
        return datetime.now(tz=IST)
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def normalize_scalper_mode(mode: Any = MODE_AUTO) -> str:
    text = str(mode or MODE_AUTO).strip().upper().replace("-", "_").replace(" ", "_")
    if text in {"ON", "AUTO", "AUTO_ADAPTIVE", "AUTO_ADAPTIVE_SCALPER", "TRUE", "1"}:
        return MODE_AUTO
    if text in {"PING_PONG", "PINGPONG", "RANGE", "RANGE_SCALP"}:
        return MODE_PING_PONG
    if text in {"VELOCITY", "OPENING_VELOCITY", "BURST"}:
        return MODE_VELOCITY
    if text in {"TREND", "TREND_ONLY", "CONTINUATION"}:
        return MODE_TREND_ONLY
    return MODE_AUTO


def _in_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    return time(9, 15) <= moment.time() <= time(15, 25)


def _in_opening_window(moment: datetime) -> bool:
    return time(9, 18) <= moment.time() < time(9, 50)


def classify_regime(
    now: datetime | str | None = None,
    *,
    symbol: str = "NIFTY",
    option_atr_1m: Any = 0.0,
    futures_volume_ratio: Any = 0.0,
    mini_velocity: Any = 0.0,
    paths_up: Any = 0,
    samples: Any = 0,
    p10: Any = 0.0,
    p90: Any = 0.0,
    median: Any = 0.0,
    spot: Any = 0.0,
    obi_5: Any = 0.0,
    tbq: Any = 0.0,
    tsq: Any = 0.0,
    spread: Any = 0.0,
    mode: Any = MODE_AUTO,
    post_trade: bool = False,
) -> dict[str, Any]:
    """Classify the live market into one of the four scalper regimes."""
    moment = _as_ist(now)
    symbol = str(symbol or "NIFTY").upper()
    mode = normalize_scalper_mode(mode)
    atr = max(_f(option_atr_1m), 0.0)
    fut_vol = max(_f(futures_volume_ratio), 0.0)
    mini_v = _f(mini_velocity)
    paths = _f(paths_up)
    total = _f(samples)
    p_up = (paths / total) if total > 0 else 0.0
    range_width = max(_f(p90) - _f(p10), 0.0) if _f(p90) > 0 and _f(p10) > 0 else 0.0
    spread = max(_f(spread), 0.0)
    bank = symbol == "BANKNIFTY"
    dead_width = 60.0 if bank else 25.0
    elastic_width = 120.0 if bank else 45.0

    diagnostics = {
        "symbol": symbol,
        "option_atr_1m": round(atr, 2),
        "futures_volume_ratio": round(fut_vol, 2),
        "mini_velocity": round(mini_v, 3),
        "paths_pct": round(p_up * 100.0, 2) if total else 0.0,
        "range_width": round(range_width, 2),
        "spread": round(spread, 2),
        "obi_5": round(_f(obi_5), 3),
        "tbq_tsq": round(_f(tbq) / _f(tsq), 3) if _f(tsq) > 0 else 0.0,
        "p10": round(_f(p10), 2),
        "median": round(_f(median), 2),
        "p90": round(_f(p90), 2),
        "spot": round(_f(spot), 2),
    }

    base = REGIME_DEAD_COMPRESSION
    reason = "NO_ACTIONABLE_REGIME"
    blocked = True
    direction = "BOTH"

    if not _in_session(moment):
        base = REGIME_CLOSED
        reason = "OUTSIDE_SCALPER_SESSION"
    elif spread > 0.60:
        base = REGIME_DEAD_COMPRESSION
        reason = f"SPREAD_TOO_WIDE ₹{spread:.2f} > ₹0.60"
    elif atr < 1.80:
        base = REGIME_DEAD_COMPRESSION
        reason = f"OPTION_1M_ATR ₹{atr:.2f} < ₹1.80"
    elif range_width < dead_width:
        base = REGIME_DEAD_COMPRESSION
        reason = f"PREDICTED_RANGE {range_width:.0f} < {dead_width:.0f} pts"
    else:
        high_atr_breakout = (
            atr >= 3.0 and fut_vol >= 2.2 and abs(mini_v) >= 0.20
        )
        if _in_opening_window(moment) and high_atr_breakout:
            base = REGIME_OPENING_VELOCITY
            reason = "09:18–09:50 HIGH-ATR BREAKOUT"
            blocked = False
            direction = "CE" if p_up >= 0.50 else "PE"
        elif high_atr_breakout and not _in_opening_window(moment):
            base = REGIME_OPENING_VELOCITY
            reason = "HIGH-ATR BREAKOUT OUTSIDE OPENING WINDOW"
            blocked = False
            direction = "CE" if p_up >= 0.50 else "PE"
        elif 0.35 <= p_up <= 0.65 and range_width >= elastic_width and atr >= 2.20:
            base = REGIME_SIDEWAYS_PING_PONG
            reason = "NEUTRAL PATHS + ELASTIC RANGE"
            blocked = False
            direction = "BOTH"
        elif total > 0 and (p_up >= 0.75 or p_up <= 0.25):
            base = REGIME_TREND_PULLBACK_SCALP
            reason = "KRONOS TREND CONVICTION ≥75%"
            blocked = False
            direction = "CE" if p_up >= 0.50 else "PE"
        else:
            base = REGIME_DEAD_COMPRESSION
            reason = "LOW CONVICTION / NO RANGE EDGE"

    # UI mode is a filter, never a bypass of risk conditions.
    mode_blocked = ""
    if mode == MODE_PING_PONG and base != REGIME_SIDEWAYS_PING_PONG:
        mode_blocked = "MODE WAITING FOR 🏓 PING-PONG RANGE"
    elif mode == MODE_VELOCITY and base != REGIME_OPENING_VELOCITY:
        mode_blocked = "MODE WAITING FOR 🚀 OPENING VELOCITY"
    elif mode == MODE_TREND_ONLY and base != REGIME_TREND_PULLBACK_SCALP:
        mode_blocked = "MODE WAITING FOR 📈 TREND PULLBACK"
    if mode_blocked:
        blocked = True

    params = _regime_params(base)
    tradeable = bool(not blocked and not mode_blocked and base not in {
        REGIME_DEAD_COMPRESSION, REGIME_CLOSED
    })
    return {
        "ok": base not in {REGIME_DEAD_COMPRESSION, REGIME_CLOSED},
        "regime": base,
        "regime_label": REGIME_LABELS[base],
        "strategy": STRATEGY_LABELS[base],
        "tradeable": tradeable,
        "blocked": not tradeable,
        "reason": mode_blocked or reason,
        "mode": mode,
        "mode_blocked": mode_blocked,
        "direction": direction,
        "post_trade": bool(post_trade),
        "now_ist": moment.isoformat(timespec="seconds"),
        "range_width": round(range_width, 2),
        "paths_pct": round(p_up * 100.0, 2) if total else 0.0,
        "diagnostics": diagnostics,
        "params": params,
        "status": _status_text(base, reason, mode_blocked),
    }


def _regime_params(regime: str) -> dict[str, Any]:
    if regime == REGIME_OPENING_VELOCITY:
        return {
            "target_min": 8.0, "target_max": 14.0, "s1": 6.5, "s2": 14.0,
            "sl_points": 4.0, "hold_seconds": 45, "hold_max_seconds": 180,
            "label": "FAST MOMENTUM BURST",
        }
    if regime == REGIME_SIDEWAYS_PING_PONG:
        return {
            "target_min": 6.5, "target_max": 10.0, "s1": 6.5, "s2": 10.0,
            "sl_points": 3.5, "hold_seconds": 30, "hold_max_seconds": 120,
            "label": "MEAN-REVERSION PING-PONG",
        }
    if regime == REGIME_TREND_PULLBACK_SCALP:
        return {
            "target_min": 6.5, "target_max": 12.0, "s1": 6.5, "s2": 12.0,
            "sl_points": 4.0, "hold_seconds": 45, "hold_max_seconds": 180,
            "label": "ATP PULLBACK CONTINUATION",
        }
    return {
        "target_min": 0.0, "target_max": 0.0, "s1": 0.0, "s2": 0.0,
        "sl_points": 0.0, "hold_seconds": 0, "hold_max_seconds": 0,
        "label": "NO TRADE",
    }


def _status_text(regime: str, reason: str, mode_blocked: str) -> str:
    if mode_blocked:
        return f"{mode_blocked} — {reason}"
    if regime == REGIME_DEAD_COMPRESSION:
        return f"🛡️ DEAD ZONE — SAVING BROKERAGE & THETA ({reason})"
    if regime == REGIME_CLOSED:
        return "⏸ OUTSIDE 09:15–15:25 IST SCALPER SESSION"
    if regime == REGIME_SIDEWAYS_PING_PONG:
        return "🏓 PING-PONG RANGE SCALPER ARMED — WATCH p10 FOR CE / p90 FOR PE"
    if regime == REGIME_OPENING_VELOCITY:
        return "🚀 FAST MOMENTUM BURST ARMED — 45s–3m HOLD"
    return "📈 TREND PULLBACK SCALPER ARMED — BUY ATP RECLAIM"


def scalp_signal(
    decision: dict[str, Any] | None,
    *,
    spot: Any,
    p10: Any = 0.0,
    p90: Any = 0.0,
    median: Any = 0.0,
    vwap: Any = 0.0,
    ce_obi_5: Any = 0.0,
    ce_tbq: Any = 0.0,
    ce_tsq: Any = 0.0,
    pe_obi_5: Any = 0.0,
    pe_tbq: Any = 0.0,
    pe_tsq: Any = 0.0,
    ce_ltp: Any = 0.0,
    pe_ltp: Any = 0.0,
    expected_move_pts: Any = None,
    min_expected_move: float = MIN_EXPECTED_MOVE,
    min_rr: float = MIN_REWARD_RISK,
    friction: float = FRICTION_PER_POINT,
    ignition_low: Any = 0.0,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Build the next ``SCALP_BUY_CE``/``SCALP_BUY_PE`` order or a veto."""
    decision = decision or {}
    regime = str(decision.get("regime") or REGIME_DEAD_COMPRESSION)
    params = decision.get("params") or _regime_params(regime)
    side = ""
    target_spot = 0.0
    band_touch = False
    obi = tbq = tsq = 0.0
    entry = 0.0
    condition_name = ""

    if regime == REGIME_SIDEWAYS_PING_PONG:
        lower = _f(p10) or (_f(vwap) - 0.0)
        upper = _f(p90)
        lower_touch = _f(spot) <= lower > 0
        upper_touch = _f(spot) >= upper > 0
        if lower_touch and ce_obi_5 >= 1.45 and ce_tbq >= 1.30 * ce_tsq:
            side = "CE"
            target_spot = _f(median) or _f(vwap) or lower
            band_touch = True
            obi, tbq, tsq = _f(ce_obi_5), _f(ce_tbq), _f(ce_tsq)
            entry = _f(ce_ltp)
            condition_name = "LOWER p10 / VWAP BAND"
        elif upper_touch and pe_obi_5 >= 1.45 and pe_tbq >= 1.30 * pe_tsq:
            side = "PE"
            target_spot = _f(median) or _f(vwap) or upper
            band_touch = True
            obi, tbq, tsq = _f(pe_obi_5), _f(pe_tbq), _f(pe_tsq)
            entry = _f(pe_ltp)
            condition_name = "UPPER p90 / VWAP BAND"
    elif regime == REGIME_OPENING_VELOCITY:
        side = str(decision.get("direction") or "CE")
        target_spot = _f(median) or _f(p90) if side == "CE" else _f(median) or _f(p10)
        obi, tbq, tsq = (
            (_f(ce_obi_5), _f(ce_tbq), _f(ce_tsq))
            if side == "CE" else (_f(pe_obi_5), _f(pe_tbq), _f(pe_tsq))
        )
        entry = _f(ce_ltp if side == "CE" else pe_ltp)
        band_touch = obi >= 1.45
        condition_name = "VELOCITY IGNITION"
    elif regime == REGIME_TREND_PULLBACK_SCALP:
        side = str(decision.get("direction") or "CE")
        target_spot = _f(vwap) or _f(median)
        obi, tbq, tsq = (
            (_f(ce_obi_5), _f(ce_tbq), _f(ce_tsq))
            if side == "CE" else (_f(pe_obi_5), _f(pe_tbq), _f(pe_tsq))
        )
        entry = _f(ce_ltp if side == "CE" else pe_ltp)
        band_touch = obi >= 1.50
        condition_name = "ATP PULLBACK"

    expected = _f(expected_move_pts)
    if expected <= 0:
        expected = max(_f(params.get("target_min")), 0.0)
    sl_points = min(4.50, max(3.50, _f(params.get("sl_points"), 4.0)))
    s1 = _f(params.get("s1"), 6.5)
    s2 = _f(params.get("s2"), 10.0)
    net_edge = round(expected - friction, 2)
    rr = round(expected / sl_points, 3) if sl_points > 0 else 0.0
    flow_ok = tsq > 0 and tbq >= 1.30 * tsq
    edge_ok = expected >= min_expected_move and net_edge >= min_expected_move - friction
    rr_ok = rr >= min_rr
    price_ok = entry > 0 and side in {"CE", "PE"}
    tradeable = bool(decision.get("tradeable")) and not decision.get("mode_blocked")
    trigger = bool(tradeable and band_touch and flow_ok and edge_ok and rr_ok and price_ok)

    conditions = [
        {"key": "regime", "pass": bool(tradeable), "value": regime or "NONE"},
        {"key": "band_touch", "pass": bool(band_touch), "value": condition_name or "WAITING FOR BAND"},
        {"key": "order_flow", "pass": bool(flow_ok), "value": f"OBI {obi:.2f} · TBQ/TSQ {(tbq/tsq if tsq else 0):.2f}"},
        {"key": "net_edge", "pass": bool(edge_ok), "value": f"₹{expected:.2f} move · ₹{net_edge:.2f} net after friction"},
        {"key": "reward_risk", "pass": bool(rr_ok), "value": f"R:R 1:{rr:.2f} (need 1:{min_rr:.1f})"},
    ]
    reason = "SCALP_READY"
    if not tradeable:
        reason = str(decision.get("reason") or "REGIME_BLOCKED")
    elif not band_touch:
        reason = "WAITING_FOR_P10_P90_OR_ATP_RECLAIM"
    elif not flow_ok:
        reason = "ORDER_FLOW_NOT_IGNITED"
    elif not edge_ok:
        reason = "LOW_NET_EDGE_BROKERAGE_TRAP"
    elif not rr_ok:
        reason = "REWARD_RISK_BELOW_1_7"
    elif not price_ok:
        reason = "OPTION_PRICE_MISSING"

    return {
        "trigger": trigger,
        "order": f"SCALP_BUY_{side}" if trigger else "WAIT",
        "side": side,
        "symbol": decision.get("diagnostics", {}).get("symbol"),
        "regime": regime,
        "strategy": params.get("label", ""),
        "conditions": conditions,
        "signature_count": sum(1 for c in conditions if c["pass"]),
        "reason": reason,
        "entry": round(entry, 2),
        "stop": round(entry - sl_points, 2) if entry > 0 else 0.0,
        "sl_points": round(sl_points, 2),
        "target_1": round(entry + s1, 2) if entry > 0 else 0.0,
        "target_2": round(entry + s2, 2) if entry > 0 else 0.0,
        "target_spot": round(target_spot, 2),
        "expected_move_pts": round(expected, 2),
        "net_edge_pts": net_edge,
        "rr": rr,
        "hold_seconds": int(params.get("hold_seconds") or 0),
        "hold_max_seconds": int(params.get("hold_max_seconds") or 0),
        "ignition_low": round(_f(ignition_low), 2),
        "band": condition_name,
        "now_ist": _as_ist(now).isoformat(timespec="seconds"),
    }


def _mark_open(trade: dict[str, Any], price: float) -> None:
    entry = _f(trade.get("entry_price"))
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    qty_open = int(trade.get("qty_open") or 0)
    realized = _f(trade.get("realized_pnl_rs"))
    pnl = realized + (price - entry) * qty_open
    trade["live_pnl_rs"] = round(pnl, 0)
    trade["live_pnl_pct"] = round(pnl / (entry * initial) * 100.0, 2) if entry and initial else 0.0


def _close_scalp(trade: dict[str, Any], price: float, reason: str) -> dict[str, Any] | None:
    qty = int(trade.get("qty_open") or 0)
    if qty <= 0:
        return None
    entry = _f(trade.get("entry_price"))
    pnl = round((price - entry) * qty, 2)
    trade["qty_open"] = 0
    trade["realized_pnl_rs"] = round(_f(trade.get("realized_pnl_rs")) + pnl, 2)
    trade["status"] = "CLOSED"
    trade["exit_reason"] = reason
    trade["exit_price"] = round(price, 2)
    leg = {
        "leg": reason, "qty": qty, "price": round(price, 2), "pnl": pnl,
        "remaining": 0, "stop": trade.get("current_trailing_sl"),
        "realized": trade["realized_pnl_rs"],
    }
    trade.setdefault("booked_legs", []).append(leg)
    _mark_open(trade, price)
    return leg


def manage_scalp_trade(
    trade: dict[str, Any],
    ltp: Any,
    *,
    obi_5: Any = 1.0,
) -> dict[str, Any]:
    """60% at S1, snap SL to entry+₹1.50, 40% at S2 or OBI flip."""
    entry = _f(trade.get("entry_price"))
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    trade.setdefault("initial_qty", initial)
    trade.setdefault("qty_open", initial)
    trade.setdefault("realized_pnl_rs", 0.0)
    trade.setdefault("booked_legs", [])
    trade.setdefault("scalp_s1_done", False)
    trade.setdefault("scalp_s2_done", False)
    trade.setdefault("scalp_s1_target", round(entry + 6.5, 2))
    trade.setdefault("scalp_s2_target", round(entry + 10.0, 2))
    trade.setdefault("current_trailing_sl", round(entry - 4.0, 2))
    result = {
        "legs": [], "stage1": False, "stage2": False, "exit": False,
        "reason": "", "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
        "remaining": int(trade.get("qty_open") or 0), "status": trade.get("status"),
    }
    price = _f(ltp)
    if str(trade.get("status") or "").upper() != "OPEN" or entry <= 0:
        return result

    s1_target = _f(trade.get("scalp_s1_target"), entry + 6.5)
    s2_target = _f(trade.get("scalp_s2_target"), entry + 10.0)

    if not trade.get("scalp_s1_done") and price >= s1_target and int(trade.get("qty_open") or 0) > 0:
        qty = min(int(trade.get("qty_open") or 0), max(1, int(round(initial * 0.60))))
        if qty > 0:
            pnl = round((price - entry) * qty, 2)
            trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
            trade["realized_pnl_rs"] = round(_f(trade.get("realized_pnl_rs")) + pnl, 2)
            trade["current_trailing_sl"] = round(max(_f(trade.get("current_trailing_sl")), entry + 1.50), 2)
            trade["scalp_s1_done"] = True
            leg = {
                "leg": "SCALP_S1", "qty": qty, "price": round(price, 2), "pnl": pnl,
                "remaining": trade["qty_open"], "stop": trade["current_trailing_sl"],
                "realized": trade["realized_pnl_rs"],
            }
            trade.setdefault("booked_legs", []).append(leg)
            result["legs"].append(leg)
            result["stage1"] = True

    if trade.get("scalp_s1_done") and int(trade.get("qty_open") or 0) > 0:
        obi_flip = _f(obi_5) < 0.75
        target_hit = price >= s2_target
        if obi_flip or target_hit:
            reason = "SCALP_OBI_FLIP" if obi_flip and not target_hit else "SCALP_S2"
            leg = _close_scalp(trade, price, reason)
            if leg:
                result["legs"].append(leg)
            result["stage2"] = True
            result["exit"] = True
            result["reason"] = reason

    # The post-S1 stop locks Entry + 1.50; before S1, the hard micro-SL applies.
    if int(trade.get("qty_open") or 0) > 0:
        stop = _f(trade.get("current_trailing_sl"))
        if stop > 0 and price <= stop:
            post_s1 = bool(trade.get("scalp_s1_done"))
            reason = "SCALP_BREAKEVEN_STOP" if post_s1 else "SCALP_SL"
            leg = _close_scalp(trade, price, reason)
            if leg:
                result["legs"].append(leg)
            result["exit"] = True
            result["reason"] = reason

    _mark_open(trade, price)
    result["total_pnl"] = round(_f(trade.get("realized_pnl_rs")), 2)
    result["remaining"] = int(trade.get("qty_open") or 0)
    result["status"] = str(trade.get("status") or "OPEN")
    return result


def brain_banner(
    decision: dict[str, Any] | None,
    *,
    trade_no: int = 0,
    pnl: float = 0.0,
    previous_regime: str = "",
) -> str:
    """Human-readable post-trade decision for the dashboard banner."""
    decision = decision or {}
    regime = str(decision.get("regime") or "")
    label = REGIME_LABELS.get(regime, regime or "WAITING")
    pnl_text = f"{'+' if pnl >= 0 else '-'}₹{abs(pnl):,.0f}"
    trade_text = f"Trade #{trade_no} ({pnl_text}) Locked" if trade_no else "No closed trade yet"
    if regime == REGIME_SIDEWAYS_PING_PONG:
        d = decision.get("diagnostics") or {}
        return (
            f"🧠 POST-TRADE BRAIN: {trade_text} → Market shifted to "
            f"{_f(d.get('range_width')):.0f}pt Sideways Box → Switched to 🏓 PING-PONG RANGE SCALPER "
            f"(Watching p10 ₹{_f(d.get('p10')):,.0f} for CE / p90 ₹{_f(d.get('p90')):,.0f} for PE)"
        )
    if regime == REGIME_OPENING_VELOCITY:
        return f"🧠 POST-TRADE BRAIN: {trade_text} → Velocity expanded → 🚀 FAST MOMENTUM BURST ARMED"
    if regime == REGIME_TREND_PULLBACK_SCALP:
        return f"🧠 POST-TRADE BRAIN: {trade_text} → Trend conviction ≥75% → 📈 ATP PULLBACK SCALPER ARMED"
    if regime == REGIME_DEAD_COMPRESSION:
        return f"🧠 POST-TRADE BRAIN: {trade_text} → 🛡️ DEAD ZONE — SAVING BROKERAGE & THETA"
    return f"🧠 POST-TRADE BRAIN: {trade_text} → {label}"


def decision_snapshot(decision: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(decision or {})
    data.setdefault("regime", REGIME_DEAD_COMPRESSION)
    data.setdefault("regime_label", REGIME_LABELS.get(data["regime"], data["regime"]))
    data.setdefault("params", _regime_params(data["regime"]))
    return data
