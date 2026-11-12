"""Continuous 09:16–15:00 IST option scalp engine.

This is intentionally a *pure* module: the live session supplies the clock,
spot/option micro-structure, 11-strike chain and futures velocity; the engine
returns a deterministic decision.  It is also the execution policy for the
dedicated ``SCALP_SLEEVE_CAPITAL`` book, separate from the macro sniper and the
older house-money adaptive scalper.

The five entry signatures are:
    1. time window 09:16–15:00 IST
    2. spot/option micro-volatility (with a hard dead-tape pause)
    3. tight option spread <= ₹0.35
    4. 1m ATP/EMA5 reclaim or p10/p90 ping-pong structure
    5. 5-level OBI + TBQ/TSQ + futures volume ignition
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any

from market.option_picker import itm_target_strike

IST = timezone(timedelta(hours=5, minutes=30))

SCALP_SLEEVE_CAPITAL = 35_000.0
MAX_SCALP_CAPITAL = 35_000.0
NIFTY_LOT_SIZE = 75
BANKNIFTY_LOT_SIZE = 35
SCALP_SESSION_START = time(9, 16)
SCALP_SESSION_END = time(15, 0)
MIN_SPOT_MOVE_NIFTY = 6.0
MIN_SPOT_MOVE_BANKNIFTY = 18.0
DEAD_SPOT_MOVE_NIFTY = 4.0
DEAD_SPOT_MOVE_BANKNIFTY = 12.0
MIN_OPTION_ATR = 2.20
MAX_SPREAD = 0.35
SPREAD_FLOOR = 0.75
SPREAD_PCT_OF_LTP = 0.004
MIN_OBI = 1.25
MIN_TBQ_TSQ = 1.25
MIN_FUTURES_VOLUME_RATIO = 1.00
ORDER_FLOW_IGNITION_OBI = 1.60
ORDER_FLOW_IGNITION_TBQ = 1.40
SMC_MIN_OBI = 1.10
MIN_DELTA = 0.35
MAX_DELTA = 0.75
PREFERRED_LTP_MIN = 70.0
PREFERRED_LTP_MAX = 145.0
EXPIRY_LTP_MIN = 35.0
EXPIRY_LTP_MAX = 95.0
COOLDOWN_SECONDS = 15
DEFAULT_SL_POINTS = 4.0
S1_LOCK_POINTS = 5.50
S1_BOOK_POINTS = 6.50
S2_POINTS = 10.0
TRAIL_GIVEBACK = 2.00

WAITING_MAIN_STATES = {
    "IDLE", "WAITING", "WAITING_FOR_PRICE_ZONE", "WAITING_FOR_STRUCTURE",
    "WAITING_FOR_ENTRY", "SCANNING", "READY",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def spread_cap_for_ltp(ltp: Any) -> float:
    """Institutional spread tolerance: max(₹0.75, 0.4% of premium)."""
    return round(max(SPREAD_FLOOR, SPREAD_PCT_OF_LTP * max(0.0, _f(ltp))), 2)


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


def _in_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    return SCALP_SESSION_START <= moment.time() <= SCALP_SESSION_END


def _main_is_waiting(main_state: Any) -> bool:
    text = str(main_state or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not text:
        return False
    if "TRADE" in text or text in {"ACTIVE", "MACRO", "PAUSED"}:
        return False
    return text in WAITING_MAIN_STATES or "WAIT" in text


def _above_all(price: float, *levels: float) -> bool:
    usable = [level for level in levels if level > 0]
    return bool(usable) and price > 0 and all(price >= level for level in usable)


def _below_all(price: float, *levels: float) -> bool:
    usable = [level for level in levels if level > 0]
    return bool(usable) and price > 0 and all(price <= level for level in usable)


def _structure_for(
    side: str,
    *,
    spot: float,
    spot_vwap: float,
    spot_ema5: float,
    p10: float,
    p90: float,
    kronos_slope: float,
    ce_ltp: float,
    ce_atp: float,
    pe_ltp: float,
    pe_atp: float,
) -> tuple[bool, str]:
    if side == "CE":
        reclaim = (
            _above_all(spot, spot_vwap, spot_ema5)
            and ce_atp > 0 and ce_ltp >= ce_atp and kronos_slope > 0
        )
        bounce = (
            p10 > 0 and spot <= p10 + 1.5
            and ce_atp > 0 and ce_ltp >= ce_atp
        )
        if reclaim:
            return True, "CE 1m ATP/EMA5 RECLAIM + KRONOS-MINI UP"
        if bounce:
            return True, "CE p10 PING-PONG BOUNCE"
        if not _above_all(spot, spot_vwap, spot_ema5):
            return False, "CE WAITING FOR ATP/EMA5 RECLAIM"
        if kronos_slope <= 0:
            return False, "CE KRONOS-MINI SLOPE NOT UP"
        return False, "CE OPTION BELOW ATP"
    reclaim = (
        _below_all(spot, spot_vwap, spot_ema5)
        and pe_atp > 0 and pe_ltp >= pe_atp and kronos_slope < 0
    )
    rejection = (
        p90 > 0 and spot >= p90 - 1.5
        and pe_atp > 0 and pe_ltp >= pe_atp
    )
    if reclaim:
        return True, "PE 1m ATP/EMA5 BREAK + KRONOS-MINI UP"
    if rejection:
        return True, "PE p90 REJECTION"
    if not _below_all(spot, spot_vwap, spot_ema5):
        return False, "PE WAITING FOR ATP/EMA5 BREAK"
    if kronos_slope >= 0:
        return False, "PE KRONOS-MINI SLOPE NOT DOWN"
    return False, "PE OPTION BELOW ATP"


def evaluate_continuous_scalp(
    now: datetime | str | None = None,
    *,
    symbol: str = "NIFTY",
    main_state: str = "IDLE",
    spot: Any = 0.0,
    spot_atr_1m: Any = 0.0,
    spot_range_1m: Any = 0.0,
    option_atr_1m: Any = 0.0,
    spread: Any = 0.0,
    spot_vwap: Any = 0.0,
    spot_ema5: Any = 0.0,
    p10: Any = 0.0,
    p90: Any = 0.0,
    kronos_slope: Any = 0.0,
    side_bias: str = "AUTO",
    ce_ltp: Any = 0.0,
    ce_atp: Any = 0.0,
    pe_ltp: Any = 0.0,
    pe_atp: Any = 0.0,
    ce_obi_5: Any = 0.0,
    ce_tbq: Any = 0.0,
    ce_tsq: Any = 0.0,
    pe_obi_5: Any = 0.0,
    pe_tbq: Any = 0.0,
    pe_tsq: Any = 0.0,
    futures_volume_ratio: Any = 0.0,
    cooldown_sec: Any = 0,
    engine_active: bool = False,
) -> dict[str, Any]:
    """Evaluate the five signatures and return a CE/PE scalp order or veto.

    ``side_bias="AUTO"`` evaluates both legs and picks the stronger ignition;
    an explicit ``CE``/``PE`` constrains the decision to that leg.  Prices and
    ratios are deliberately not guessed: missing inputs fail the relevant gate.
    """
    moment = _as_ist(now)
    symbol = str(symbol or "NIFTY").upper()
    bank = symbol == "BANKNIFTY"
    spot_px = _f(spot)
    spot_vol = max(_f(spot_atr_1m), _f(spot_range_1m))
    min_move = MIN_SPOT_MOVE_BANKNIFTY if bank else MIN_SPOT_MOVE_NIFTY
    dead_move = DEAD_SPOT_MOVE_BANKNIFTY if bank else DEAD_SPOT_MOVE_NIFTY
    spread = max(0.0, _f(spread))
    atr_opt = max(0.0, _f(option_atr_1m))
    slope = _f(kronos_slope)
    fut_ratio = max(0.0, _f(futures_volume_ratio))
    bias = str(side_bias or "AUTO").strip().upper()

    time_ok = _in_session(moment)
    main_ok = _main_is_waiting(main_state)
    dead_tape = 0.0 < spot_vol < dead_move
    volatility_ok = spot_vol >= min_move and atr_opt >= MIN_OPTION_ATR and not dead_tape
    spread_cap = spread_cap_for_ltp(max(_f(ce_ltp), _f(pe_ltp)))
    spread_ok = 0.0 < spread <= spread_cap

    candidates: list[dict[str, Any]] = []
    for side, ltp, atp, obi, tbq, tsq in (
        ("CE", _f(ce_ltp), _f(ce_atp), _f(ce_obi_5), _f(ce_tbq), _f(ce_tsq)),
        ("PE", _f(pe_ltp), _f(pe_atp), _f(pe_obi_5), _f(pe_tbq), _f(pe_tsq)),
    ):
        if bias not in {"AUTO", side}:
            continue
        structure_ok, structure_label = _structure_for(
            side, spot=spot_px, spot_vwap=_f(spot_vwap),
            spot_ema5=_f(spot_ema5), p10=_f(p10), p90=_f(p90),
            kronos_slope=slope, ce_ltp=_f(ce_ltp), ce_atp=_f(ce_atp),
            pe_ltp=_f(pe_ltp), pe_atp=_f(pe_atp),
        )
        ratio = (tbq / tsq) if tsq > 0 else 0.0
        trend_route = bool(
            structure_ok and obi >= MIN_OBI and ratio >= MIN_TBQ_TSQ
            and fut_ratio >= MIN_FUTURES_VOLUME_RATIO
        )
        ignition_route = bool(
            obi >= ORDER_FLOW_IGNITION_OBI and ratio >= ORDER_FLOW_IGNITION_TBQ
        )
        smc_route = bool(
            structure_ok and obi >= SMC_MIN_OBI
            and ("p10" in structure_label.upper() or "p90" in structure_label.upper())
        )
        flow_ok = bool(trend_route or ignition_route or smc_route)
        route = (
            "TREND_CONTINUATION" if trend_route else
            "ORDER_FLOW_IGNITION" if ignition_route else
            "SMC_REVERSAL" if smc_route else ""
        )
        candidates.append({
            "side": side,
            # The ₹35K engine is independent: a macro sniper position must
            # never turn AUTO into "TREND ONLY / WAITING" with 0 Qty.
            "trigger": bool(time_ok and volatility_ok and spread_ok and flow_ok
                            and not dead_tape and not engine_active
                            and _f(cooldown_sec) <= 0),
            "entry": round(ltp, 2),
            "atp": round(atp, 2),
            "obi_5": round(obi, 3),
            "tbq_tsq": round(ratio, 3),
            "structure_ok": structure_ok,
            "structure_label": structure_label,
            "trend_route": trend_route,
            "ignition_route": ignition_route,
            "smc_route": smc_route,
            "route": route,
            "flow_ok": flow_ok,
            "score": round(obi + min(ratio, 3.0) + max(slope, 0.0), 3),
        })

    chosen = next((c for c in candidates if c["trigger"]), None)
    if chosen is None and candidates:
        chosen = max(candidates, key=lambda c: (c["flow_ok"], c["structure_ok"], c["score"]))
    side = chosen["side"] if chosen else ""
    entry = chosen["entry"] if chosen else 0.0
    structure_ok = bool(chosen and chosen["structure_ok"])
    flow_ok = bool(chosen and chosen["flow_ok"])
    trigger = bool(chosen and chosen["trigger"])
    route = str((chosen or {}).get("route") or "")

    reason = "SCALP_READY"
    if engine_active:
        reason = "SCALP_ALREADY_OPEN"
    elif _f(cooldown_sec) > 0:
        reason = "POST_SCALP_COOLDOWN"
    elif not time_ok:
        reason = "OUTSIDE_SCALP_SESSION"
    elif dead_tape:
        reason = "DEAD_TAPE_AUTOPAUSE"
    elif spot_vol < min_move or atr_opt < MIN_OPTION_ATR:
        reason = "VOLATILITY_TOO_LOW"
    elif not spread_ok:
        reason = "SPREAD_TOO_WIDE"
    elif not flow_ok:
        reason = "NO_FLEXIBLE_ROUTE"
    elif not side:
        reason = "NO_DIRECTIONAL_SETUP"

    sl_points = round(min(4.50, max(3.50, atr_opt * 0.9)), 2) if atr_opt else DEFAULT_SL_POINTS
    conditions = [
        {"key": "time_window", "pass": time_ok,
         "value": f"{moment.strftime('%H:%M:%S')} IST" if time_ok else "outside 09:16–15:00"},
        {"key": "micro_volatility", "pass": volatility_ok,
         "value": f"spot {spot_vol:.2f}pt / option ATR ₹{atr_opt:.2f}"},
        {"key": "spread", "pass": spread_ok, "value": f"₹{spread:.2f} (max ₹{spread_cap:.2f})"},
        {"key": "structure", "pass": structure_ok,
         "value": (chosen or {}).get("structure_label") or "waiting for ATP/EMA5 or p10/p90"},
        {"key": "flow", "pass": flow_ok,
         "value": f"{route or 'NO ROUTE'} · OBI {float((chosen or {}).get('obi_5') or 0):.2f} · TBQ/TSQ {float((chosen or {}).get('tbq_tsq') or 0):.2f} · fut {fut_ratio:.2f}x"},
    ]
    state = "AUTO_SCALPING" if trigger else (
        "PAUSED_DEAD_TAPE" if dead_tape else
        "WAITING_FOR_SIGNATURES"
    )
    return {
        "trigger": trigger,
        "order": f"SCALP_BUY_{side}" if trigger else "WAIT",
        "side": side,
        "symbol": symbol,
        "entry": entry,
        "atp": float((chosen or {}).get("atp") or 0.0),
        "stop": round(entry - sl_points, 2) if entry else 0.0,
        "sl_points": sl_points,
        "target_1": round(entry + S1_BOOK_POINTS, 2) if entry else 0.0,
        "target_2": round(entry + S2_POINTS, 2) if entry else 0.0,
        "breakeven_trigger": round(entry + S1_LOCK_POINTS, 2) if entry else 0.0,
        "breakeven_stop": round(entry + 1.50, 2) if entry else 0.0,
        "conditions": conditions,
        "signature_count": sum(1 for condition in conditions if condition["pass"]),
        "all_five": all(condition["pass"] for condition in conditions),
        "reason": reason,
        "engine_state": state,
        "trigger_route": route,
        "flexible_routes": {
            "trend_continuation": bool((chosen or {}).get("trend_route")),
            "order_flow_ignition": bool((chosen or {}).get("ignition_route")),
            "smc_reversal": bool((chosen or {}).get("smc_route")),
        },
        "main_state": str(main_state or ""),
        "now_ist": moment.isoformat(timespec="seconds"),
        "dead_tape": dead_tape,
        "candidates": candidates,
        "diagnostics": {
            "spot": round(spot_px, 2),
            "spot_vol_1m": round(spot_vol, 2),
            "option_atr_1m": round(atr_opt, 2),
            "spread": round(spread, 2),
            "kronos_slope": round(slope, 3),
            "futures_volume_ratio": round(fut_ratio, 2),
            "p10": round(_f(p10), 2),
            "p90": round(_f(p90), 2),
        },
    }


def _leg(row: dict[str, Any], side: str) -> dict[str, Any]:
    key = "call_options" if side == "CE" else "put_options"
    return dict(row.get(key) or row.get(key.replace("_options", "")) or row.get("option") or {})


def _market(leg: dict[str, Any]) -> dict[str, Any]:
    return dict(leg.get("market_data") or leg.get("market") or {})


def _greeks(leg: dict[str, Any]) -> dict[str, Any]:
    return dict(leg.get("option_greeks") or leg.get("greeks") or {})


def select_scalp_contract(
    chain: list[dict[str, Any]] | None,
    side: str,
    *,
    capital: Any = MAX_SCALP_CAPITAL,
    lot_size: Any = NIFTY_LOT_SIZE,
    expiry_day: bool = False,
    max_spread: float | None = MAX_SPREAD,
    spot: Any = 0.0,
    strike_step: Any = None,
) -> dict[str, Any] | None:
    """Pick the most explosive in-the-money strike inside the hard cap.

    When ``spot`` is supplied the candidate set is restricted to the **1-step
    ITM** strike (and its immediate ITM neighbours), so the ₹35K sleeve can no
    longer buy a cheap OTM strike that merely scored well on premium distance.
    Without ``spot`` the legacy whole-chain scoring is preserved for callers
    that only have a chain.

    Quantity is lot-aligned and can never exceed ``MAX_SCALP_CAPITAL``.  A
    strike outside the preferred premium band is never substituted with a
    synthetic price; if the chain has no eligible contract the caller waits.
    """
    side = str(side or "").upper()
    if side not in {"CE", "PE"}:
        return None
    cap = max(0.0, min(_f(capital, MAX_SCALP_CAPITAL), MAX_SCALP_CAPITAL))
    lot = max(1, int(_f(lot_size, NIFTY_LOT_SIZE)))
    px = _f(spot)
    step = _f(strike_step) or (100.0 if px > 20_000 else 50.0)
    target = itm_target_strike(px, side, step) if px > 0 else 0.0
    low, high = (EXPIRY_LTP_MIN, EXPIRY_LTP_MAX) if expiry_day else (
        PREFERRED_LTP_MIN, PREFERRED_LTP_MAX
    )
    best: dict[str, Any] | None = None
    for row in chain or []:
        if not isinstance(row, dict):
            continue
        leg = _leg(row, side)
        if not leg:
            continue
        market = _market(leg)
        greeks = _greeks(leg)
        ltp = _f(leg.get("ltp") or market.get("ltp") or market.get("last_price")
                 or market.get("close") or leg.get("price"))
        delta = abs(_f(greeks.get("delta") or leg.get("delta") or market.get("delta")))
        gamma = abs(_f(greeks.get("gamma") or leg.get("gamma") or market.get("gamma")))
        bid = _f(market.get("bid_price") or market.get("bid") or leg.get("bid_price"))
        ask = _f(market.get("ask_price") or market.get("ask") or leg.get("ask_price"))
        spread = max(0.0, ask - bid) if bid > 0 and ask > 0 else 0.0
        allowed_spread = (spread_cap_for_ltp(ltp) if max_spread is None
                          else _f(max_spread, MAX_SPREAD))
        strike = _f(row.get("strike_price") or row.get("strike") or leg.get("strike_price"))
        if ltp <= 0 or spread > allowed_spread:
            continue
        # Hard ITM filter: a CE must sit below spot and a PE above it, and the
        # strike must be within three grid steps of the 1-step ITM target.
        if px > 0:
            itm_depth = (px - strike) if side == "CE" else (strike - px)
            if itm_depth <= 0:
                continue
            gap = abs(strike - target)
            if gap > step * 3.01:
                continue
        else:
            gap = 0.0
        qty = int(math.floor(cap / (ltp * lot))) * lot if ltp > 0 else 0
        if qty <= 0 and cap >= ltp * lot:
            qty = lot
        if qty <= 0:
            continue
        deployed = round(qty * ltp, 2)
        if deployed > MAX_SCALP_CAPITAL:
            continue
        preferred = bool(low <= ltp <= high and MIN_DELTA <= delta <= MAX_DELTA)
        # Distance-ranked ITM tier.  A flat bonus let the "preferred premium"
        # term pull the sleeve back to a nearly-ATM strike (delta 0.50) one grid
        # step away from the true 1-step ITM target, so the reward now shrinks
        # with every step of distance and always outweighs a premium preference.
        itm_tier = 0.0
        if px > 0:
            itm_steps = gap / step
            itm_tier = max(0.0, 60_000.0 - itm_steps * 20_000.0)
        # Gamma is the explosive component; delta proximity to 0.56 and a tight
        # spread break ties.  If no strike is inside the narrow preferred band,
        # a real affordable lot is still returned rather than 0 Qty.
        score = round(
            gamma * 10_000.0
            + itm_tier
            + (MAX_DELTA - abs(delta - 0.56)) * 100.0
            + (1.0 / (1.0 + spread))
            - abs(ltp - 105.0) / 100.0
            + (10_000.0 if preferred else 0.0),
            4,
        )
        candidate = {
            "side": side,
            "symbol": str(row.get("symbol") or row.get("underlying") or "NIFTY"),
            "strike": round(strike, 2),
            "instrument_key": str(leg.get("instrument_key") or ""),
            "option_symbol": str(leg.get("trading_symbol") or leg.get("tradingsymbol")
                                   or leg.get("instrument_key") or ""),
            "ltp": round(ltp, 2),
            "entry": round(ltp, 2),
            "delta": round(delta, 4),
            "gamma": round(gamma, 5),
            "spread": round(spread, 2),
            "spread_cap": round(allowed_spread, 2),
            "lot_size": lot,
            "qty": qty,
            "deployed": deployed,
            "capital": round(cap, 2),
            "score": score,
            "preferred_band": preferred,
            "min_one_lot_enforced": qty >= lot,
            "expiry_day": bool(expiry_day),
            "spot": round(px, 2) if px > 0 else None,
            "itm_target": round(target, 2) if target > 0 else None,
            "itm_gap_steps": round(gap / step, 3) if (px > 0 and step) else None,
            "selection_basis": "ONE_STEP_ITM" if (px > 0 and gap <= step * 1.01)
                               else ("ITM_NEIGHBOUR" if px > 0 else "CHAIN_SCORE"),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best


def init_scalp_trade(contract: dict[str, Any], *, now: datetime | str | None = None) -> dict[str, Any]:
    """Create the mutable paper/live trade dictionary for a selected contract."""
    entry = _f(contract.get("entry") or contract.get("ltp"))
    qty = int(_f(contract.get("qty")))
    lot = max(1, int(_f(contract.get("lot_size"), NIFTY_LOT_SIZE)))
    if qty <= 0 and entry > 0 and entry * lot <= MAX_SCALP_CAPITAL:
        qty = lot
    sl_points = round(min(4.50, max(3.50, _f(contract.get("sl_points"), DEFAULT_SL_POINTS))), 2)
    return {
        "status": "OPEN",
        "entry_price": round(entry, 2),
        "initial_qty": qty,
        "qty": qty,
        "qty_open": qty,
        "realized_pnl_rs": 0.0,
        "booked_legs": [],
        "sl_points": sl_points,
        "initial_sl": round(entry - sl_points, 2),
        "current_trailing_sl": round(entry - sl_points, 2),
        "target_1": round(entry + S1_BOOK_POINTS, 2),
        "target_2_p90": round(entry + S2_POINTS, 2),
        "scalp_s1_target": round(entry + S1_BOOK_POINTS, 2),
        "scalp_s2_target": round(entry + S2_POINTS, 2),
        "breakeven_trigger": round(entry + S1_LOCK_POINTS, 2),
        "peak_ltp": round(entry, 2),
        "breakeven_locked": False,
        "scalp_s1_done": False,
        "option_type": str(contract.get("side") or ""),
        "strike": _f(contract.get("strike")),
        "sleeve": "SCALP_35K",
        "continuous_scalp": True,
        "scalp_mode": True,
        "opened_at": _as_ist(now).isoformat(timespec="seconds"),
    }


def _mark_open(trade: dict[str, Any], price: float) -> float:
    entry = _f(trade.get("entry_price"))
    qty_open = int(trade.get("qty_open") or 0)
    realized = _f(trade.get("realized_pnl_rs"))
    pnl = realized + (price - entry) * qty_open
    trade["live_pnl_rs"] = round(pnl, 2)
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    trade["live_pnl_pct"] = round(pnl / (entry * initial) * 100.0, 2) if entry and initial else 0.0
    return round(pnl, 2)


def _close_qty(trade: dict[str, Any], price: float, qty: int, reason: str) -> dict[str, Any] | None:
    entry = _f(trade.get("entry_price"))
    qty = min(max(0, int(qty)), int(trade.get("qty_open") or 0))
    if qty <= 0:
        return None
    pnl = round((price - entry) * qty, 2)
    trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
    trade["realized_pnl_rs"] = round(_f(trade.get("realized_pnl_rs")) + pnl, 2)
    if trade["qty_open"] <= 0:
        trade["status"] = "CLOSED"
        trade["exit_reason"] = reason
        trade["exit_price"] = round(price, 2)
    leg = {
        "leg": reason,
        "qty": qty,
        "price": round(price, 2),
        "pnl": pnl,
        "remaining": int(trade.get("qty_open") or 0),
        "stop": round(_f(trade.get("current_trailing_sl")), 2),
        "realized": round(_f(trade.get("realized_pnl_rs")), 2),
    }
    trade.setdefault("booked_legs", []).append(leg)
    return leg


def manage_continuous_scalp(
    trade: dict[str, Any],
    ltp: Any,
    *,
    obi_5: Any = 1.0,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Manage the paid-to-free runner: 50% at +6.50, trail the rest by ₹2.

    A trade carrying the new asymmetric plan is delegated to the 35/35/30
    rider; legacy trades keep the exact 50% path covered by existing tests.
    """
    if trade.get("asymmetric_plan"):
        from execution.asymmetric_trend_rider import manage_asymmetric_trade

        return manage_asymmetric_trade(trade, ltp, obi_5=obi_5, now=now)
    entry = _f(trade.get("entry_price"))
    price = _f(ltp)
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    trade.setdefault("initial_qty", initial)
    trade.setdefault("qty_open", initial)
    trade.setdefault("realized_pnl_rs", 0.0)
    trade.setdefault("booked_legs", [])
    trade.setdefault("scalp_s1_done", False)
    trade.setdefault("breakeven_locked", False)
    trade.setdefault("peak_ltp", entry)
    trade.setdefault("current_trailing_sl", round(entry - _f(trade.get("sl_points"), DEFAULT_SL_POINTS), 2))
    result = {
        "legs": [], "stage1": False, "breakeven": False, "exit": False,
        "reason": "", "total_pnl": round(_f(trade.get("realized_pnl_rs")), 2),
        "remaining": int(trade.get("qty_open") or 0), "status": trade.get("status"),
    }
    if str(trade.get("status") or "").upper() != "OPEN" or entry <= 0 or price <= 0:
        return result

    trade["peak_ltp"] = max(_f(trade.get("peak_ltp"), entry), price)

    # Snap to Entry + ₹1.50 as soon as the broker round-trip is covered.
    if not trade.get("breakeven_locked") and price >= entry + S1_LOCK_POINTS:
        trade["current_trailing_sl"] = round(
            max(_f(trade.get("current_trailing_sl")), entry + 1.50), 2
        )
        trade["breakeven_locked"] = True
        result["breakeven"] = True

    # Book half at +₹6.50, not a point earlier.
    if not trade.get("scalp_s1_done") and price >= entry + S1_BOOK_POINTS:
        book_qty = min(int(trade.get("qty_open") or 0), max(1, int(round(initial * 0.50))))
        leg = _close_qty(trade, price, book_qty, "SCALP_S1_50PCT")
        if leg:
            result["legs"].append(leg)
            result["stage1"] = True
        trade["scalp_s1_done"] = True
        trade["current_trailing_sl"] = round(
            max(_f(trade.get("current_trailing_sl")), entry + 1.50), 2
        )

    # After the first book, the remaining half trails the peak by ₹2.00.
    if trade.get("scalp_s1_done") and int(trade.get("qty_open") or 0) > 0:
        trail = round(_f(trade.get("peak_ltp")) - TRAIL_GIVEBACK, 2)
        trade["current_trailing_sl"] = round(
            max(_f(trade.get("current_trailing_sl")), trail), 2
        )
        if _f(obi_5) < 0.80:
            leg = _close_qty(trade, price, int(trade.get("qty_open") or 0), "SCALP_OBI_FLIP")
            if leg:
                result["legs"].append(leg)
            result["exit"] = True
            result["reason"] = "SCALP_OBI_FLIP"

    if int(trade.get("qty_open") or 0) > 0:
        stop = _f(trade.get("current_trailing_sl"))
        if stop > 0 and price <= stop:
            reason = "SCALP_TRAIL_STOP" if int(trade.get("realized_pnl_rs") or 0) > 0 else "SCALP_SL"
            leg = _close_qty(trade, price, int(trade.get("qty_open") or 0), reason)
            if leg:
                result["legs"].append(leg)
            result["exit"] = True
            result["reason"] = reason

    trade["live_pnl_rs"] = _mark_open(trade, price)
    result["total_pnl"] = round(_f(trade.get("realized_pnl_rs")), 2)
    result["remaining"] = int(trade.get("qty_open") or 0)
    result["status"] = str(trade.get("status") or "OPEN")
    return result


def detect_scalp_flip(
    active_side: str,
    *,
    ce_obi_5: Any = 0.0,
    pe_obi_5: Any = 0.0,
    ce_tbq: Any = 0.0,
    ce_tsq: Any = 0.0,
    pe_tbq: Any = 0.0,
    pe_tsq: Any = 0.0,
    profit_booked: bool = True,
    cooldown_sec: Any = 0,
) -> dict[str, Any]:
    """Detect a paid CE→PE / PE→CE flow reversal after the first book.

    The 15-second cooldown belongs to the engine; a flip never fires merely
    because the opposite book is green.  The opposite 5-level book must carry
    the full OBI + TBQ/TSQ ignition.
    """
    active = str(active_side or "").upper()
    if active not in {"CE", "PE"}:
        return {"flip": False, "side": "", "reason": "NO_ACTIVE_SIDE"}
    if not profit_booked:
        return {"flip": False, "side": "", "reason": "PROFIT_NOT_BOOKED"}
    if _f(cooldown_sec) > 0:
        return {"flip": False, "side": "", "reason": "FLIP_COOLDOWN",
                "cooldown_sec": int(_f(cooldown_sec))}
    target = "PE" if active == "CE" else "CE"
    obi = _f(pe_obi_5 if target == "PE" else ce_obi_5)
    tbq = _f(pe_tbq if target == "PE" else ce_tbq)
    tsq = _f(pe_tsq if target == "PE" else ce_tsq)
    ratio = (tbq / tsq) if tsq > 0 else 0.0
    ok = obi >= MIN_OBI and ratio >= MIN_TBQ_TSQ
    return {
        "flip": ok,
        "side": target,
        "reason": f"{target}_FLOW_REVERSAL" if ok else "OPPOSITE_FLOW_NOT_IGNITED",
        "obi_5": round(obi, 3),
        "tbq_tsq": round(ratio, 3),
        "cooldown_sec": 0 if ok else int(_f(cooldown_sec)),
    }


@dataclass
class ContinuousScalpEngine:
    """Small stateful wrapper for replay/session integration."""

    capital: float = SCALP_SLEEVE_CAPITAL
    cooldown_seconds: int = COOLDOWN_SECONDS
    active: dict[str, Any] | None = None
    last_decision: dict[str, Any] = field(default_factory=dict)
    last_exit_at: datetime | None = None
    last_flip_at: datetime | None = None

    def cooldown_remaining(self, now: datetime | str | None = None) -> int:
        if not self.last_exit_at:
            return 0
        moment = _as_ist(now)
        return max(0, int(math.ceil((self.last_exit_at + timedelta(seconds=self.cooldown_seconds) - moment).total_seconds())))

    def evaluate(self, now: datetime | str | None = None, **kwargs: Any) -> dict[str, Any]:
        decision = evaluate_continuous_scalp(
            now,
            engine_active=bool(self.active),
            cooldown_sec=self.cooldown_remaining(now),
            **kwargs,
        )
        self.last_decision = decision
        return decision

    def open(self, contract: dict[str, Any], *, now: datetime | str | None = None) -> dict[str, Any]:
        if self.active:
            return {"ok": False, "reason": "SCALP_ALREADY_OPEN"}
        contract = dict(contract)
        if not contract.get("qty"):
            lot = max(1, int(_f(contract.get("lot_size"), NIFTY_LOT_SIZE)))
            qty = int(math.floor(self.capital / (_f(contract.get("ltp")) * lot))) * lot
            contract["qty"] = max(lot, qty) if _f(contract.get("ltp")) * lot <= self.capital else qty
        self.active = init_scalp_trade(contract, now=now)
        return {"ok": True, "trade": self.active}

    def manage(self, ltp: Any, *, obi_5: Any = 1.0,
               now: datetime | str | None = None) -> dict[str, Any]:
        if not self.active:
            return {"exit": False, "reason": "NO_ACTIVE_SCALP", "legs": []}
        result = manage_continuous_scalp(self.active, ltp, obi_5=obi_5, now=now)
        if result.get("exit"):
            self.last_exit_at = _as_ist(now)
        return result

    def flip(self, **kwargs: Any) -> dict[str, Any]:
        active_side = str((self.active or {}).get("option_type") or "")
        result = detect_scalp_flip(active_side, **kwargs)
        if result.get("flip"):
            self.last_flip_at = _as_ist(kwargs.get("now"))
        return result

    def snapshot(self) -> dict[str, Any]:
        trade = dict(self.active or {})
        return {
            "engine_state": "ACTIVE" if trade else "WAITING",
            "trade": trade,
            "last_decision": dict(self.last_decision),
            "last_exit_at": self.last_exit_at.isoformat() if self.last_exit_at else "",
        }


__all__ = [
    "ContinuousScalpEngine",
    "DEAD_SPOT_MOVE_BANKNIFTY",
    "DEAD_SPOT_MOVE_NIFTY",
    "MAX_SCALP_CAPITAL",
    "MIN_OPTION_ATR",
    "SCALP_SLEEVE_CAPITAL",
    "detect_scalp_flip",
    "evaluate_continuous_scalp",
    "init_scalp_trade",
    "manage_continuous_scalp",
    "select_scalp_contract",
]
