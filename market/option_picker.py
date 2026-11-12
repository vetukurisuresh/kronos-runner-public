"""Pure helpers: ATM strike selection, CE/PE side, and the 4-gear trailing stop.

Kept free of network code so the terminal's most safety-critical numbers are
unit-testable.
"""

from __future__ import annotations

from typing import Any

from config.settings import (
    ITM_DELTA_HIGH,
    ITM_DELTA_LOW,
    ITM_DELTA_TARGET,
    index_strike_step,
)


def pick_atm(rows: list[dict], spot: float) -> dict | None:
    """Nearest strike to spot (ties resolve to the lower strike)."""
    best: tuple[float, float, dict] | None = None
    for row in rows or []:
        try:
            strike = float(row.get("strike_price"))
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        distance = abs(strike - spot)
        if best is None or (distance, strike) < (best[0], best[1]):
            best = (distance, strike, row)
    return best[2] if best else None


def choose_side(paths_up: int | None, samples: int | None, action: str = "") -> str:
    """Directional bias: explicit plan action wins, else the Monte Carlo majority."""
    if action == "BUY_PE":
        return "PE"
    if action == "BUY_CE":
        return "CE"
    if paths_up is not None and samples:
        return "CE" if paths_up >= samples / 2 else "PE"
    return "CE"


def choose_watch_side(
    *,
    active_trade: dict | None = None,
    manual: str = "AUTO",
    upside: float | None = None,
    downside: float | None = None,
    pcr: float | None = None,
    spot: float | None = None,
    vwap: float | None = None,
    obi_5: float | None = None,
    smc_side: str = "",
    fallback: str = "CE",
) -> str:
    """Pick the option leg the desk should watch right now.

    Priority is deliberate: an open position is immutable; a human pin wins
    while flat; only then does the live directional/flow bias pick the leg.
    A bearish model with a put-heavy chain, price below VWAP or a weak book
    therefore watches an ATM PE instead of a stale CE.  This function only
    selects the *watch* contract; it never creates an order.
    """
    trade = active_trade if isinstance(active_trade, dict) else {}
    if str(trade.get("status") or "").upper() == "OPEN":
        pinned = str(trade.get("option_type") or trade.get("side") or "").upper()
        if pinned in {"CE", "PE"}:
            return pinned
    pin = str(manual or "AUTO").upper()
    if pin in {"CE", "PE"}:
        return pin

    def _f(value: float | None, default: float = 0.0) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    up = _f(upside)
    down = _f(downside)
    ratio = _f(pcr)
    px = _f(spot)
    avg = _f(vwap)
    book = _f(obi_5)
    smc = str(smc_side or "").upper()
    below_vwap = bool(px > 0 and avg > 0 and px < avg)
    above_vwap = bool(px > 0 and avg > 0 and px >= avg)
    put_chain = 0.0 < ratio < 0.82
    call_chain = ratio > 1.05

    if down > up and (put_chain or below_vwap or (0.0 < book < 0.90) or smc == "PE"):
        return "PE"
    if up >= down and (call_chain or above_vwap or book >= 1.05 or smc == "CE"):
        return "CE"
    if put_chain and below_vwap:
        return "PE"
    if call_chain and above_vwap:
        return "CE"
    return "PE" if str(fallback).upper() == "PE" else "CE"


def contract_label(symbol: str, strike: float | int | None, side: str) -> str:
    """Always return a real contract label; never ``NIFTY 0 CE``."""
    try:
        number = int(float(strike or 0))
    except (TypeError, ValueError):
        number = 0
    leg = "PE" if str(side).upper() == "PE" else "CE"
    return f"{symbol} {number} {leg}" if number > 0 else f"{symbol} ATM {leg}"


def chain_greek(row: dict, side: str, name: str = "delta") -> float:
    """Read one Greek from either leg of an option-chain row.

    Upstox publishes ``call_options``/``put_options`` with ``option_greeks``
    nested inside ``market_data``'s sibling object.  A missing Greek is 0.0, never
    a guessed value, so the delta band stays an honest filter.
    """
    leg = (row or {}).get("call_options" if str(side).upper() == "CE" else "put_options")
    if not isinstance(leg, dict):
        return 0.0
    greeks = leg.get("option_greeks") or (leg.get("market_data") or {}).get("option_greeks") or {}
    if not isinstance(greeks, dict):
        return 0.0
    try:
        return float(greeks.get(name) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def pick_itm_strike(
    rows: list[dict],
    spot: float,
    side: str,
    *,
    symbol: str = "NIFTY",
    strike_step: float | None = None,
    delta_low: float = ITM_DELTA_LOW,
    delta_high: float = ITM_DELTA_HIGH,
    delta_target: float = ITM_DELTA_TARGET,
) -> dict[str, Any] | None:
    """Select the 1-strike ITM contract an institutional buyer would hold.

    For a bullish ``CE`` the strike one grid step *below* spot carries real
    intrinsic value (delta ~0.54-0.66) instead of the 0.50 coin-flip of ATM, so
    afternoon theta decay is smaller and the move pays from the first tick.  A
    bearish ``PE`` mirrors it: one step *above* spot.

    The Greek band is a preference, not a veto.  When the chain has not yet
    streamed Greeks the structural strike is still returned with
    ``delta_source="STRUCTURAL"`` so the desk can render the contract; the
    candle/quorum gate remains the only thing that can authorise an order.
    """
    leg_side = "PE" if str(side).upper() == "PE" else "CE"
    px = float(spot or 0.0)
    if px <= 0:
        return None
    step = float(strike_step or index_strike_step(symbol)) or 50.0
    target = px - step if leg_side == "CE" else px + step
    candidates: list[tuple[float, float, float, dict]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            strike = float(row.get("strike_price") or row.get("strike") or 0.0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        # Intrinsic value must be positive on the selected leg.
        if leg_side == "CE" and strike >= px:
            continue
        if leg_side == "PE" and strike <= px:
            continue
        delta = abs(chain_greek(row, leg_side, "delta"))
        candidates.append((abs(strike - target), strike, delta, row))
    if not candidates:
        return None
    in_band = [c for c in candidates if delta_low <= c[2] <= delta_high]
    if in_band:
        chosen = min(in_band, key=lambda c: (abs(c[2] - delta_target), c[0]))
        source = "GREEK_BAND"
    else:
        chosen = min(candidates, key=lambda c: c[0])
        source = "STRUCTURAL"
    _, strike, delta, row = chosen
    intrinsic = round(max(0.0, px - strike if leg_side == "CE" else strike - px), 2)
    return {
        "symbol": symbol,
        "side": leg_side,
        "strike": round(strike, 2),
        "contract": contract_label(symbol, strike, leg_side),
        "spot": round(px, 2),
        "strike_step": step,
        "itm_steps": 1,
        "intrinsic_points": intrinsic,
        "delta": round(delta, 4),
        "delta_band": [delta_low, delta_high],
        "delta_source": source,
        "row": row,
    }


def selection_snapshot(
    *,
    symbol: str,
    strike: float | int | None,
    previous_side: str = "",
    active_trade: dict | None = None,
    manual: str = "AUTO",
    upside: float | None = None,
    downside: float | None = None,
    paths_up: int | None = None,
    paths_total: int | None = None,
    pcr: float | None = None,
    spot: float | None = None,
    vwap: float | None = None,
    obi_5: float | None = None,
    smc_side: str = "",
    fallback: str = "CE",
    force_side: str = "",
) -> dict[str, Any]:
    """Return the watched leg plus the exact five live switch conditions.

    This is presentation evidence, not an order instruction.  An open trade
    remains locked even when the checklist has not yet filled.
    """
    trade = active_trade if isinstance(active_trade, dict) else {}
    open_trade = str(trade.get("status") or "").upper() == "OPEN"
    side = choose_watch_side(
        active_trade=trade, manual=manual, upside=upside, downside=downside,
        pcr=pcr, spot=spot, vwap=vwap, obi_5=obi_5, smc_side=smc_side,
        fallback=fallback,
    )
    if str(force_side or "").upper() in {"CE", "PE"}:
        side = str(force_side).upper()
    if open_trade:
        mode = "LOCKED_TO_OPEN_TRADE"
    elif str(manual or "AUTO").upper() in {"CE", "PE"}:
        mode = "MANUAL_PIN"
    else:
        mode = "AUTO"
    up = float(upside or 0.0)
    down = float(downside or 0.0)
    total = int(paths_total or 100)
    bullish = int(paths_up or 0)
    bearish = max(0, total - bullish)
    bullish_pct = 100.0 * bullish / max(total, 1)
    bearish_pct = 100.0 * bearish / max(total, 1)
    pcr_value = float(pcr or 0.0)
    px, avg, book = float(spot or 0.0), float(vwap or 0.0), float(obi_5 or 0.0)
    below = px > 0 and avg > 0 and px < avg
    above = px > 0 and avg > 0 and px >= avg
    conditions = [
        {"key": "bias", "label": "100-Way Bias",
         "pass": (bearish > bullish) if side == "PE" else (bullish >= bearish),
         "value": (f"Bearish {bearish_pct:.0f}% ({bearish}/{total} paths ↓)"
                   if side == "PE" else f"Bullish {bullish_pct:.0f}% ({bullish}/{total} paths ↑)")},
        {"key": "spot_vwap", "label": "Spot vs VWAP",
         "pass": below if side == "PE" else above,
         "value": (f"{px:,.2f} < VWAP {avg:,.2f} ({(px - avg):+.2f} pts)"
                   if side == "PE" else f"{px:,.2f} ≥ VWAP {avg:,.2f} ({(px - avg):+.2f} pts)")},
        {"key": "pcr", "label": "Live PCR & ΔOI",
         "pass": (0 < pcr_value < 0.82) if side == "PE" else (pcr_value > 1.05),
         "value": f"PCR {pcr_value:.2f} ({'Call Writers Dominant' if pcr_value < 0.82 else 'Put Writers Dominant' if pcr_value > 1.05 else 'Neutral'})"},
        {"key": "obi", "label": "Option Order-Book (OBI_5)",
         "pass": (0 < book <= 0.90) if side == "PE" else (book >= 1.05),
         "value": f"{side} OBI {book:.2f}x ({'Buyers > Sellers' if book >= 1.0 else 'Sellers > Buyers'})"},
        {"key": "smc", "label": "SMC Structure",
         "pass": str(smc_side or "").upper() == side,
         "value": f"{str(smc_side or 'WAITING').upper()} structure"},
    ]
    label = contract_label(symbol, trade.get("strike") if open_trade else strike, side)
    transition = bool(previous_side and str(previous_side).upper() != side and not open_trade)
    if transition:
        transition_te = (
            f"🔄 {previous_side} ➔ {side} మార్పు: "
            f"100 దారులలో {bearish if side == 'PE' else bullish}/{total} దారులు "
            f"{'Downside' if side == 'PE' else 'Upside'} చూపిస్తున్నాయి; "
            f"కాబట్టి చార్ట్ {label} కి మారింది"
        )
    else:
        transition_te = ""
    return {
        "side": side, "mode": mode, "symbol": symbol, "strike": strike,
        "contract": label, "previous_side": str(previous_side or ""),
        "transition": transition, "transition_te": transition_te,
        "conditions": conditions,
        "conditions_passed": sum(1 for row in conditions if row["pass"]),
        "reason_te": (
            f"🔒 ఓపెన్ ట్రేడ్ కాంట్రాక్ట్ lock చేయబడింది: {label}"
            if mode == "LOCKED_TO_OPEN_TRADE" else
            f"📌 మాన్యువల్ pin: {label}" if mode == "MANUAL_PIN" else
            f"🎯 AUTO: {label} ఎంపిక చేయబడింది ({side} bias)"
        ),
    }


def new_trade(entry: float, risk: float, qty: int = 300) -> dict[str, Any]:
    risk = max(float(risk), 0.5)
    entry = float(entry)
    return {
        "status": "OPEN",
        "entry_price": round(entry, 2),
        "initial_sl": round(entry - risk, 2),
        "current_trailing_sl": round(entry - risk, 2),
        "tsl_gear": 1,
        "target_1": round(entry + 1.5 * risk, 2),
        "target_2_p90": round(entry + 3.0 * risk, 2),
        "qty": int(qty),
        "initial_qty": int(qty),
        "qty_open": int(qty),
        "realized_pnl_rs": 0.0,
        "booked_legs": [],
        "t1_booked": False,
        "t2_booked": False,
        "exit_reason": None,
        "risk_points": round(risk, 2),
        "live_pnl_rs": 0.0,
        "live_pnl_pct": 0.0,
    }


def gear_update(trade: dict, ltp: float, low_5m: float | None = None,
                low_1m: float | None = None) -> dict:
    """Move the stop through the four gears; never move it down."""
    entry = float(trade["entry_price"])
    risk = float(trade.get("risk_points") or max(entry - float(trade["initial_sl"]), 0.5))
    r_multiple = (float(ltp) - entry) / risk
    stop = float(trade["current_trailing_sl"])
    gear = int(trade["tsl_gear"])

    if r_multiple >= 3.0:
        trail = float(low_1m) if low_1m else float(ltp) - risk
        stop, gear = max(stop, trail), 4
    elif r_multiple >= 2.0:
        stop, gear = max(stop, float(low_5m) if low_5m else entry + risk * 0.5), 3
    elif r_multiple >= 1.0:
        stop, gear = max(stop, entry), 2

    trade["current_trailing_sl"] = round(stop, 2)
    trade["tsl_gear"] = gear
    # After T1 only the open quantity keeps marking; realised rupees stay banked.
    open_qty = int(trade.get("qty_open", trade.get("qty", 0)) or 0)
    initial_qty = int(trade.get("initial_qty", trade.get("qty", 1)) or 1)
    realised = float(trade.get("realized_pnl_rs") or 0.0)
    pnl = realised + (float(ltp) - entry) * open_qty
    trade["live_pnl_rs"] = round(pnl, 0)
    trade["live_pnl_pct"] = round(pnl / (entry * initial_qty) * 100.0, 2)
    if float(ltp) <= trade["current_trailing_sl"]:
        trade["status"] = "CLOSED"
    return trade
