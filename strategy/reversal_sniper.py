"""Pre-emptive institutional reversal sniper.

The detector is deliberately pure: the live session supplies the Upstox V3
full-spectrum values and the functions return a deterministic decision plus a
five-item checklist for the dashboard.  No broker calls and no clock reads.
"""

from __future__ import annotations

from typing import Any

MISSING_REASON = "MISSING_OR_STALE_FULL_SPECTRUM_DATA"
MIN_SL_POINTS = 6.0
MAX_SL_POINTS = 9.0
BOTTOM_PATHS_UP = 0.80
VOLUME_SPIKE_MULTIPLE = 2.5
OBI_FLIP = 1.50
TBQ_DOMINANCE = 1.35
CE_SELLER_DOMINANCE = 1.80
CE_OBI_CRUSH = 0.65
PE_OBI_FLIP = 1.50


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive(*values: float) -> bool:
    return all(float(value) > 0 for value in values)


def paths_ratio(paths_up: float | int | None, samples: float | int | None = 0) -> float:
    """Normalise a path count (``88/100``) or an already-fractional value."""
    if paths_up is None:
        return 0.0
    value = _f(paths_up)
    sample_count = _f(samples)
    if sample_count > 0 and value > 1.0:
        return max(0.0, min(value / sample_count, 1.0))
    if value > 1.0:
        return max(0.0, min(value / 100.0, 1.0))
    return max(0.0, min(value, 1.0))


def clamp_sl(raw_points: float, minimum: float = MIN_SL_POINTS,
             maximum: float = MAX_SL_POINTS) -> float:
    """Clamp a structural stop into the sniper's ₹6–₹9 risk envelope."""
    return round(min(max(_f(raw_points), float(minimum)), float(maximum)), 2)


def _item(key: str, label: str, passed: bool, value: str) -> dict[str, Any]:
    return {"key": key, "label": label, "pass": bool(passed), "value": value}


def checklist_label(items: list[dict[str, Any]]) -> str:
    passed = sum(1 for item in items if item.get("pass"))
    return f"\U0001F3AF SNIPER REVERSAL CHECKLIST ({passed}/{len(items)})"


def checklist_state(items: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for item in items if item.get("pass"))
    return {
        "items": items,
        "checklist": items,
        "passed": passed,
        "total": len(items),
        "complete": bool(items) and passed == len(items),
        "badge": checklist_label(items),
    }


def detect_bottom_reversal(
    *,
    spot: float,
    p10: float,
    paths_up: float | int | None,
    samples: float | int | None = 0,
    futures_volume_ratio: float,
    price_velocity: float = 0.0,
    velocity_stall_threshold: float = 0.0,
    obi_5: float,
    tbq: float,
    tsq: float,
    pe_oi_change_5m: float = 0.0,
    ce_oi_change_5m: float = 0.0,
    entry: float = 0.0,
    absorption_low: float = 0.0,
    min_sl_points: float = MIN_SL_POINTS,
    max_sl_points: float = MAX_SL_POINTS,
) -> dict[str, Any]:
    """Detect the five-signature bottom reversal and size the CE sniper stop."""
    ratio = paths_ratio(paths_up, samples)
    raw_risk = round(_f(entry) - _f(absorption_low), 2)
    sl_points = clamp_sl(raw_risk, min_sl_points, max_sl_points) if raw_risk > 0 else 0.0
    sl_ok = bool(raw_risk > 0 and raw_risk <= max_sl_points)

    data_ok = bool(
        _positive(spot, p10, futures_volume_ratio, obi_5, tbq, tsq, entry, absorption_low)
        and paths_up is not None
    )

    sig_band = bool(data_ok and _f(spot) <= _f(p10) * 1.0005 and ratio >= BOTTOM_PATHS_UP)
    sig_volume = bool(
        data_ok
        and _f(futures_volume_ratio) >= VOLUME_SPIKE_MULTIPLE
        and _f(price_velocity) >= -abs(_f(velocity_stall_threshold))
    )
    sig_obi = bool(
        data_ok
        and _f(obi_5) > OBI_FLIP
        and _f(tbq) > TBQ_DOMINANCE * _f(tsq)
    )
    sig_oi = bool(data_ok and (_f(pe_oi_change_5m) > 0 or _f(ce_oi_change_5m) < 0))
    sig_sl = bool(data_ok and sl_ok)

    items = [
        _item("p10_band", "[p10/p90 Band]",
              sig_band, f"Spot {_f(spot):,.2f} vs p10 {_f(p10):,.2f} · paths {ratio:.0%}"),
        _item("futures_volume", "[Fut Vol 300%]",
              sig_volume, f"{_f(futures_volume_ratio):.2f}x 20-bar mean · velocity {_f(price_velocity):+.2f}"),
        _item("obi_5", "[5-Lvl OBI > 1.5]",
              sig_obi, f"OBI {_f(obi_5):.2f} · TBQ/TSQ {(_f(tbq) / _f(tsq)) if _f(tsq) else 0:.2f}"),
        _item("oi_panic", "[5m ΔOI Panic]",
              sig_oi, f"ΔPE OI {_f(pe_oi_change_5m):+,.0f} · ΔCE OI {_f(ce_oi_change_5m):+,.0f}"),
        _item("sl_cap", "[SL <= ₹9]",
              sig_sl, f"SL ₹{sl_points:.2f} (structural ₹{raw_risk:.2f})" if sl_points else "no valid wick"),
    ]
    trigger = all(item["pass"] for item in items)
    reason = "BOTTOM_REVERSAL_CE_CONFIRMED" if trigger else (
        MISSING_REASON if not data_ok else "WAITING_FOR_BOTTOM_REVERSAL"
    )
    return {
        **checklist_state(items),
        "side": "CE",
        "trigger": trigger,
        "peak_exit": False,
        "pe_trigger": False,
        "data_ok": data_ok,
        "reason": reason,
        "entry": round(_f(entry), 2),
        "stop": round(_f(entry) - sl_points, 2) if sl_points else 0.0,
        "sl_points": sl_points,
        "structural_sl_points": raw_risk,
        "paths_up_pct": round(ratio * 100.0, 1),
        "signature_count": sum(1 for item in items if item["pass"]),
        "confidence": round(sum(1 for item in items if item["pass"]) / len(items) * 100.0, 1),
        "action": "BUY_CE" if trigger else "WAIT",
    }


def _mtf_bearish(mtf: Any) -> bool:
    if isinstance(mtf, str):
        return mtf.strip().upper() == "BEAR"
    if isinstance(mtf, dict):
        if str(mtf.get("alignment") or "").upper() == "BEAR":
            return True
        frames = mtf.get("frames") or {}
        trends = [str((frames.get(key) or {}).get("trend") or "").upper()
                  for key in ("1m", "5m", "15m")]
        return bool(trends) and all(trend == "BEAR" for trend in trends)
    return False


def detect_top_reversal(
    *,
    spot: float,
    p90: float,
    basis: float,
    previous_basis: float | None,
    ce_iv_change_5m: float | None,
    ce_tbq: float,
    ce_tsq: float,
    ce_obi_5: float,
    mtf: Any = None,
    pe_obi_5: float = 0.0,
    pe_tbq: float = 0.0,
    pe_tsq: float = 0.0,
    entry: float = 0.0,
    absorption_high: float = 0.0,
    min_sl_points: float = MIN_SL_POINTS,
    max_sl_points: float = MAX_SL_POINTS,
    min_basis_drop: float = 0.0,
) -> dict[str, Any]:
    """Detect the p90 top wave, CE peak exit and the pre-emptive PE re-entry."""
    data_ok = bool(
        _positive(spot, p90, ce_tbq, ce_tsq, ce_obi_5, entry, absorption_high)
        and previous_basis is not None
        and ce_iv_change_5m is not None
    )
    basis_drop = _f(previous_basis) - _f(basis)
    raw_risk = round(_f(absorption_high) - _f(entry), 2)
    sl_points = clamp_sl(raw_risk, min_sl_points, max_sl_points) if raw_risk > 0 else 0.0
    sl_ok = bool(raw_risk > 0 and raw_risk <= max_sl_points)

    sig_band = bool(data_ok and _f(spot) >= _f(p90))
    sig_basis = bool(data_ok and basis_drop > max(_f(min_basis_drop), 0.0))
    sig_iv = bool(
        data_ok
        and _f(ce_iv_change_5m) < 0
        and (_f(ce_tsq) >= CE_SELLER_DOMINANCE * _f(ce_tbq) or _f(ce_obi_5) < CE_OBI_CRUSH)
    )
    pe_flow = bool(
        _positive(pe_obi_5, pe_tbq, pe_tsq)
        and _f(pe_obi_5) > PE_OBI_FLIP
        and _f(pe_tbq) > TBQ_DOMINANCE * _f(pe_tsq)
    )
    mtf_ok = _mtf_bearish(mtf)
    sig_pe = bool(pe_flow and mtf_ok)
    sig_sl = bool(sl_ok)

    items = [
        _item("p90_band", "[p10/p90 Band]",
              sig_band, f"Spot {_f(spot):,.2f} vs p90 {_f(p90):,.2f}"),
        _item("basis_drop", "[Fut Vol 300%]",
              sig_basis, f"Basis {_f(basis):+,.2f} vs prev {_f(previous_basis):+,.2f} (drop {basis_drop:+.2f})"),
        _item("iv_crush", "[5-Lvl OBI > 1.5]",
              sig_iv, f"5m ΔIV {_f(ce_iv_change_5m):+.2f}% · TSQ/TBQ {(_f(ce_tsq) / _f(ce_tbq)) if _f(ce_tbq) else 0:.2f}"),
        _item("pe_flow", "[5m ΔOI Panic]",
              sig_pe, f"PE OBI {_f(pe_obi_5):.2f} · MTF {'BEAR' if mtf_ok else 'not bearish'}"),
        _item("sl_cap", "[SL <= ₹9]",
              sig_sl, f"SL ₹{sl_points:.2f} (structural ₹{raw_risk:.2f})" if sl_points else "no valid wick"),
    ]
    peak_exit = bool(sig_band and sig_basis and sig_iv)
    pe_trigger = bool(peak_exit and sig_pe and sig_sl)
    reason = MISSING_REASON if not data_ok else (
        "TOP_REVERSAL_CE_EXIT_PE_ENTRY" if pe_trigger
        else "TOP_REVERSAL_CE_PEAK_EXIT" if peak_exit
        else "WAITING_FOR_TOP_REVERSAL"
    )
    return {
        **checklist_state(items),
        "side": "PE" if pe_trigger else "CE",
        "trigger": peak_exit or pe_trigger,
        "peak_exit": peak_exit,
        "close_ce": peak_exit,
        "pe_trigger": pe_trigger,
        "data_ok": data_ok,
        "reason": reason,
        "entry": round(_f(entry), 2),
        "stop": round(_f(entry) + sl_points, 2) if sl_points else 0.0,
        "sl_points": sl_points,
        "structural_sl_points": raw_risk,
        "basis": round(_f(basis), 2),
        "basis_drop": round(basis_drop, 2),
        "signature_count": sum(1 for item in items if item["pass"]),
        "confidence": round(sum(1 for item in items if item["pass"]) / len(items) * 100.0, 1),
        "action": "CLOSE_CE_AND_BUY_PE" if pe_trigger else "CLOSE_CE_PEAK" if peak_exit else "WAIT",
    }


def sniper_state(bottom: dict[str, Any] | None = None,
                 top: dict[str, Any] | None = None) -> dict[str, Any]:
    """Choose the actionable checklist for the dashboard badge."""
    bottom = bottom or {}
    top = top or {}
    chosen = top if top.get("trigger") else bottom
    items = list(chosen.get("checklist") or [])
    return {
        **checklist_state(items),
        "bottom": bottom,
        "top": top,
        "side": chosen.get("side") or "",
        "trigger": bool(bottom.get("trigger") or top.get("trigger") or top.get("peak_exit")),
        "action": chosen.get("action") or "WAIT",
        "reason": chosen.get("reason") or "WAITING_FOR_BOTTOM_REVERSAL",
    }
