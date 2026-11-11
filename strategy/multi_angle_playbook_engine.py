"""Four parallel autonomous playbooks for every market structure.

The old reversal sniper demanded a p10 band *and* an extreme futures-volume
spike at the same instant.  That is a fine top/bottom filter, but it cannot see
an established 87.5% trend with a one-sided option book.  This engine evaluates
four independent routes in parallel on every cycle:

1. TREND_SURF_PULLBACK       - directional trend continuation
2. ORDERFLOW_MOMENTUM_IGNITION - whale book + 1m momentum
3. SMC_EXTREME_REVERSAL_SNIPER - p10/p90 sweep + structure shift
4. WRITER_TRAP_GAMMA_SQUEEZE - trapped writers + Greek convexity

Every score is derived from live fields.  Missing data fails the relevant
condition; it never creates a synthetic quote or a default high score.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from market.option_picker import choose_watch_side, contract_label, pick_atm
from strategy.quant_math_formulas import (
    book_imbalance_from_depth,
    directional_conviction_score,
    flow_score,
    greek_ev_ratio,
)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
ENTRY_THRESHOLD = 70.0

PLAYBOOK_IDS = (
    "TREND_SURF_PULLBACK",
    "ORDERFLOW_MOMENTUM_IGNITION",
    "SMC_EXTREME_REVERSAL_SNIPER",
    "WRITER_TRAP_GAMMA_SQUEEZE",
)
PLAYBOOK_META = (
    ("TREND_SURF_PULLBACK", 1, "🌊 Trend Surf", "TREND SURF"),
    ("ORDERFLOW_MOMENTUM_IGNITION", 2, "⚡ OrderFlow Burst", "ORDERFLOW MOMENTUM IGNITION"),
    ("SMC_EXTREME_REVERSAL_SNIPER", 3, "🎯 SMC Sweep", "SMC EXTREME REVERSAL SNIPER"),
    ("WRITER_TRAP_GAMMA_SQUEEZE", 4, "🔥 OI Squeeze", "WRITER TRAP GAMMA SQUEEZE"),
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (max(1, period) + 1.0)
    out = values[0]
    for value in values[1:]:
        out = value * alpha + out * (1.0 - alpha)
    return out


def _candles(rows: Any, limit: int = 150) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out = [row for row in rows if isinstance(row, Mapping)]
    return out[-max(1, limit):]


def _close(row: Mapping[str, Any]) -> float:
    return _f(row.get("close", row.get("c")))


def _high(row: Mapping[str, Any]) -> float:
    return _f(row.get("high", row.get("h")))


def _low(row: Mapping[str, Any]) -> float:
    return _f(row.get("low", row.get("l")))


def _pct(value: Any) -> float:
    return _clamp(_f(value) / 100.0)


def _condition(key: str, label: str, passed: bool, value: str) -> dict[str, Any]:
    return {"key": key, "label": label, "pass": bool(passed), "value": value}


def _futures_ratio(feed: Mapping[str, Any], symbol: str) -> tuple[float, str]:
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    for key in ("futures_volume_ratio", "volume_ratio", "vtt_ratio"):
        value = _f(spectrum.get(key))
        if value > 0:
            return value, f"full_spectrum.{key}"
    futures = spectrum.get("futures") or {}
    direct = _f(futures.get("volume_ratio") or futures.get("vtt_ratio"))
    if direct > 0:
        return direct, "futures.volume_ratio"
    # The sniper audit publishes the real 20-bar ratio as display text when no
    # numeric field is present.  Parse that value rather than inventing 1.0.
    sniper = spectrum.get("sniper") or (feed.get("sniper") or {}).get(symbol) or {}
    rows = list(sniper.get("items") or []) + list(sniper.get("checklist") or [])
    for row in rows:
        text = str((row or {}).get("value") or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*x", text, flags=re.IGNORECASE)
        if match and (row or {}).get("key") == "futures_volume":
            return _f(match.group(1)), "sniper.checklist"
    return 0.0, "MISSING"


def _leg_quote(row: Mapping[str, Any], side: str) -> dict[str, Any]:
    leg = row.get("put_options" if side == "PE" else "call_options") or {}
    market = leg.get("market_data") or {}
    greeks = leg.get("option_greeks") or {}
    return {
        "ltp": _f(market.get("ltp")),
        "bid": _f(market.get("bid_price")),
        "ask": _f(market.get("ask_price")),
        "oi": _f(market.get("oi")),
        "prev_oi": _f(market.get("prev_oi")),
        "oi_change": _f(market.get("oi_change")),
        "delta": _f(greeks.get("delta")),
        "gamma": _f(greeks.get("gamma")),
        "theta": _f(greeks.get("theta")),
        "iv": _f(greeks.get("iv")),
        "source": "option_chain",
    }


def _oi_change_pct(quote: Mapping[str, Any]) -> float:
    prev = _f(quote.get("prev_oi"))
    change = _f(quote.get("oi_change"))
    if prev > 0:
        return round(change / prev * 100.0, 2)
    cur = _f(quote.get("oi"))
    if cur > 0 and change:
        return round(change / max(abs(cur - change), 1.0) * 100.0, 2)
    return 0.0


def _context(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    spot_block = (feed.get("spot") or {}).get(symbol) or {}
    option = (feed.get("option") or {}).get(symbol) or {}
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    chain = (feed.get("option_chain") or {}).get(symbol) or []
    oscillator = feed.get("model_oscillator") or {}
    brain = (feed.get("model_brain") or {}).get(symbol) or {}

    spot = _f(spot_block.get("ltp") or (feed.get("prices") or {}).get("NSE_INDEX|Nifty 50"))
    vwap = _f(spot_block.get("vwap") or spot_block.get("atp") or spot)
    atr = _f(spot_block.get("atr"))
    up = _f(oscillator.get("bullish_upside_prob_pct"), 50.0)
    down = _f(oscillator.get("bearish_downside_prob_pct"), 100.0 - up)
    raw_pcr = spectrum.get("pcr")
    if isinstance(raw_pcr, Mapping):
        pcr = _f(raw_pcr.get("pcr") or raw_pcr.get("ratio") or raw_pcr.get("value"))
    else:
        pcr = _f(raw_pcr)
    first_row = chain[0] if chain and isinstance(chain[0], Mapping) else {}
    if pcr <= 0:
        pcr = _f(first_row.get("pcr"))
    strike_hint = _f(option.get("strike_price") or option.get("strike"))
    # The selected live option strike is authoritative.  Only fall back to the
    # nearest chain strike when the feed has no selected contract (e.g. a cold
    # start before the first option cycle).
    row = pick_atm(chain, strike_hint if strike_hint > 0 else spot) if chain else None
    row = row or {}
    strike = _f(row.get("strike_price") or strike_hint)
    if strike <= 0 and spot > 0:
        strike = round(spot / 50.0) * 50.0

    obi = _f(option.get("obi_5"))
    if obi <= 0:
        obi = _f((option.get("depth") or {}).get("obi_5"))
    if obi <= 0:
        obi = book_imbalance_from_depth(option.get("depth") or spectrum.get("depth"))
    if obi <= 0:
        obi = _f((spectrum.get("depth") or {}).get("obi_5"), 1.0)
    weighted_obi = book_imbalance_from_depth(option.get("depth") or {})
    if weighted_obi <= 0:
        weighted_obi = obi
    tbq_tsq = _f(option.get("tbq_tsq"))
    if tbq_tsq <= 0:
        tbq_tsq = _f((option.get("depth") or {}).get("tbq_tsq"))
    if tbq_tsq <= 0:
        tbq_tsq = _f((spectrum.get("depth") or {}).get("tbq_tsq"), 1.0)

    side = choose_watch_side(
        active_trade=(feed.get("trade") or {}).get(symbol),
        manual="AUTO",
        upside=up,
        downside=down,
        pcr=pcr,
        spot=spot,
        vwap=vwap,
        obi_5=obi,
        smc_side=str(((feed.get("smc") or {}).get(symbol) or {}).get("state") or ""),
    )
    leg = _leg_quote(row, side)
    feed_side = str(option.get("option_type") or option.get("side") or "").upper()
    if leg["ltp"] <= 0 and feed_side == side:
        leg["ltp"] = _f(option.get("ltp"))
    if leg["delta"] == 0 and feed_side == side:
        leg["delta"] = _f(option.get("delta"))
        leg["iv"] = _f(option.get("iv"))
    if leg["bid"] <= 0 and feed_side == side:
        leg["bid"] = _f(option.get("bid"))
        leg["ask"] = _f(option.get("ask"))
        leg["source"] = "live_option_book"
    if leg["oi_change"] == 0:
        leg["oi_change"] = _f(option.get("oi_change_5m_pct")) if feed_side == side else 0.0
        leg["prev_oi"] = 0.0

    spot_bars = _candles(spot_block.get("candles_1m") or [], 150)
    option_bars = _candles(option.get("candles_1m") or [], 150)
    spot_closes = [_close(bar) for bar in spot_bars if _close(bar) > 0]
    option_closes = [_close(bar) for bar in option_bars if _close(bar) > 0]
    spot_ema9 = _ema(spot_closes[-150:], 9)
    spot_ema5 = _ema(spot_closes[-150:], 5)
    option_ema9 = _ema(option_closes[-150:], 9)
    option_ema5 = _ema(option_closes[-150:], 5)
    last_spot_bar = spot_bars[-1] if spot_bars else {}
    prev_spot_bar = spot_bars[-2] if len(spot_bars) >= 2 else last_spot_bar
    direction = 1 if side == "CE" else -1
    delta_velocity = direction * (_close(last_spot_bar) - _close(prev_spot_bar)) if last_spot_bar else 0.0
    if abs(delta_velocity) < 1e-9 and spot_ema9 > 0:
        delta_velocity = direction * (spot - spot_ema9)
    prior_low = min((_low(bar) for bar in spot_bars[-6:] if _low(bar) > 0), default=0.0)
    prior_high = max((_high(bar) for bar in spot_bars[-6:] if _high(bar) > 0), default=0.0)

    greeks = spectrum.get("greeks") or {}
    delta = abs(leg["delta"] or _f(greeks.get("delta")))
    gamma = _f(leg.get("gamma") or greeks.get("gamma"))
    theta = _f(leg.get("theta") or greeks.get("theta"))
    spread = max(0.0, leg["ask"] - leg["bid"])
    p10, p50, p90 = _f(brain.get("p10")), _f(brain.get("p50")), _f(brain.get("p90"))
    width = max(0.0, p90 - p10)
    expected_move = width / 2.0 if width > 0 else max(atr, spot * 0.001)
    ev = greek_ev_ratio(
        delta=delta, gamma=gamma, expected_abs_move=expected_move,
        theta_15m=theta, spread_inr=spread,
    )

    futures_ratio, futures_source = _futures_ratio(feed, symbol)
    if futures_ratio <= 0 and _f(spectrum.get("futures", {}).get("volume")) > 0:
        fut = spectrum.get("futures") or {}
        mean = _f(fut.get("volume_20bar_mean") or fut.get("volume"))
        futures_ratio = _f(fut.get("volume")) / max(mean, 1.0)
        futures_source = "futures.volume/mean"

    probability = down if side == "PE" else up
    paths_bullish = int(round(up))
    paths_bearish = max(0, 100 - paths_bullish)
    smc = (feed.get("smc") or {}).get(symbol) or {}
    structures = smc.get("structures") or {}
    primary = structures.get("5m") or smc.get("structure") or {}
    micro = structures.get("1m") or {}
    return {
        "symbol": symbol,
        "spot": round(spot, 2),
        "vwap": round(vwap, 2),
        "atr": atr,
        "side": side,
        "strike": strike,
        "contract": contract_label(symbol, strike, side),
        "option": leg,
        "option_type": feed_side,
        "option_ltp": round(leg["ltp"], 2),
        "option_atp": _f(option.get("atp_vwap")),
        "option_ema9": round(option_ema9, 2),
        "option_ema5": round(option_ema5, 2),
        "spot_ema9": round(spot_ema9, 2),
        "spot_ema5": round(spot_ema5, 2),
        "delta_velocity": round(delta_velocity, 3),
        "prior_low": round(prior_low, 2),
        "prior_high": round(prior_high, 2),
        "obi_5": round(obi, 3),
        "weighted_obi_5": round(weighted_obi, 4),
        "tbq_tsq": round(tbq_tsq, 3),
        "futures_ratio": round(futures_ratio, 3),
        "futures_source": futures_source,
        "upside_prob": round(up, 2),
        "downside_prob": round(down, 2),
        "probability": round(probability, 2),
        "pcr": round(pcr, 4),
        "paths_bullish": paths_bullish,
        "paths_bearish": paths_bearish,
        "p10": round(p10, 2),
        "p50": round(p50, 2),
        "p90": round(p90, 2),
        "ev": ev,
        "smc": smc,
        "primary": primary,
        "micro": micro,
        "call_oi_change_pct": _oi_change_pct(_leg_quote(row, "CE")),
        "put_oi_change_pct": _oi_change_pct(_leg_quote(row, "PE")),
    }


def _angle(
    *, key: str, index: int, label: str, short: str, side: str, score: float,
    conditions: list[dict[str, Any]], reason: str,
) -> dict[str, Any]:
    trigger = bool(conditions) and all(row["pass"] for row in conditions)
    return {
        "key": key,
        "index": index,
        "angle": f"ANGLE {index}",
        "label": label,
        "short": short,
        "side": side,
        "score": round(max(0.0, min(100.0, score)), 1),
        "threshold": ENTRY_THRESHOLD,
        "trigger": trigger,
        "status": "FIRING" if trigger else "WATCHING",
        "conditions": conditions,
        "conditions_passed": sum(1 for row in conditions if row["pass"]),
        "conditions_total": len(conditions),
        "reason": reason,
    }


def evaluate_playbooks(
    feed: Mapping[str, Any] | None,
    *,
    symbol: str = "NIFTY",
    now: float | None = None,
    data_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate all four playbooks and return the winning executable order.

    The winning order is only a proposal.  The master arbitrator applies the
    live-data gate and the session's own risk/cooldown guards before entry.
    """
    moment = float(now if now is not None else time.time())
    feed = feed or {}
    ctx = _context(feed, symbol)
    side = ctx["side"]
    prob = ctx["probability"]
    obi = ctx["obi_5"]
    tbq = ctx["tbq_tsq"]
    futures = ctx["futures_ratio"]
    spot, vwap = ctx["spot"], ctx["vwap"]

    aligned = (spot < vwap) if side == "PE" else (spot > vwap)
    a1_conditions = [
        _condition("probability", "Directional Probability >= 75%", prob >= 75.0, f"{prob:.1f}%"),
        _condition("spot_vwap", "Spot Aligned With VWAP", aligned,
                   f"{spot:,.2f} {'<' if side == 'PE' else '>'} VWAP {vwap:,.2f}"),
        _condition("obi_5", "Option OBI_5 >= 1.30", obi >= 1.30, f"{obi:.2f}x"),
        _condition("tbq_tsq", "TBQ/TSQ >= 1.25", tbq >= 1.25, f"{tbq:.2f}x"),
        _condition("futures_ratio", "Futures Volume >= 1.05x", futures >= 1.05, f"{futures:.2f}x"),
    ]
    a1_score = 100.0 * (
        0.45 * directional_conviction_score(prob)
        + 0.10 * (1.0 if aligned else 0.0)
        + 0.20 * _clamp((obi - 1.0) / 2.0)
        + 0.15 * _clamp((tbq - 1.0) / 1.5)
        + 0.10 * _clamp((futures - 1.0) / 1.0)
    )

    above_ema = spot > ctx["spot_ema9"] if side == "CE" else spot < ctx["spot_ema9"]
    above_vwap = spot > vwap if side == "CE" else spot < vwap
    delta_ok = ctx["delta_velocity"] > 0
    a2_conditions = [
        _condition("obi_5", "Institutional OBI_5 >= 1.75", obi >= 1.75, f"{obi:.2f}x"),
        _condition("tbq_tsq", "Institutional TBQ/TSQ >= 1.60", tbq >= 1.60, f"{tbq:.2f}x"),
        _condition("ema9_break", "1m EMA-9 Directional Break", above_ema,
                   f"Spot {spot:,.2f} vs EMA9 {ctx['spot_ema9']:,.2f}"),
        _condition("vwap_break", "1m VWAP Directional Break", above_vwap,
                   f"Spot {spot:,.2f} vs VWAP {vwap:,.2f}"),
        _condition("delta_velocity", "Positive Delta Velocity", delta_ok,
                   f"{ctx['delta_velocity']:+.2f} pts/candle"),
        _condition("probability", "Directional Probability >= 68%", prob >= 68.0, f"{prob:.1f}%"),
    ]
    momentum = sum(1.0 for row in (above_ema, above_vwap, delta_ok) if row) / 3.0
    a2_score = 100.0 * (
        0.25 * directional_conviction_score(prob)
        + 0.25 * _clamp((obi - 1.0) / 4.0)
        + 0.20 * _clamp((tbq - 1.0) / 2.0)
        + 0.30 * momentum
    )

    at_floor = spot <= ctx["p10"] if side == "PE" and ctx["p10"] > 0 else (
        spot >= ctx["p90"] if side == "CE" and ctx["p90"] > 0 else False
    )
    sweeps = list(ctx["smc"].get("sweeps") or [])
    sweep_ok = bool(sweeps)
    primary = ctx["primary"]
    micro = ctx["micro"]
    choch = (
        str(primary.get("active_choch") or "").upper().startswith("BEARISH" if side == "PE" else "BULLISH")
        or str(micro.get("active_choch") or "").upper().startswith("BEARISH" if side == "PE" else "BULLISH")
    )
    absorption = bool(ctx["smc"].get("order_blocks"))
    structure_ok = bool(choch or absorption)
    a3_conditions = [
        _condition("extreme_band", "p10 Floor / p90 Ceiling", at_floor,
                   f"Spot {spot:,.2f} · p10 {ctx['p10']:,.2f} · p90 {ctx['p90']:,.2f}"),
        _condition("liquidity_sweep", "Liquidity Sweep", sweep_ok,
                   f"{len(sweeps)} sweep(s)"),
        _condition("choch_ob", "1m CHoCH / Order Block Absorption", structure_ok,
                   "CHoCH + absorption" if structure_ok else "No valid structure shift"),
        _condition("obi_5", "Sweep OBI_5 >= 1.20", obi >= 1.20, f"{obi:.2f}x"),
    ]
    a3_score = 100.0 * (
        0.25 * (1.0 if at_floor else 0.0)
        + 0.30 * (1.0 if sweep_ok else 0.0)
        + 0.30 * (1.0 if structure_ok else 0.0)
        + 0.15 * _clamp((obi - 1.0) / 2.0)
    )

    if side == "PE":
        threatened_oi, opposite_oi = ctx["call_oi_change_pct"], ctx["put_oi_change_pct"]
        threatened_side, opposite_side = "CE", "PE"
    else:
        threatened_oi, opposite_oi = ctx["put_oi_change_pct"], ctx["call_oi_change_pct"]
        threatened_side, opposite_side = "PE", "CE"
    writers_unwind = threatened_oi <= -2.5
    opposite_build = opposite_oi >= 3.5
    convexity_ok = ctx["ev"]["ev_ratio"] > 0.0
    a4_conditions = [
        _condition("threatened_oi", f"{threatened_side} 5m ΔOI <= -2.5%", writers_unwind,
                   f"{threatened_oi:+.2f}%"),
        _condition("opposite_oi", f"{opposite_side} 5m ΔOI >= +3.5%", opposite_build,
                   f"{opposite_oi:+.2f}%"),
        _condition("greek_convexity", "Greek Convexity Edge > 0", convexity_ok,
                   f"EV ratio {ctx['ev']['ev_ratio']:.2f}"),
    ]
    a4_score = 100.0 * (
        0.35 * (1.0 if writers_unwind else 0.0)
        + 0.35 * (1.0 if opposite_build else 0.0)
        + 0.30 * (1.0 if convexity_ok else 0.0)
    )

    angles = [
        _angle(key="TREND_SURF_PULLBACK", index=1, label="🌊 Trend Surf", short="TREND SURF",
               side=side, score=a1_score, conditions=a1_conditions,
               reason=f"{prob:.1f}% directional · Spot {'<' if side == 'PE' else '>'} VWAP · OBI {obi:.2f}x"),
        _angle(key="ORDERFLOW_MOMENTUM_IGNITION", index=2, label="⚡ OrderFlow Burst",
               short="ORDERFLOW MOMENTUM IGNITION", side=side, score=a2_score,
               conditions=a2_conditions,
               reason=f"Whale book {obi:.2f}x / {tbq:.2f}x · 1m break {'confirmed' if momentum >= 2/3 else 'waiting'}"),
        _angle(key="SMC_EXTREME_REVERSAL_SNIPER", index=3, label="🎯 SMC Sweep",
               short="SMC EXTREME REVERSAL SNIPER", side=side, score=a3_score,
               conditions=a3_conditions,
               reason=f"p10/p90 band {'hit' if at_floor else 'not hit'} · {len(sweeps)} sweep(s)"),
        _angle(key="WRITER_TRAP_GAMMA_SQUEEZE", index=4, label="🔥 OI Squeeze",
               short="WRITER TRAP GAMMA SQUEEZE", side=side, score=a4_score,
               conditions=a4_conditions,
               reason=f"{threatened_side} ΔOI {threatened_oi:+.2f}% · {opposite_side} ΔOI {opposite_oi:+.2f}%"),
    ]
    triggered = [row for row in angles if row["trigger"] and row["score"] >= ENTRY_THRESHOLD]
    winner = max(triggered, key=lambda row: row["score"]) if triggered else None

    entry = ctx["option_ltp"]
    risk = max(2.0, min(9.0, round(ctx["atr"] * 0.5, 2) if ctx["atr"] > 0 else 6.0))
    if entry <= 0:
        risk = 0.0
    order = {
        "side": side,
        "option_type": side,
        "symbol": symbol,
        "strike": ctx["strike"],
        "contract": ctx["contract"],
        "entry_price": round(entry, 2),
        "stop_loss": round(entry - risk, 2) if entry > 0 else 0.0,
        "target_1": round(entry + 1.5 * risk, 2) if entry > 0 else 0.0,
        "target_2": round(entry + 3.0 * risk, 2) if entry > 0 else 0.0,
        "risk_points": round(risk, 2),
        "qty": 300,
        "playbook_id": winner["key"] if winner else "",
        "playbook_index": winner["index"] if winner else 0,
        "playbook_label": winner["short"] if winner else "",
        "score": winner["score"] if winner else 0.0,
        "entry_context": {
            "probability": prob,
            "paths_bullish_of_100": ctx["paths_bullish"],
            "paths_bearish_of_100": ctx["paths_bearish"],
            "spot": spot,
            "vwap": vwap,
            "obi_5": obi,
            "weighted_obi_5": ctx["weighted_obi_5"],
            "tbq_tsq": tbq,
            "futures_ratio": futures,
            "futures_source": ctx["futures_source"],
            "pcr": ctx["pcr"],
            "ev_ratio": ctx["ev"]["ev_ratio"],
            "spread_inr": round(max(0.0, ctx["option"]["ask"] - ctx["option"]["bid"]), 2),
            "source": ctx["option"]["source"],
        },
    }
    gate_allowed = True
    if isinstance(data_gate, Mapping):
        gate_allowed = bool(data_gate.get("allowed", True))
    ev_approved = bool(ctx["ev"]["approved"])
    auto_execute = bool(winner and entry > 0 and gate_allowed and ev_approved)
    auto_reason = (
        f"🎯 AUTO-PICKED: {ctx['contract']} · {ctx['paths_bearish'] if side == 'PE' else ctx['paths_bullish']}/100 Paths "
        f"{'Bearish' if side == 'PE' else 'Bullish'} · Spot {'<' if side == 'PE' else '>'} VWAP · "
        f"{side} OBI {obi:.2f}x"
        if side else "WAITING FOR DIRECTION"
    )
    headline = (
        f"✅ AUTO-EXECUTED [ANGLE {winner['index']}: {winner['short']}] · BUY {ctx['contract']} @ ₹{entry:,.2f}"
        if auto_execute and winner else "⏳ NO PLAYBOOK ABOVE THRESHOLD — WATCHING ALL FOUR ANGLES"
    )
    return {
        "ok": bool(auto_execute),
        "symbol": symbol,
        "as_of": str(feed.get("updated_at") or datetime.fromtimestamp(moment, tz=IST).isoformat(timespec="seconds")),
        "threshold": ENTRY_THRESHOLD,
        "side": side,
        "strike": ctx["strike"],
        "contract": ctx["contract"],
        "probability": prob,
        "paths_bullish_of_100": ctx["paths_bullish"],
        "paths_bearish_of_100": ctx["paths_bearish"],
        "spot": spot,
        "vwap": vwap,
        "obi_5": obi,
        "weighted_obi_5": ctx["weighted_obi_5"],
        "tbq_tsq": tbq,
        "futures_ratio": futures,
        "ev_ratio": ctx["ev"]["ev_ratio"],
        "ev_approved": ev_approved,
        "angles": angles,
        "radar": [
            {
                "id": row["key"], "angle": row["angle"], "label": row["label"],
                "score": row["score"], "trigger": row["trigger"], "status": row["status"],
            }
            for row in angles
        ],
        "winner": winner,
        "winner_id": winner["key"] if winner else "",
        "auto_execute": auto_execute,
        "data_gate_allowed": gate_allowed,
        "auto_pick_reason": auto_reason,
        "headline": headline,
        "entry": order,
    }


def playbook_radar(feed: Mapping[str, Any] | None, *, symbol: str = "NIFTY") -> list[dict[str, Any]]:
    """Lightweight radar rows for the fast dashboard path."""
    return evaluate_playbooks(feed, symbol=symbol).get("radar") or []


__all__ = [
    "ENTRY_THRESHOLD",
    "PLAYBOOK_IDS",
    "PLAYBOOK_META",
    "evaluate_playbooks",
    "playbook_radar",
]
