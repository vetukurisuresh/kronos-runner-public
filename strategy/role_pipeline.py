"""Role-specialized pipeline: 25M data gate -> 102M direction -> 4M timing.

Each role has one job and never competes in a flat vote:

* ``data_gate``   (KRONOS-25M) confirms the market data: PCR, 5m delta-OI,
  max pain, Greeks/IV, futures volume, VIX slope and heavyweight breadth.
* ``micro_timing`` (KRONOS-4M) confirms the selected option's order book and
  1m candle formation, and arms the expiry "zero-to-hero" sleeve.
* ``build_role_pipeline`` joins them per index and renders the badge text.

Every value is derived from the live feed.  A missing input counts as a miss
(score 0) and is reported as ``None``; nothing is invented.
"""

from __future__ import annotations

from typing import Any, Mapping

from strategy.full_spectrum_alpha import chain_metrics

GATE_FLOOR = 60.0
EXPIRY_PREMIUM_BAND = (10.0, 38.0)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def iv_percent(value: Any) -> float | None:
    """0.1718 -> 17.18%, while an already-percent 17.18 stays 17.18."""
    iv = _f(value)
    if iv <= 0:
        return None
    return round(iv * 100.0, 2) if iv < 2.0 else round(iv, 2)


def _block(feed: Mapping[str, Any], name: str, symbol: str) -> dict[str, Any]:
    block = (feed.get(name) or {}).get(symbol) or {}
    return dict(block) if isinstance(block, Mapping) else {}


def _doi_pct(change: float, oi: float) -> float | None:
    """5m delta-OI as a percentage of the prior book (never divides by zero)."""
    if oi <= 0:
        return None
    prior = max(oi - change, 1.0)
    return round(100.0 * change / prior, 2)


def data_gate(feed: Mapping[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
    """KRONOS-25M: the 360-degree data confirmation gate."""
    symbol = str(symbol or "NIFTY").upper()
    feed = feed or {}
    rows = (feed.get("option_chain") or {}).get(symbol) or []
    metrics = chain_metrics(rows)
    ce_oi, pe_oi = _f(metrics.get("ce_oi")), _f(metrics.get("pe_oi"))
    ce_doi_pct = _doi_pct(_f(metrics.get("ce_doi")), ce_oi)
    pe_doi_pct = _doi_pct(_f(metrics.get("pe_doi")), pe_oi)

    spectrum = _block(feed, "full_spectrum", symbol)
    depth = spectrum.get("depth") or {}
    greeks = spectrum.get("greeks") or {}
    vix = spectrum.get("vix") or {}
    breadth = spectrum.get("breadth") or {}
    futures = spectrum.get("futures") or {}
    option = _block(feed, "option", symbol)
    spot = _block(feed, "spot", symbol)

    obi = _f(option.get("obi_5") or depth.get("obi_5"))
    tbq = _f(option.get("tbq_tsq") or depth.get("tbq_tsq"))
    ltp = _f(option.get("ltp"))
    opt_vwap = _f(option.get("atp_vwap") or option.get("vwap"))
    pcr = _f(metrics.get("pcr"))
    # The raw 11-strike window can be OI-empty early in a session; the
    # spectrum aggregate still carries the real PCR (dict or bare number).
    spec_pcr = spectrum.get("pcr")
    if isinstance(spec_pcr, Mapping):
        spec_pcr = spec_pcr.get("pcr")
    pcr = pcr or _f(spec_pcr)
    aligned, total = _f(breadth.get("aligned")), _f(breadth.get("total"))
    vtt = _f(futures.get("vtt_ratio") or futures.get("volume_ratio"))
    vix_slope = _f(vix.get("change_pct"))
    spot_ltp, spot_vwap = _f(spot.get("ltp")), _f(spot.get("vwap") or spot.get("atp"))
    theta = _f(greeks.get("theta"))
    iv_pct = iv_percent(greeks.get("iv") or option.get("iv"))

    def components(bullish: bool) -> dict[str, bool | None]:
        # A side only confirms its own direction when the opposite book is not
        # also building.  Both sides building is ambiguous, not two signals.
        if bullish:
            doi = ((pe_doi_pct or 0.0) > 0 and (ce_doi_pct is None or ce_doi_pct <= 0))
        else:
            doi = ((ce_doi_pct or 0.0) > 0 and (pe_doi_pct is None or pe_doi_pct <= 0))
        if ce_doi_pct is None and pe_doi_pct is None:
            doi = None
        return {
            "pcr": (pcr >= 1.0 if bullish else pcr <= 0.8) if pcr else None,
            "doi": doi,
            "obi": (obi >= 1.15) if obi else None,
            "tbq": (tbq >= 1.20) if tbq else None,
            "vtt": (vtt >= 1.0) if vtt else None,
            "breadth": (aligned >= max(total / 2.0, 1.0)) if total else None,
            "spot_vwap": ((spot_ltp >= spot_vwap) if bullish else (spot_ltp <= spot_vwap))
            if (spot_ltp and spot_vwap) else None,
        }

    def score(comps: Mapping[str, bool | None]) -> float:
        # Fixed denominator: a missing input is a miss, never a free win.
        wins = sum(1 for value in comps.values() if value is True)
        return round(100.0 * wins / len(comps), 1)

    bull_components = components(True)
    bear_components = components(False)
    bull_score, bear_score = score(bull_components), score(bear_components)
    greeks_ok = bool(_f(greeks.get("delta")) > 0 and _f(greeks.get("gamma")) and iv_pct)
    trap = bool(
        ce_doi_pct is not None
        and pe_doi_pct is not None
        and (
            (ce_doi_pct <= -3.0 and pe_doi_pct >= 3.5)
            or (pe_doi_pct <= -3.0 and ce_doi_pct >= 3.5)
        )
    )
    return {
        "role": "KRONOS-25M",
        "symbol": symbol,
        "strikes": int(metrics.get("strike_count") or 0),
        "valid_strikes": int(metrics.get("valid_strikes") or 0),
        "pcr": metrics.get("pcr"),
        "doi_pcr": metrics.get("doi_pcr"),
        "ce_doi_pct": ce_doi_pct,
        "pe_doi_pct": pe_doi_pct,
        "max_pain": metrics.get("max_pain"),
        "writer_trap_explosion": trap,
        "trapped_writers_fuel": bool(trap),
        "delta": greeks.get("delta"),
        "gamma": greeks.get("gamma"),
        "theta": greeks.get("theta"),
        "iv_pct": iv_pct,
        "theta_ev_ratio": round(abs(ltp) / abs(theta), 2) if theta else None,
        "greeks_ok": greeks_ok,
        "obi_5": obi or None,
        "tbq_tsq": tbq or None,
        "futures_vtt": vtt or None,
        "basis": futures.get("basis"),
        "vix_slope": vix_slope or None,
        "breadth": f"{int(aligned)}/{int(total)}" if total else None,
        "option_vwap": opt_vwap or None,
        "data_confirmed_bullish": bool(
            bull_score >= GATE_FLOOR and greeks_ok
            and all(bull_components[key] is True for key in ("pcr", "doi", "spot_vwap"))
        ),
        "bull_score": bull_score,
        "data_confirmed_bearish": bool(
            bear_score >= GATE_FLOOR and greeks_ok
            and all(bear_components[key] is True for key in ("pcr", "doi", "spot_vwap"))
        ),
        "bear_score": bear_score,
        "components": {"bullish": bull_components, "bearish": bear_components},
    }


def micro_timing(feed: Mapping[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
    """KRONOS-4M: 1m option candles + 5-level book, and the expiry sleeve."""
    symbol = str(symbol or "NIFTY").upper()
    feed = feed or {}
    option = _block(feed, "option", symbol)
    spectrum = _block(feed, "full_spectrum", symbol)
    depth = spectrum.get("depth") or {}
    candles = option.get("candles_1m") or []
    formed = sum(
        1
        for row in candles
        if isinstance(row, Mapping)
        and _f(row.get("h", row.get("high"))) > 0
        and _f(row.get("l", row.get("low"))) > 0
    )
    ltp = _f(option.get("ltp"))
    vwap = _f(option.get("atp_vwap") or option.get("vwap"))
    obi = _f(option.get("obi_5") or depth.get("obi_5"))
    tbq = _f(option.get("tbq_tsq") or depth.get("tbq_tsq"))
    above_vwap = bool(ltp > 0 and vwap > 0 and ltp > vwap)
    premium_ok = bool(EXPIRY_PREMIUM_BAND[0] <= ltp <= EXPIRY_PREMIUM_BAND[1])
    armed = bool(
        formed >= 30 and above_vwap and obi >= 1.75 and tbq >= 1.80 and premium_ok
    )
    return {
        "role": "KRONOS-4M",
        "symbol": symbol,
        "candles_1m": formed,
        "ltp": ltp or None,
        "option_vwap": vwap or None,
        "above_vwap": above_vwap,
        "obi_5": obi or None,
        "tbq_tsq": tbq or None,
        "premium_band_ok": premium_ok,
        "expiry_armed": armed,
        "directional_ok": bool(formed >= 30 and above_vwap and obi >= 1.15),
    }


def _badges(gate: Mapping[str, Any], micro: Mapping[str, Any],
            upside: Any) -> dict[str, str]:
    prob = _f(upside)
    if prob >= 62.0:
        direction = f"{prob:.0f}% BULL"
    elif prob and prob <= 38.0:
        direction = f"{100.0 - prob:.0f}% BEAR"
    else:
        direction = f"{prob:.0f}% NEUTRAL" if prob else "WAITING"
    if gate.get("data_confirmed_bullish") and gate.get("data_confirmed_bearish"):
        gate_txt = "✅ CONFIRMED BOTH"
    elif gate.get("data_confirmed_bullish"):
        gate_txt = "✅ CONFIRMED BULL"
    elif gate.get("data_confirmed_bearish"):
        gate_txt = "✅ CONFIRMED BEAR"
    else:
        gate_txt = "⏳ WAITING"
    return {
        "d102": f"🧭 102M DIRECTIONAL: {direction}",
        "gate25": (
            f"📊 25M DATA GATE: {gate_txt} "
            f"(PCR {gate.get('pcr') if gate.get('pcr') is not None else '—'} · "
            f"Breadth {gate.get('breadth') or '—'})"
        ),
        "sniper4": (
            "💥 4M EXPIRY/SNIPER: "
            + ("ARMED" if micro.get("expiry_armed") else
               "MICRO OK" if micro.get("directional_ok") else "SCANNING")
            + f" (OBI {micro.get('obi_5') or '—'}x)"
        ),
    }


def build_role_pipeline(feed: Mapping[str, Any], symbol: str = "NIFTY") -> dict[str, Any]:
    """Join the three roles for the badge strip; never raises."""
    feed = feed or {}
    symbols = ("NIFTY", "BANKNIFTY", "SENSEX")
    gates = {name: data_gate(feed, name) for name in symbols}
    micros = {name: micro_timing(feed, name) for name in symbols}
    primary = str(symbol or "NIFTY").upper()
    if primary not in gates:
        primary = "NIFTY"
    upside = (feed.get("model_oscillator") or {}).get("bullish_upside_prob_pct")
    return {
        "primary": primary,
        "gate_25m": gates[primary],
        "micro_4m": micros[primary],
        "symbols": {
            name: {"gate_25m": gates[name], "micro_4m": micros[name]}
            for name in symbols
        },
        "badges": _badges(gates[primary], micros[primary], upside),
    }


__all__ = ["data_gate", "micro_timing", "build_role_pipeline", "iv_percent"]
