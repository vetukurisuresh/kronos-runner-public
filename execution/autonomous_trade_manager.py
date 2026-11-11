"""Zero-touch four-stage profit ratchet for the four-angle desk.

The existing :func:`execution.pro_money_manager.book_partials` books 50% at T1
and then books *all* remaining quantity at T2.  The institutional runner wants
50% / 25% / 25%, so this manager owns that exact lifecycle while reusing the
battle-tested T1 booking, trailing-stop and settlement helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from execution.pro_money_manager import (
    book_partials,
    close_remaining,
    init_partial_state,
    _mark_open,
)
from market.option_picker import new_trade

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
MICRO_SHIELD_PROGRESS = 0.40
MICRO_SHIELD_RISK_FRACTION = 0.25
T1_BOOK_FRACTION = 0.50
T2_BOOK_FRACTION = 0.25
RUNNER_FRACTION = 0.25
RUNNER_HOLD_OBI = 0.85
RUNNER_EXIT_OBI = 0.70


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stage_name(stage: int) -> str:
    return {
        1: "ENTRY_FILLED",
        2: "MICRO_SHIELD_ARMED",
        3: "T1_50PCT_BOOKED",
        4: "T2_RUNNER_TRAILING",
        5: "RUNNER_EXITED",
    }.get(stage, "WATCHING")


def _stage_pills(trade: Mapping[str, Any]) -> list[dict[str, Any]]:
    t1 = bool(trade.get("t1_booked"))
    shield = bool(trade.get("micro_shield_armed")) or t1
    t2 = bool(trade.get("t2_booked"))
    runner = bool(trade.get("runner_active")) or t2
    closed = str(trade.get("status") or "").upper() == "CLOSED"
    return [
        {"stage": 1, "label": "1. Entry Filled", "done": bool(trade.get("entry_price")), "value": f"₹{_f(trade.get('entry_price')):,.2f}"},
        {"stage": 2, "label": "2. Shield Armed", "done": shield, "value": f"SL ₹{_f(trade.get('current_trailing_sl')):,.2f}"},
        {"stage": 3, "label": "3. T1 50% Booked", "done": t1, "value": f"₹{_f(trade.get('target_1')):,.2f}"},
        {"stage": 4, "label": "4. T2 Runner Trailing", "done": t2 or runner, "value": ("EXITED" if closed else f"₹{_f(trade.get('target_2_p90')):,.2f}")},
    ]


def create_autonomous_trade(
    playbook: Mapping[str, Any] | None,
    *,
    qty: int | None = None,
    entry_at: str | None = None,
) -> dict[str, Any]:
    """Freeze the winning playbook into a live trade dictionary.

    ``playbook`` is the return value of
    :func:`strategy.multi_angle_playbook_engine.evaluate_playbooks`.  The order
    proposal is only used when the engine explicitly marked it executable.
    """
    data = playbook or {}
    if not data.get("auto_execute"):
        raise ValueError("playbook is not executable")
    order = data.get("entry") or {}
    entry = _f(order.get("entry_price"))
    risk = _f(order.get("risk_points"))
    strike = _f(order.get("strike"))
    side = str(order.get("option_type") or data.get("side") or "").upper()
    if entry <= 0 or risk <= 0 or side not in {"CE", "PE"}:
        raise ValueError("playbook entry is missing a positive price/risk/side")
    trade = new_trade(entry, risk, qty=int(qty or order.get("qty") or 300))
    init_partial_state(trade)
    trade.update({
        "trade_id": f"AUTO-{data.get('winner_id') or order.get('playbook_id') or '4ANGLE'}",
        "entry_at": entry_at or datetime.now(tz=IST).isoformat(timespec="seconds"),
        "option_type": side,
        "side": side,
        "strike": strike,
        "option_symbol": str(order.get("contract") or "").upper(),
        "strike_symbol": str(order.get("contract") or "").upper(),
        "playbook_id": str(data.get("winner_id") or order.get("playbook_id") or ""),
        "playbook_label": str((data.get("winner") or {}).get("short") or order.get("playbook_label") or ""),
        "playbook_angle": int((data.get("winner") or {}).get("index") or order.get("playbook_index") or 0),
        "playbook_score": _f((data.get("winner") or {}).get("score") or order.get("score")),
        "auto_executed": True,
        "sniper_grade": "AUTO_4ANGLE",
        "target_1": _f(order.get("target_1")) or trade["target_1"],
        "target_2_p90": _f(order.get("target_2")) or trade["target_2_p90"],
        "risk_points": round(risk, 2),
        "initial_sl": _f(order.get("stop_loss")) or trade["initial_sl"],
        "current_trailing_sl": _f(order.get("stop_loss")) or trade["current_trailing_sl"],
        "micro_shield_armed": False,
        "runner_active": False,
        "auto_stage": 1,
        "auto_stage_label": _stage_name(1),
        "entry_context": dict(order.get("entry_context") or {}),
    })
    trade["stage_pills"] = _stage_pills(trade)
    _mark_open(trade, entry)
    return trade


def _book_t2(trade: dict[str, Any], ltp: float) -> dict[str, Any] | None:
    """Book 25% of the original quantity at T2, keeping a 25% runner."""
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    open_qty = int(trade.get("qty_open") or 0)
    qty = min(open_qty, max(1, int(round(initial * T2_BOOK_FRACTION))))
    if qty <= 0:
        return None
    entry = _f(trade.get("entry_price"))
    target2 = _f(trade.get("target_2_p90") or trade.get("target_2"))
    fill = max(float(ltp), target2) if target2 > 0 else float(ltp)
    pnl = round((fill - entry) * qty, 2)
    trade["qty_open"] = open_qty - qty
    trade["realized_pnl_rs"] = round(_f(trade.get("realized_pnl_rs")) + pnl, 2)
    trade["t2_booked"] = True
    trade["runner_active"] = True
    trade["gear_runner"] = 5
    trade["tsl_gear"] = 5
    trade["current_trailing_sl"] = round(
        max(_f(trade.get("current_trailing_sl")), entry + 1.50), 2
    )
    leg = {
        "leg": "T2_RUNNER",
        "qty": qty,
        "price": round(fill, 2),
        "pnl": pnl,
        "remaining": trade["qty_open"],
        "stop": trade["current_trailing_sl"],
        "realized": trade["realized_pnl_rs"],
        "booked_fraction": T2_BOOK_FRACTION,
    }
    trade.setdefault("booked_legs", []).append(leg)
    return leg


def manage_autonomous_trade(
    trade: dict[str, Any],
    ltp: Any,
    *,
    obi_5: Any = 1.0,
    ema5: Any = 0.0,
    prior_1m_low: Any = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Run the entire four-stage lifecycle once per tick.

    Stage 1 is the entry already frozen by :func:`create_autonomous_trade`.
    This function then arms the micro-shield, books T1/T2 and owns the final
    order-flow runner.  It never lowers a stop and never books more than the
    original allocation.
    """
    init_partial_state(trade)
    price = _f(ltp)
    obi = _f(obi_5, 1.0)
    ema = _f(ema5)
    prior_low = _f(prior_1m_low)
    result: dict[str, Any] = {
        "legs": [], "events": [], "stage": int(trade.get("auto_stage") or 1),
        "stage_label": str(trade.get("auto_stage_label") or "ENTRY_FILLED"),
        "status": str(trade.get("status") or "OPEN"),
        "realized_pnl_rs": round(_f(trade.get("realized_pnl_rs")), 2),
        "qty_open": int(trade.get("qty_open") or 0),
        "runner_active": bool(trade.get("runner_active")),
    }
    if price <= 0:
        result["stage_pills"] = _stage_pills(trade)
        return result
    if str(trade.get("status") or "").upper() != "OPEN":
        result.update({
            "stage": int(trade.get("auto_stage") or 5),
            "stage_label": str(trade.get("auto_stage_label") or "CLOSED"),
            "stage_pills": _stage_pills(trade),
        })
        return result

    entry = _f(trade.get("entry_price"))
    risk = _f(trade.get("risk_points") or (entry - _f(trade.get("initial_sl"))))
    t1 = _f(trade.get("target_1"))
    t2 = _f(trade.get("target_2_p90") or trade.get("target_2"))
    stop = _f(trade.get("current_trailing_sl"))

    # Hard stop first.  A shield/trail can only improve this stop.
    if stop > 0 and price <= stop:
        leg = close_remaining(trade, price, reason="AUTO_SL")
        if leg:
            result["legs"].append(leg)
            result["events"].append("SL_HIT")
        trade["auto_stage"] = 5
        trade["auto_stage_label"] = _stage_name(5)
        result.update({
            "stage": 5, "stage_label": trade["auto_stage_label"],
            "status": str(trade.get("status") or "CLOSED"),
            "realized_pnl_rs": round(_f(trade.get("realized_pnl_rs")), 2),
            "qty_open": int(trade.get("qty_open") or 0),
            "stage_pills": _stage_pills(trade),
        })
        trade["stage_pills"] = result["stage_pills"]
        return result

    # Stage 2: at +40% of the distance to T1, lock the stop above the original
    # risk.  This prevents a winner from turning into a full stop-out.
    shield_level = entry + max(0.0, t1 - entry) * MICRO_SHIELD_PROGRESS
    if (not trade.get("micro_shield_armed") and t1 > entry and price >= shield_level):
        shield_stop = entry - max(0.0, risk) * MICRO_SHIELD_RISK_FRACTION
        trade["current_trailing_sl"] = round(max(stop, shield_stop), 2)
        trade["micro_shield_armed"] = True
        trade["micro_shield_level"] = round(shield_level, 2)
        trade["micro_shield_at"] = round(price, 2)
        result["events"].append("MICRO_SHIELD_ARMED")
        result["stage"] = 2
        result["stage_label"] = _stage_name(2)

    # Stage 3: reuse the canonical T1 booking (50%, stop Entry+₹1.50).  The
    # ride_wave flag stops it from also dumping the remaining quantity at T2.
    if not trade.get("t1_booked") and t1 > entry and price >= t1:
        legs = book_partials(trade, price, ride_wave=True)
        result["legs"].extend(legs)
        if legs:
            trade["micro_shield_armed"] = True
            trade["runner_active"] = True
            trade["auto_stage"] = 3
            trade["auto_stage_label"] = _stage_name(3)
            result["events"].append("T1_50PCT_BOOKED")
            result["stage"] = 3
            result["stage_label"] = trade["auto_stage_label"]

    # Stage 4a: harvest 25% at T2, leave the final 25% to the Gear-5 runner.
    if trade.get("t1_booked") and not trade.get("t2_booked") and t2 > entry and price >= t2:
        leg = _book_t2(trade, price)
        if leg:
            result["legs"].append(leg)
            result["events"].append("T2_25PCT_BOOKED")
            trade["auto_stage"] = 4
            trade["auto_stage_label"] = _stage_name(4)
            result["stage"] = 4
            result["stage_label"] = trade["auto_stage_label"]

    # Stage 4b: order-flow ratchet.  Hold the final quarter while OBI_5 stays
    # at/above 0.85 and LTP holds the 1m EMA-5; exit on loss of 0.70 or a break
    # of the prior 1m low.  Before T1 the same exit can protect a stalled entry.
    runner_now = bool(trade.get("runner_active")) or trade.get("t2_booked")
    low_break = prior_low > 0 and price < prior_low
    of_exit = obi < RUNNER_EXIT_OBI or low_break
    if runner_now and of_exit and int(trade.get("qty_open") or 0) > 0:
        leg = close_remaining(trade, price, reason="ORDER_FLOW_RUNNER_EXIT")
        if leg:
            result["legs"].append(leg)
            result["events"].append("ORDER_FLOW_RUNNER_EXIT")
        trade["auto_stage"] = 5
        trade["auto_stage_label"] = _stage_name(5)
        result["stage"] = 5
        result["stage_label"] = trade["auto_stage_label"]
    elif trade.get("t2_booked"):
        # Publish the exact hold/exit evidence for the UI.  This is not a
        # synthetic indicator: it is the same OBI and EMA the decision used.
        trade["runner_hold"] = bool(obi >= RUNNER_HOLD_OBI and (ema <= 0 or price >= ema))
        trade["runner_obi"] = round(obi, 3)
        trade["runner_ema5"] = round(ema, 2)
        trade["runner_prior_low"] = round(prior_low, 2)
        result["stage"] = 4
        result["stage_label"] = _stage_name(4)
        trade["auto_stage"] = 4
        trade["auto_stage_label"] = _stage_name(4)

    if int(trade.get("qty_open") or 0) <= 0 and str(trade.get("status") or "").upper() == "CLOSED":
        trade["auto_stage"] = 5
        trade["auto_stage_label"] = _stage_name(5)

    _mark_open(trade, price)
    trade["stage_pills"] = _stage_pills(trade)
    result.update({
        "stage": int(trade.get("auto_stage") or result["stage"]),
        "stage_label": str(trade.get("auto_stage_label") or result["stage_label"]),
        "status": str(trade.get("status") or "OPEN"),
        "realized_pnl_rs": round(_f(trade.get("realized_pnl_rs")), 2),
        "qty_open": int(trade.get("qty_open") or 0),
        "current_trailing_sl": round(_f(trade.get("current_trailing_sl")), 2),
        "runner_active": bool(trade.get("runner_active")),
        "runner_hold": bool(trade.get("runner_hold")),
        "stage_pills": trade["stage_pills"],
        "entry": round(entry, 2),
        "target_1": round(t1, 2),
        "target_2": round(t2, 2),
    })
    return result


__all__ = [
    "MICRO_SHIELD_PROGRESS",
    "RUNNER_EXIT_OBI",
    "RUNNER_HOLD_OBI",
    "T1_BOOK_FRACTION",
    "T2_BOOK_FRACTION",
    "create_autonomous_trade",
    "manage_autonomous_trade",
]
