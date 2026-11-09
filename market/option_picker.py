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
        "risk_points": round(risk, 2),
        "live_pnl_rs": 0.0,
        "live_pnl_pct": 0.0,
    }


def gear_update(trade: dict, ltp: float, low_5m: float | None = None) -> dict:
    """Move the stop through the four gears; never move it down."""
    entry = float(trade["entry_price"])
    risk = float(trade.get("risk_points") or max(entry - float(trade["initial_sl"]), 0.5))
    r_multiple = (float(ltp) - entry) / risk
    stop = float(trade["current_trailing_sl"])
    gear = int(trade["tsl_gear"])

    if r_multiple >= 3.0:
        stop, gear = max(stop, float(ltp) - risk), 4
    elif r_multiple >= 2.0:
        stop, gear = max(stop, float(low_5m) if low_5m else entry + risk * 0.5), 3
    elif r_multiple >= 1.0:
        stop, gear = max(stop, entry), 2

    trade["current_trailing_sl"] = round(stop, 2)
    trade["tsl_gear"] = gear
    trade["live_pnl_rs"] = round((float(ltp) - entry) * int(trade.get("qty", 0)), 0)
    trade["live_pnl_pct"] = round((float(ltp) / entry - 1.0) * 100.0, 2)
    if float(ltp) <= trade["current_trailing_sl"]:
        trade["status"] = "CLOSED"
    return trade
