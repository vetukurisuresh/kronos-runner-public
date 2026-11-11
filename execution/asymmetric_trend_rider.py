"""Asymmetric "small SL -> full trend capture" execution engine.

The desk gives the market a deliberately small, structure-derived risk and then
lets a paid-for runner stay in the trade while the live trend and order flow
remain aligned.  This module is pure: the session supplies the option LTP,
OBI_5, candle low and EMA; the engine returns every booked leg and the exact
canonical rupee fields.  Existing 40/30/30 and 50/25/25 managers are untouched
unless a trade explicitly carries an ``asymmetric_plan``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

T1_R_MULTIPLE = 1.8
T2_R_MULTIPLE = 3.2
T1_BOOK_FRACTION = 0.35
T2_BOOK_FRACTION = 0.35
RUNNER_FRACTION = 0.30
BREAKEVEN_OFFSET_INR = 1.50
MAX_INITIAL_RISK_PCT = 0.07
MIN_INITIAL_RISK_PCT = 0.06
TIME_SCRATCH_SECONDS = 90.0
TIME_SCRATCH_MIN_PROFIT_INR = 2.00
TIME_SCRATCH_OBI_FLOOR = 0.65
RUNNER_HOLD_OBI = 0.78
RUNNER_TRAIL_GIVEBACK_INR = 1.50
BROKERAGE_ROUND_TRIP_INR = 40.0
BROKERAGE_TURNOVER_RATE = 0.0005


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _as_ist(moment: datetime | str | float | int | None) -> datetime:
    if moment is None:
        return datetime.now(tz=IST)
    if isinstance(moment, (int, float)):
        return datetime.fromtimestamp(float(moment), tz=IST)
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def _bar_low(row: Any) -> float:
    if isinstance(row, Mapping):
        return _f(row.get("low") or row.get("l"))
    return _f(getattr(row, "low", 0.0))


def lot_split(qty: Any) -> dict[str, int]:
    """Split lots 35/35/remainder without ever inventing a contract.

    For a NIFTY lot of 75 this returns exactly ``26 / 26 / 23``.  Small
    quantities stay lot-aligned and the runner gets the residual, so the three
    legs always sum back to the original quantity.
    """
    total = max(0, int(_f(qty)))
    if total <= 0:
        return {"initial": 0, "t1": 0, "t2": 0, "runner": 0}
    t1 = int(math.floor(total * T1_BOOK_FRACTION))
    t2 = int(math.floor(total * T2_BOOK_FRACTION))
    if total >= 2 and t1 <= 0:
        t1 = 1
    if total >= 3 and t2 <= 0:
        t2 = 1
    if t1 + t2 >= total and total > 1:
        t2 = max(0, total - t1 - 1)
    if t1 >= total:
        t1 = max(1, total - 1)
    runner = max(0, total - t1 - t2)
    return {"initial": total, "t1": t1, "t2": t2, "runner": runner}


def initial_sl_for(entry_price: Any, ignition_low: Any) -> dict[str, float]:
    """Return a 6-7% maximum-risk stop anchored to the ignition candle.

    ``min(ignition_low - 1.50, entry * 0.93)`` can *widen* risk when the candle
    is far away.  The desk requirement is a cap, so the structure distance is
    clamped to the 6-7% band before the level is published.
    """
    entry = max(0.0, _f(entry_price))
    if entry <= 0:
        return {"entry": 0.0, "initial_sl": 0.0, "risk_points": 0.0, "risk_pct": 0.0}
    low = _f(ignition_low)
    structure_distance = (entry - low + BREAKEVEN_OFFSET_INR) if 0 < low < entry else entry * MAX_INITIAL_RISK_PCT
    risk = min(entry * MAX_INITIAL_RISK_PCT, max(entry * MIN_INITIAL_RISK_PCT, structure_distance))
    risk = round(max(0.01, risk), 2)
    sl = round(max(0.01, entry - risk), 2)
    return {
        "entry": round(entry, 2),
        "initial_sl": sl,
        "risk_points": round(entry - sl, 2),
        "risk_pct": round((entry - sl) / entry * 100.0, 3),
    }


def build_asymmetric_plan(
    *,
    entry_price: Any,
    ignition_low: Any = 0.0,
    qty: Any = 75,
    side: str = "CE",
    trigger_label: str = "",
    decision_latency_ms: Any = 0.0,
    candles_1m: list[Any] | None = None,
    created_at: datetime | str | float | int | None = None,
) -> dict[str, Any]:
    """Freeze the exact SL, BE, T1, T2 and runner policy for one trade."""
    entry = max(0.0, _f(entry_price))
    if 0 >= _f(ignition_low) and candles_1m:
        lows = [_bar_low(row) for row in candles_1m[-2:] if _bar_low(row) > 0]
        ignition_low = min(lows) if lows else ignition_low
    risk = initial_sl_for(entry, ignition_low)
    split = lot_split(qty)
    entry = risk["entry"]
    risk_points = risk["risk_points"]
    t1 = round(entry + risk_points * T1_R_MULTIPLE, 2) if entry > 0 else 0.0
    t2 = round(entry + risk_points * T2_R_MULTIPLE, 2) if entry > 0 else 0.0
    be_trigger = round(entry + risk_points, 2) if entry > 0 else 0.0
    be_sl = round(entry + BREAKEVEN_OFFSET_INR, 2) if entry > 0 else 0.0
    return {
        "model": "ASYMMETRIC_SMALL_SL_FULL_TREND",
        "side": str(side or "").upper(),
        "entry_price": entry,
        "ignition_low": round(_f(ignition_low), 2),
        "initial_sl": risk["initial_sl"],
        "risk_points": risk_points,
        "risk_pct": risk["risk_pct"],
        "breakeven_trigger": be_trigger,
        "breakeven_sl": be_sl,
        "t1_price": t1,
        "t2_price": t2,
        "t1_r_multiple": T1_R_MULTIPLE,
        "t2_r_multiple": T2_R_MULTIPLE,
        "t1_fraction": T1_BOOK_FRACTION,
        "t2_fraction": T2_BOOK_FRACTION,
        "runner_fraction": RUNNER_FRACTION,
        "initial_qty": split["initial"],
        "t1_qty": split["t1"],
        "t2_qty": split["t2"],
        "runner_qty": split["runner"],
        "time_scratch_seconds": TIME_SCRATCH_SECONDS,
        "time_scratch_obi_floor": TIME_SCRATCH_OBI_FLOOR,
        "runner_hold_obi": RUNNER_HOLD_OBI,
        "runner_trail_giveback_inr": RUNNER_TRAIL_GIVEBACK_INR,
        "trigger_label": str(trigger_label or ""),
        "decision_latency_ms": round(max(0.0, _f(decision_latency_ms)), 3),
        "created_at": _as_ist(created_at).isoformat(timespec="milliseconds"),
    }


def attach_plan_to_trade(trade: dict[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the plan to a mutable trade without changing the legacy path."""
    frozen = dict(plan or {})
    trade["asymmetric_plan"] = frozen
    trade["asymmetric_mode"] = True
    trade["initial_sl"] = frozen.get("initial_sl") or trade.get("initial_sl")
    trade["current_trailing_sl"] = frozen.get("initial_sl") or trade.get("current_trailing_sl")
    trade["risk_points"] = frozen.get("risk_points") or trade.get("risk_points")
    trade["target_1"] = frozen.get("t1_price") or trade.get("target_1")
    trade["target_2_p90"] = frozen.get("t2_price") or trade.get("target_2_p90")
    trade["breakeven_trigger"] = frozen.get("breakeven_trigger")
    trade["breakeven_sl"] = frozen.get("breakeven_sl")
    trade["asymmetric_rr"] = {
        "risk_inr": frozen.get("risk_points"),
        "risk_pct": frozen.get("risk_pct"),
        "t1": frozen.get("t1_price"),
        "t2": frozen.get("t2_price"),
        "runner": "UNCAPPED",
    }
    trade["trigger_label"] = frozen.get("trigger_label") or trade.get("trigger_label") or ""
    trade.setdefault("decision_latency_ms", frozen.get("decision_latency_ms"))
    return trade


def _mark_live(trade: dict[str, Any], price: float) -> float:
    entry = _f(trade.get("entry_price"))
    qty_open = int(trade.get("qty_open") or 0)
    realized = _f(trade.get("realized_pnl_rs"))
    pnl = realized + (price - entry) * qty_open
    trade["live_pnl_rs"] = round(pnl, 2)
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    trade["live_pnl_pct"] = round(pnl / (entry * initial) * 100.0, 2) if entry and initial else 0.0
    return round(pnl, 2)


def _book(
    trade: dict[str, Any],
    price: float,
    qty: int,
    leg_name: str,
    *,
    at: str,
) -> dict[str, Any] | None:
    qty = min(max(0, int(qty)), int(trade.get("qty_open") or 0))
    if qty <= 0 or price <= 0:
        return None
    entry = _f(trade.get("entry_price"))
    pnl = round((price - entry) * qty, 2)
    trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
    trade["realized_pnl_rs"] = round(_f(trade.get("realized_pnl_rs")) + pnl, 2)
    leg = {
        "leg": leg_name,
        "qty": qty,
        "price": round(price, 2),
        "pnl": pnl,
        "remaining": int(trade.get("qty_open") or 0),
        "stop": round(_f(trade.get("current_trailing_sl")), 2),
        "realized": round(_f(trade.get("realized_pnl_rs")), 2),
        "at": at,
    }
    trade.setdefault("booked_legs", []).append(leg)
    return leg


def _finalize(trade: dict[str, Any], price: float, reason: str, at: str) -> None:
    entry = _f(trade.get("entry_price"))
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    legs = list(trade.get("booked_legs") or [])
    turnover = entry * initial + sum(_f(leg.get("price")) * int(_f(leg.get("qty"))) for leg in legs)
    gross = round(_f(trade.get("realized_pnl_rs")), 2)
    costs = round(BROKERAGE_ROUND_TRIP_INR + max(0.0, turnover) * BROKERAGE_TURNOVER_RATE, 2)
    net = round(gross - costs, 2)
    trade["gross_pnl_rs"] = gross
    trade["brokerage_taxes"] = costs
    trade["costs"] = costs
    trade["net_pnl_rs"] = net
    trade["vaulted"] = round(max(0.0, net) * 0.50, 2)
    trade["outcome"] = "WIN" if net > 0 else "SL" if net < 0 else "BREAKEVEN"
    trade["status"] = "CLOSED"
    trade["qty_open"] = 0
    trade["exit_price"] = round(price, 2)
    trade["exit_reason"] = reason
    trade["exit_at"] = at
    trade["runner_active"] = False
    trade["asymmetric_stage"] = 5
    trade["asymmetric_stage_label"] = "RUNNER_EXITED" if reason.startswith("ASYM_RUNNER") else "CLOSED"
    trade["live_pnl_rs"] = gross


def manage_asymmetric_trade(
    trade: dict[str, Any],
    ltp: Any,
    *,
    obi_5: Any = 1.0,
    ema9: Any = 0.0,
    prior_1m_low: Any = 0.0,
    regime_trendy: bool = False,
    now: datetime | str | float | int | None = None,
    candles_1m: list[Any] | None = None,
) -> dict[str, Any]:
    """Manage one tick of the asymmetric trade.

    The order is deliberate: hard stop, time/flow scratch, +1R breakeven, T1,
    T2, then the uncapped order-flow runner.  A stop or scratch always wins over
    a simultaneous target, so the risk can never be re-opened.
    """
    moment = _as_ist(now)
    stamp = moment.isoformat(timespec="seconds")
    price = _f(ltp)
    plan = dict(trade.get("asymmetric_plan") or {})
    entry = _f(trade.get("entry_price") or plan.get("entry_price"))
    initial = int(trade.get("initial_qty") or trade.get("qty") or plan.get("initial_qty") or 0)
    trade.setdefault("initial_qty", initial)
    trade.setdefault("qty_open", initial)
    trade.setdefault("realized_pnl_rs", 0.0)
    trade.setdefault("booked_legs", [])
    trade.setdefault("breakeven_locked", False)
    trade.setdefault("runner_active", False)
    if not plan:
        plan = build_asymmetric_plan(
            entry_price=entry,
            ignition_low=_f(trade.get("initial_sl")),
            qty=initial,
            side=str(trade.get("option_type") or ""),
            trigger_label=str(trade.get("trigger_label") or ""),
            created_at=trade.get("entry_at") or moment,
        )
        trade["asymmetric_plan"] = plan

    risk = _f(plan.get("risk_points") or trade.get("risk_points"))
    if risk <= 0:
        risk = max(0.01, entry * MIN_INITIAL_RISK_PCT)
    t1 = _f(plan.get("t1_price") or trade.get("target_1"))
    t2 = _f(plan.get("t2_price") or trade.get("target_2_p90"))
    stop = _f(trade.get("current_trailing_sl") or plan.get("initial_sl"))
    t1_qty = int(plan.get("t1_qty") or trade.get("t1_qty") or lot_split(initial)["t1"])
    t2_qty = int(plan.get("t2_qty") or trade.get("t2_qty") or lot_split(initial)["t2"])
    flow_obi = _f(obi_5, 1.0)
    ema = _f(ema9)
    prior_low = _f(prior_1m_low)
    if not prior_low and candles_1m:
        lows = [_bar_low(row) for row in candles_1m[-2:] if _bar_low(row) > 0]
        prior_low = min(lows) if lows else 0.0
    result: dict[str, Any] = {
        "legs": [],
        "events": [],
        "status": str(trade.get("status") or "OPEN"),
        "exit": str(trade.get("status") or "").upper() == "CLOSED",
        "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
        "net_pnl_rs": round(_f(trade.get("net_pnl_rs")), 2),
        "remaining": int(trade.get("qty_open") or 0),
        "stage": int(trade.get("asymmetric_stage") or 1),
        "stage_label": str(trade.get("asymmetric_stage_label") or "ENTRY_FILLED"),
        "current_trailing_sl": round(stop, 2),
        "runner_active": bool(trade.get("runner_active")),
    }
    if price <= 0:
        return result
    if str(trade.get("status") or "").upper() != "OPEN":
        _mark_live(trade, price)
        result["total_pnl"] = round(_f(trade.get("realized_pnl_rs")), 2)
        return result
    trade.setdefault("asymmetric_stage", 1)
    trade.setdefault("asymmetric_stage_label", "ENTRY_FILLED")

    # 1) Hard stop / already-ratcheted runner stop.
    if stop > 0 and price <= stop:
        leg = _book(
            trade, price, int(trade.get("qty_open") or 0),
            "ASYM_SL" if not trade.get("t1_booked") else "ASYM_TRAIL_STOP",
            at=stamp,
        )
        if leg:
            result["legs"].append(leg)
            result["events"].append("SL_HIT")
        _finalize(trade, price, "ASYM_SL" if not trade.get("t1_booked") else "ASYM_TRAIL_STOP", stamp)
        result.update({
            "status": "CLOSED", "exit": True, "stage": 5,
            "stage_label": str(trade.get("asymmetric_stage_label") or "CLOSED"),
            "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
            "net_pnl_rs": round(_f(trade.get("net_pnl_rs")), 2),
            "remaining": 0,
        })
        return result

    # 2) Time and flow scratch: a stalled entry is cut before the full stop.
    entry_at = trade.get("entry_at") or plan.get("created_at")
    elapsed = max(0.0, (moment - _as_ist(entry_at)).total_seconds()) if entry_at else 0.0
    if (not trade.get("t1_booked")
            and elapsed <= TIME_SCRATCH_SECONDS
            and price < entry + TIME_SCRATCH_MIN_PROFIT_INR
            and flow_obi < TIME_SCRATCH_OBI_FLOOR):
        leg = _book(trade, price, int(trade.get("qty_open") or 0), "ASYM_FLOW_SCRATCH", at=stamp)
        if leg:
            result["legs"].append(leg)
            result["events"].append("TIME_FLOW_SCRATCH")
        _finalize(trade, price, "ASYM_FLOW_SCRATCH", stamp)
        result.update({
            "status": "CLOSED", "exit": True, "stage": 5,
            "stage_label": "FLOW_SCRATCH", "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
            "net_pnl_rs": round(_f(trade.get("net_pnl_rs")), 2), "remaining": 0,
        })
        return result

    # 3) Instant breakeven at +1R.
    if not trade.get("breakeven_locked") and price >= entry + risk:
        trade["current_trailing_sl"] = round(max(stop, entry + BREAKEVEN_OFFSET_INR), 2)
        trade["breakeven_locked"] = True
        trade["breakeven_at"] = round(price, 2)
        trade["asymmetric_stage"] = 2
        trade["asymmetric_stage_label"] = "BREAKEVEN_LOCKED"
        stop = _f(trade["current_trailing_sl"])
        result["events"].append("BREAKEVEN_LOCKED")
        result["stage"] = 2
        result["stage_label"] = "BREAKEVEN_LOCKED"

    # 4) T1: book 35% and keep the stop at cost+1.50.
    if not trade.get("t1_booked") and t1 > 0 and price >= t1:
        leg = _book(trade, price, t1_qty, "ASYM_T1_35PCT", at=stamp)
        if leg:
            result["legs"].append(leg)
            result["events"].append("T1_35PCT_BOOKED")
            trade["t1_booked"] = True
            trade["t1_fill"] = round(price, 2)
            trade["t1_qty"] = int(leg["qty"])
            trade["t1_at"] = stamp
            trade["current_trailing_sl"] = round(max(_f(trade.get("current_trailing_sl")), entry + BREAKEVEN_OFFSET_INR), 2)
            trade["asymmetric_stage"] = 3
            trade["asymmetric_stage_label"] = "T1_35PCT_BOOKED"
            result["stage"] = 3
            result["stage_label"] = "T1_35PCT_BOOKED"

    # 5) T2: book 35% and move the remaining stop to T1.
    if trade.get("t1_booked") and not trade.get("t2_booked") and t2 > 0 and price >= t2:
        leg = _book(trade, price, t2_qty, "ASYM_T2_35PCT", at=stamp)
        if leg:
            result["legs"].append(leg)
            result["events"].append("T2_35PCT_BOOKED")
            trade["t2_booked"] = True
            trade["t2_fill"] = round(price, 2)
            trade["t2_qty"] = int(leg["qty"])
            trade["t2_at"] = stamp
            trade["runner_active"] = int(trade.get("qty_open") or 0) > 0
            trade["current_trailing_sl"] = round(max(_f(trade.get("current_trailing_sl")), t1), 2)
            trade["asymmetric_stage"] = 4
            trade["asymmetric_stage_label"] = "T2_35PCT_RUNNER_30PCT"
            result["stage"] = 4
            result["stage_label"] = "T2_35PCT_RUNNER_30PCT"
            result["runner_active"] = bool(trade.get("runner_active"))

    # 6) Uncapped runner: hold above EMA-9 and OBI 0.78; ratchet under the
    # newly closed 1m/5m low minus 1.50.  The fixed targets never cap it.
    if trade.get("t2_booked") and int(trade.get("qty_open") or 0) > 0:
        stop = _f(trade.get("current_trailing_sl"))
        if prior_low > 0:
            ratchet = round(prior_low - RUNNER_TRAIL_GIVEBACK_INR, 2)
            if ratchet > stop:
                trade["current_trailing_sl"] = ratchet
                stop = ratchet
                result["events"].append("RUNNER_TRAIL_RATCHET")
        runner_reason = ""
        if ema > 0 and price < ema:
            runner_reason = "ASYM_RUNNER_EMA_BREAK"
        elif flow_obi < RUNNER_HOLD_OBI:
            runner_reason = "ASYM_RUNNER_OBI_BREAK"
        elif prior_low > 0 and price < prior_low:
            runner_reason = "ASYM_RUNNER_CANDLE_BREAK"
        trade["runner_hold"] = not runner_reason
        trade["runner_obi"] = round(flow_obi, 3)
        trade["runner_ema9"] = round(ema, 2)
        trade["runner_prior_low"] = round(prior_low, 2)
        trade["runner_mode"] = "TRENDY_5M" if regime_trendy else "1M"
        if runner_reason:
            leg = _book(trade, price, int(trade.get("qty_open") or 0), "ASYM_RUNNER_30PCT", at=stamp)
            if leg:
                result["legs"].append(leg)
                result["events"].append("RUNNER_EXITED")
            _finalize(trade, price, runner_reason, stamp)
            result.update({
                "status": "CLOSED", "exit": True, "stage": 5,
                "stage_label": "RUNNER_EXITED", "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
                "net_pnl_rs": round(_f(trade.get("net_pnl_rs")), 2), "remaining": 0,
            })
            return result

    _mark_live(trade, price)
    trade["asymmetric_stage_label"] = str(trade.get("asymmetric_stage_label") or "ENTRY_FILLED")
    result.update({
        "status": str(trade.get("status") or "OPEN"),
        "stage": int(trade.get("asymmetric_stage") or result["stage"]),
        "stage_label": str(trade.get("asymmetric_stage_label") or result["stage_label"]),
        "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
        "net_pnl_rs": round(_f(trade.get("net_pnl_rs")), 2),
        "remaining": int(trade.get("qty_open") or 0),
        "current_trailing_sl": round(_f(trade.get("current_trailing_sl")), 2),
        "runner_active": bool(trade.get("runner_active")),
        "runner_hold": bool(trade.get("runner_hold")),
    })
    trade["stage_pills"] = asymmetric_stage_pills(trade)
    return result


def asymmetric_stage_pills(trade: Mapping[str, Any]) -> list[dict[str, Any]]:
    risk = _f((trade.get("asymmetric_plan") or {}).get("risk_points") or trade.get("risk_points"))
    return [
        {"stage": 1, "label": "Entry Filled", "done": bool(trade.get("entry_price")),
         "value": f"INR {_f(trade.get('entry_price')):,.2f}"},
        {"stage": 2, "label": f"Small SL -{risk:,.2f}", "done": bool(trade.get("initial_sl")),
         "value": f"SL {_f(trade.get('initial_sl')):,.2f}"},
        {"stage": 3, "label": "BE at +1R", "done": bool(trade.get("breakeven_locked")),
         "value": f"SL {_f(trade.get('breakeven_sl')):,.2f}"},
        {"stage": 4, "label": "T1 35% / T2 35%", "done": bool(trade.get("t2_booked")),
         "value": f"T1 {_f(trade.get('target_1')):,.2f} / T2 {_f(trade.get('target_2_p90')):,.2f}"},
        {"stage": 5, "label": "30% Uncapped Runner", "done": bool(trade.get("t2_booked")),
         "value": "TRAILING" if int(_f(trade.get("qty_open"))) > 0 else "EXITED"},
    ]


__all__ = [
    "BREAKEVEN_OFFSET_INR",
    "MAX_INITIAL_RISK_PCT",
    "RUNNER_HOLD_OBI",
    "T1_R_MULTIPLE",
    "T2_R_MULTIPLE",
    "TIME_SCRATCH_SECONDS",
    "asymmetric_stage_pills",
    "attach_plan_to_trade",
    "build_asymmetric_plan",
    "initial_sl_for",
    "lot_split",
    "manage_asymmetric_trade",
]
