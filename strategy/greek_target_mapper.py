"""Exact Greek-adjusted target mapping for the KRONOS sniper desk.

The option target is a second-order Taylor expansion over the 12-candle
(60-minute) forecast horizon::

    Entry + Delta*dS + 0.5*Gamma*dS^2 + Vega*dIV - |Theta|*dt

T1 is the p65 band with a minimum 1:1.8 reward/risk and books 50%.  T2 is the
p90 exhaustion wave, with a gamma/short-covering boost when 5-minute OI
unwinding exceeds 8%.  The module is pure and returns every component so the
dashboard and Telugu story can explain the number exactly.
"""

from __future__ import annotations

from typing import Any

MIN_RR = 1.8
T1_BOOK_PCT = 0.50
BREAKEVEN_OFFSET = 1.50
P65_Z_FRACTION = 0.3853 / 1.2816  # p65 between p50 and p90 for a normal cone


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def interpolate_p65(p10: float, p50: float, p90: float) -> float:
    """Interpolate the p65 band when KRONOS only publishes p10/p50/p90."""
    p50 = _f(p50)
    p90 = _f(p90)
    if p50 > 0 and p90 > 0:
        return round(p50 + (p90 - p50) * P65_Z_FRACTION, 2)
    p10 = _f(p10)
    if p10 > 0 and p90 > 0:
        return round(p10 + (p90 - p10) * 0.65, 2)
    return 0.0


def _taylor(
    *,
    entry: float,
    move: float,
    delta: float,
    gamma: float,
    vega: float,
    iv_change: float,
    theta: float,
    dt_minutes: float,
) -> dict[str, float]:
    delta_term = delta * move
    gamma_term = 0.5 * gamma * move * move
    vega_term = vega * iv_change
    theta_term = abs(theta) * max(dt_minutes, 0.0) / 375.0
    target = entry + delta_term + gamma_term + vega_term - theta_term
    return {
        "target": round(target, 2),
        "move": round(move, 2),
        "delta_term": round(delta_term, 4),
        "gamma_term": round(gamma_term, 4),
        "vega_term": round(vega_term, 4),
        "theta_term": round(theta_term, 4),
    }


def map_greek_targets(
    *,
    entry: float,
    spot: float,
    p65: float | None = None,
    p50: float | None = None,
    p90: float,
    p10: float | None = None,
    delta: float,
    gamma: float,
    theta: float,
    vega: float,
    expected_iv_change: float = 0.0,
    dt_minutes: float = 60.0,
    oi_unwinding_pct: float = 0.0,
    side: str = "CE",
    sl_points: float = 7.5,
    qty: int = 300,
    min_rr: float = MIN_RR,
) -> dict[str, Any]:
    """Map the KRONOS p65/p90 bands into exact option-premium targets."""
    side = str(side or "CE").upper()
    entry = _f(entry)
    spot = _f(spot)
    p90 = _f(p90)
    p65 = _f(p65) or interpolate_p65(_f(p10), _f(p50), p90)
    sl_points = max(_f(sl_points), 0.1)

    missing = [
        name for name, value in (
            ("entry", entry), ("spot", spot), ("p65", p65), ("p90", p90),
            ("delta", _f(delta)), ("gamma", _f(gamma)), ("theta", _f(theta)),
            ("vega", _f(vega)),
        ) if value == 0
    ]
    if missing:
        return {
            "ok": False,
            "reason": "MISSING_GREEKS_OR_TARGET_BAND",
            "missing": missing,
            "target_1": 0.0,
            "target_2_p90": 0.0,
            "t1_label": "",
            "t2_label": "",
        }

    direction = 1.0 if side == "CE" else -1.0
    move_t1 = abs(p65 - spot)
    move_t2 = abs(p90 - spot)
    signed_t1 = direction * move_t1
    signed_t2 = direction * move_t2

    t1 = _taylor(
        entry=entry, move=signed_t1, delta=_f(delta), gamma=_f(gamma),
        vega=_f(vega), iv_change=_f(expected_iv_change), theta=_f(theta),
        dt_minutes=dt_minutes,
    )
    t2 = _taylor(
        entry=entry, move=signed_t2, delta=_f(delta), gamma=_f(gamma),
        vega=_f(vega), iv_change=_f(expected_iv_change), theta=_f(theta),
        dt_minutes=dt_minutes,
    )

    # T1 is the 75%-probability p65 target, but never below 1:1.8 R:R.
    t1_floor = entry + min_rr * sl_points
    target_1 = round(max(t1["target"], t1_floor), 2)
    rr = round((target_1 - entry) / sl_points, 2)

    # T2 gets the short-covering/gamma boost when writers are unwinding.
    boost = 0.0
    if _f(oi_unwinding_pct) > 8.0:
        boost = round(abs(t2["gamma_term"]) * 0.25, 4)
    target_2 = round(max(t2["target"] + boost, target_1 + 0.8 * sl_points), 2)

    t1_qty = max(0, int(qty) // 2)
    t2_qty = max(0, int(qty) - t1_qty)
    breakeven = round(entry + BREAKEVEN_OFFSET, 2)
    t1_label = f"T1 \u20b9{target_1:.2f} (p65 \u00b7 50% Book \u00b7 \u0394+\u0393)"
    t2_label = f"T2 \u20b9{target_2:.2f} (p90 Wave \u00b7 Final Exit)"
    return {
        "ok": True,
        "side": side,
        "entry": round(entry, 2),
        "spot": round(spot, 2),
        "p65": round(p65, 2),
        "p90": round(p90, 2),
        "target_1": target_1,
        "target_2_p90": target_2,
        "t1_points": round(target_1 - entry, 2),
        "t2_points": round(target_2 - entry, 2),
        "rr": rr,
        "min_rr": round(float(min_rr), 2),
        "sl_points": round(sl_points, 2),
        "breakeven": breakeven,
        "t1_book_qty": t1_qty,
        "t2_book_qty": t2_qty,
        "t1_book_pct": T1_BOOK_PCT,
        "t1_label": t1_label,
        "t2_label": t2_label,
        "labels": {"t1": t1_label, "t2": t2_label},
        "formula": {
            "t1": {**t1, "target": target_1, "floor": round(t1_floor, 2)},
            "t2": {**t2, "target": target_2, "boost": boost},
            "theta_minutes": round(_f(dt_minutes), 1),
        },
        "reason": "GREEK_TARGETS_MAPPED",
    }


def target_labels(targets: dict[str, Any]) -> dict[str, str]:
    """Safe labels for the chart even when the mapper failed closed."""
    if not targets or not targets.get("ok"):
        return {"t1": "T1 —", "t2": "T2 —"}
    return {
        "t1": str(targets.get("t1_label") or ""),
        "t2": str(targets.get("t2_label") or ""),
    }
