"""Pure helpers: ATM strike selection, CE/PE side, and the 4-gear trailing stop.

Kept free of network code so the terminal's most safety-critical numbers are
unit-testable.
"""

from __future__ import annotations

from typing import Any


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
    below_vwap = bool(px > 0 and avg > 0 and px < avg)
    above_vwap = bool(px > 0 and avg > 0 and px >= avg)
    put_chain = 0.0 < ratio < 0.82
    call_chain = ratio > 1.05

    if down > up and (put_chain or below_vwap or (0.0 < book < 0.90)):
        return "PE"
    if up >= down and (call_chain or above_vwap or book >= 1.05):
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
