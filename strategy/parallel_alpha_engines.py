"""Five parallel quantitative microstructure engines.

The module is deliberately pure: the live session supplies 250ms-updated
futures/option/heavyweight fields and receives deterministic order intents and
radar rows.  The capital sleeves remain owned by the session; this module never
invents a position or a P&L number.

Engines
-------
``parity``       Put-call parity discount sniper.
``lead_lag``     Heavyweight/BankNifty 3-second lead-lag detector.
``tsunami``      5/5 heavyweight order-flow synchronization.
``coiled_spring`` 30-minute volatility compression breakout trap.
``rotator``      NIFTY/BANKNIFTY 0–100 opportunity allocator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import floor
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

PARITY_THRESHOLD_NIFTY = 3.50
PARITY_THRESHOLD_BANKNIFTY = 8.00
PARITY_NORMALIZED = 0.50
PARITY_MIN_OBI = 1.40
LEAD_LAG_LAG_THRESHOLD = 1.50
LEAD_LAG_VTT_RATIO = 2.20
TSUNAMI_TARGET_1 = 15.00
TSUNAMI_TARGET_2 = 28.00
COMPRESSION_MAX_WIDTH = 22.0
COMPRESSION_MIN_MINUTES = 30
BREAKOUT_BUFFER = 3.0
COILED_VTT_RATIO = 2.0
SCALP_SLEEVE = 35_000.0
SNIPER_SLEEVE = 65_000.0
ENGINE_ORDER = ("parity", "lead_lag", "tsunami", "coiled_spring", "rotator")
DEFAULT_HEAVYWEIGHTS = ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_ist(moment: datetime | str | float | None = None) -> datetime:
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


def _ratio(tbq: Any, tsq: Any) -> float:
    seller = _f(tsq)
    return (_f(tbq) / seller) if seller > 0 else 0.0


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "bull", "bullish", "bear", "bearish"}
    return bool(value)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _normalize_metric(value: Any, *, ceiling: float) -> float:
    """Accept a 0–1 ratio, a 0–100 score, or a raw points/ratio value.

    Raw market units are scaled by their practical ceiling (for example an OBI
    of 1.50 against a 3.00 ceiling is 50 points).  Callers that already hold a
    normalised 0–100 component keep it as-is.
    """
    number = abs(_f(value))
    if number <= 1.0:
        return _clamp(number * 100.0)
    if ceiling and number <= ceiling:
        return _clamp(number / ceiling * 100.0)
    return _clamp(number)


# ---------------------------------------------------------------------------
# 1. Put-call parity
# ---------------------------------------------------------------------------


def put_call_parity(
    futures_ltp: Any,
    strike: Any,
    ce_ltp: Any,
    pe_ltp: Any,
) -> dict[str, Any]:
    """Compute C/P fair values and the rupee discount on each leg."""
    futures_price = _f(futures_ltp)
    strike_price = _f(strike)
    call = _f(ce_ltp)
    put = _f(pe_ltp)
    basis = futures_price - strike_price if futures_price > 0 else 0.0
    fair_ce = put + basis if put > 0 else 0.0
    fair_pe = call - basis if call > 0 else 0.0
    return {
        "futures_ltp": round(futures_price, 2),
        "strike": round(strike_price, 2),
        "basis": round(basis, 2),
        "ce_ltp": round(call, 2),
        "pe_ltp": round(put, 2),
        "fair_ce": round(fair_ce, 2),
        "fair_pe": round(fair_pe, 2),
        "ce_discount": round(fair_ce - call, 2) if fair_ce > 0 else 0.0,
        "pe_discount": round(fair_pe - put, 2) if fair_pe > 0 else 0.0,
    }


def parity_dislocation_snipe(
    futures_ltp: Any,
    strike: Any,
    ce_ltp: Any,
    pe_ltp: Any,
    *,
    symbol: str = "NIFTY",
    ce_obi_5: Any = 0.0,
    pe_obi_5: Any = 0.0,
    kronos_bullish: bool = False,
    kronos_bearish: bool = False,
    obi_5: Any = 0.0,
) -> dict[str, Any]:
    """Fire when the futures move leaves one ATM premium cheap by ₹3.50/₹8."""
    symbol = str(symbol or "NIFTY").upper()
    threshold = (PARITY_THRESHOLD_BANKNIFTY if symbol == "BANKNIFTY"
                 else PARITY_THRESHOLD_NIFTY)
    values = put_call_parity(futures_ltp, strike, ce_ltp, pe_ltp)
    ce_obi = _f(ce_obi_5, _f(obi_5))
    pe_obi = _f(pe_obi_5, _f(obi_5))
    ce_edge = _f(values["ce_discount"])
    pe_edge = _f(values["pe_discount"])
    bull = bool(kronos_bullish)
    bear = bool(kronos_bearish)
    ce_ready = ce_edge >= threshold and ce_obi >= PARITY_MIN_OBI and bull
    pe_ready = pe_edge >= threshold and pe_obi >= PARITY_MIN_OBI and bear
    side = ""
    edge = 0.0
    if ce_ready and (not pe_ready or ce_edge >= pe_edge):
        side, edge = "CE", ce_edge
    elif pe_ready:
        side, edge = "PE", pe_edge
    entry = _f(values["ce_ltp"] if side == "CE" else values["pe_ltp"])
    normalized = bool(
        side and max(ce_edge if side == "CE" else pe_edge, 0.0) <= PARITY_NORMALIZED
    )
    return {
        "engine": "parity",
        "name": "Put-Call Parity",
        "trigger": bool(side),
        "side": side,
        "symbol": symbol,
        "edge": round(edge, 2),
        "ce_edge": round(ce_edge, 2),
        "pe_edge": round(pe_edge, 2),
        "ce_obi_5": round(ce_obi, 3),
        "pe_obi_5": round(pe_obi, 3),
        "threshold": threshold,
        "fair": values,
        "entry": round(entry, 2),
        "target_1": round(entry + 5.50, 2) if entry else 0.0,
        "target_2": round(entry + 9.00, 2) if entry else 0.0,
        "normalized": normalized,
        "exit_rule": "PARITY_NORMALIZED" if normalized else "HOLD_FOR_GAP_CLOSE",
        "reason": ("PARITY_DISLOCATION_SNIPE" if side else
                   "NO_PARITY_DISLOCATION"),
        "status": (
            f"⚖️ Parity Edge: {side or '—'} ₹{edge:.2f} Cheap"
            + (" 🟢" if side else "")
        ),
        "engine_state": "TRIGGERED" if side else "SCANNING",
    }


# ---------------------------------------------------------------------------
# 2. Lead-lag
# ---------------------------------------------------------------------------


def _leader_map(leaders: Any) -> dict[str, dict[str, Any]]:
    if isinstance(leaders, dict):
        return {str(name).upper(): dict(row or {}) for name, row in leaders.items()
                if isinstance(row, dict)}
    out: dict[str, dict[str, Any]] = {}
    for row in leaders or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("symbol") or "").upper()
        if name:
            merged = dict(row)
            merged["name"] = name
            out[name] = merged
    return out


def _leader_row(row: dict[str, Any]) -> dict[str, Any]:
    ltp = _f(row.get("ltp") or row.get("price"))
    high = _f(row.get("one_min_high") or row.get("high_1m") or row.get("high"))
    low = _f(row.get("one_min_low") or row.get("low_1m") or row.get("low"))
    velocity = _f(row.get("velocity_3s") or row.get("delta_3s") or row.get("move_3s"))
    vtt = _f(row.get("vtt_ratio") or row.get("volume_velocity")
             or row.get("volume_ratio") or row.get("vtt_3s_ratio"))
    high_break = row.get("high_break")
    low_break = row.get("low_break")
    if high_break is None:
        high_break = bool(ltp > 0 and high > 0 and ltp >= high)
    if low_break is None:
        low_break = bool(ltp > 0 and low > 0 and ltp <= low)
    return {
        "name": str(row.get("name") or row.get("symbol") or "").upper(),
        "ltp": round(ltp, 2),
        "velocity_3s": round(velocity, 2),
        "vtt_ratio": round(vtt, 2),
        "high_break": bool(high_break),
        "low_break": bool(low_break),
        "tbq": _f(row.get("tbq")),
        "tsq": _f(row.get("tsq")),
    }


def lead_lag_snipe(
    leaders: Any,
    *,
    lagging_option_move_3s: Any = 99.0,
    lagging_obi_5: Any = 0.0,
    lagging_side: str = "",
    symbol: str = "NIFTY",
    leaders_required: tuple[str, ...] | list[str] | None = None,
    vtt_ratio_min: float = LEAD_LAG_VTT_RATIO,
    lag_threshold: float = LEAD_LAG_LAG_THRESHOLD,
) -> dict[str, Any]:
    """Detect a synchronized leader break while the target option is asleep."""
    mapped = _leader_map(leaders)
    rows = {name: _leader_row(row) for name, row in mapped.items()}
    required = tuple(str(name).upper().replace(" FUT", "").replace("_FUT", "")
                     for name in (leaders_required or ("HDFCBANK", "ICICIBANK", "BANKNIFTY")))
    required = tuple("BANKNIFTY" if name == "BANKNIFTYFUT" else name for name in required)
    # A caller may pass a custom three-leader basket.
    chosen = [rows[name] for name in required if name in rows]
    if len(chosen) < 3:
        chosen = list(rows.values())
    bull = [row for row in chosen
            if row["velocity_3s"] > 0 and row["high_break"]
            and row["vtt_ratio"] >= vtt_ratio_min]
    bear = [row for row in chosen
            if row["velocity_3s"] < 0 and row["low_break"]
            and row["vtt_ratio"] >= vtt_ratio_min]
    lagging = abs(_f(lagging_option_move_3s)) < lag_threshold
    side = ""
    if lagging and len(bull) >= 3:
        side = "CE"
    if lagging and len(bear) >= 3 and (not side or len(bear) > len(bull)):
        side = "PE"
    if lagging_side:
        requested = str(lagging_side).upper()
        if requested in {"CE", "PE"}:
            side = requested if (requested == "CE" and len(bull) >= 3) or (
                requested == "PE" and len(bear) >= 3) else ""
    leader_side = "BULL" if side == "CE" else "BEAR" if side == "PE" else ""
    active_leaders = bull or bear
    # BANKNIFTY is the headline lead when it is in the basket; otherwise use the
    # first confirmed leader (custom baskets such as RELIANCE/INFY/TCS).
    lead_row = (next((row for row in active_leaders if row["name"] == "BANKNIFTY"),
                     active_leaders[0])
                if active_leaders else (chosen[0] if chosen else {}))
    move_pct = 0.0
    if lead_row:
        base = _f(lead_row.get("ltp"))
        move_pct = (lead_row["velocity_3s"] / base * 100.0) if base else 0.0
    return {
        "engine": "lead_lag",
        "name": "Lead-Lag",
        "trigger": bool(side and lagging),
        "side": side,
        "symbol": str(symbol or "NIFTY").upper(),
        "leader_side": leader_side,
        "leader_move_pct": round(move_pct, 2),
        "leaders": chosen,
        "bull_leaders": len(bull),
        "bear_leaders": len(bear),
        "lagging_option_move_3s": round(_f(lagging_option_move_3s), 2),
        "lagging_obi_5": round(_f(lagging_obi_5), 3),
        "lag_threshold": lag_threshold,
        "vtt_ratio_min": vtt_ratio_min,
        "reason": "LEAD_LAG_SNIPE" if side else "NO_LEAD_LAG_DIVERGENCE",
        "status": (
            f"⚡ Lead-Lag: {lead_row.get('name', '—')} "
            f"{'Leading' if side == 'CE' else 'Bear-Leading' if side == 'PE' else 'Watching'} "
            f"{move_pct:+.2f}%"
            + (" 🟢" if side else "")
        ),
        "engine_state": "TRIGGERED" if side else "SCANNING",
    }


# ---------------------------------------------------------------------------
# 3. 5/5 heavyweight tsunami
# ---------------------------------------------------------------------------


def _stock_sync(row: dict[str, Any]) -> dict[str, Any]:
    ltp = _f(row.get("ltp") or row.get("price"))
    atp = _f(row.get("atp") or row.get("vwap"))
    candle = row.get("candle") or {}
    open_1m = _f(row.get("open_1m") or row.get("open") or candle.get("open"))
    close_1m = _f(row.get("close_1m") or row.get("close") or candle.get("close") or ltp)
    tbq = _f(row.get("tbq"))
    tsq = _f(row.get("tsq"))
    bull = bool(ltp > 0 and atp > 0 and ltp > atp and close_1m > open_1m
                and tbq >= 1.25 * tsq)
    bear = bool(ltp > 0 and atp > 0 and ltp < atp and close_1m < open_1m
                and tsq >= 1.25 * tbq)
    return {
        "name": str(row.get("name") or row.get("symbol") or "").upper(),
        "ltp": round(ltp, 2),
        "atp": round(atp, 2),
        "open_1m": round(open_1m, 2),
        "close_1m": round(close_1m, 2),
        "tbq": round(tbq, 2),
        "tsq": round(tsq, 2),
        "bull_sync": bull,
        "bear_sync": bear,
    }


def _stock_map(stocks: Any) -> dict[str, dict[str, Any]]:
    if isinstance(stocks, dict):
        return {str(name).upper(): dict(row or {}) for name, row in stocks.items()
                if isinstance(row, dict)}
    out: dict[str, dict[str, Any]] = {}
    for row in stocks or []:
        if isinstance(row, dict):
            name = str(row.get("name") or row.get("symbol") or "").upper()
            if name:
                out[name] = dict(row)
    return out


def heavyweight_tsunami(
    stocks: Any,
    *,
    previous_bull_count: int = 0,
    previous_bear_count: int = 0,
    kronos_bullish: bool = False,
    kronos_bearish: bool = False,
    direction: str = "",
) -> dict[str, Any]:
    """Fire only when all five heavyweights agree and KRONOS is aligned."""
    mapped = _stock_map(stocks)
    rows = [_stock_sync(row) for row in mapped.values()]
    bull_count = sum(1 for row in rows if row["bull_sync"])
    bear_count = sum(1 for row in rows if row["bear_sync"])
    all_five = len(rows) == 5
    requested = str(direction or "").upper()
    bull_ok = bool(kronos_bullish or requested in {"BULL", "BULLISH", "CE"})
    bear_ok = bool(kronos_bearish or requested in {"BEAR", "BEARISH", "PE"})
    bull_transition = all_five and bull_count == 5 and previous_bull_count < 5 and bull_ok
    bear_transition = all_five and bear_count == 5 and previous_bear_count < 5 and bear_ok
    side = "CE" if bull_transition else "PE" if bear_transition else ""
    aligned = "BULL" if bull_transition else "BEAR" if bear_transition else ""
    if aligned == "BULL":
        display_count, display_word = bull_count, "BULL"
    elif aligned == "BEAR":
        display_count, display_word = bear_count, "BEAR"
    elif all_five and bull_count == 5:
        display_count, display_word = bull_count, "BULL"
    elif all_five and bear_count == 5:
        display_count, display_word = bear_count, "BEAR"
    else:
        display_count, display_word = bull_count, "MIXED"
    return {
        "engine": "tsunami",
        "name": "5/5 Heavyweight Tsunami",
        "trigger": bool(side),
        "side": side,
        "aligned": aligned,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "total": len(rows),
        "all_bull": all_five and bull_count == 5,
        "all_bear": all_five and bear_count == 5,
        "fresh_transition": bool(bull_transition or bear_transition),
        "previous_bull_count": int(previous_bull_count),
        "previous_bear_count": int(previous_bear_count),
        "stocks": rows,
        "target_1": TSUNAMI_TARGET_1,
        "target_2": TSUNAMI_TARGET_2,
        "gear": 3,
        "reason": "HEAVYWEIGHT_5_OF_5_TSUNAMI" if side else "NO_5_OF_5_ALIGNMENT",
        "status": (
            f"🏛️ Heavyweights: {display_count}/5 {display_word}"
            + (" TSUNAMI 🔥" if side else "")
        ),
        "engine_state": "TRIGGERED" if side else "SCANNING",
    }


# ---------------------------------------------------------------------------
# 4. Coiled spring and dual-index rotator
# ---------------------------------------------------------------------------


def coiled_spring_trap(
    spot: Any,
    *,
    box_high: Any = 0.0,
    box_low: Any = 0.0,
    compression_minutes: Any = 0.0,
    straddle_ltp: Any = 0.0,
    straddle_intraday_low: Any = 0.0,
    futures_oi_rising: bool = False,
    futures_vtt_ratio: Any = 0.0,
    armed: bool | None = None,
) -> dict[str, Any]:
    """Arm a two-sided breakout bracket only when compression conditions hold."""
    price = _f(spot)
    high = _f(box_high)
    low = _f(box_low)
    width = (high - low) if high > 0 and low > 0 else 0.0
    straddle = _f(straddle_ltp)
    intraday_low = _f(straddle_intraday_low)
    compression_ok = bool(
        0 < width <= COMPRESSION_MAX_WIDTH
        and _f(compression_minutes) >= COMPRESSION_MIN_MINUTES
        and futures_oi_rising
        and (intraday_low <= 0 or straddle <= intraday_low * 1.01)
    )
    armed_now = bool(armed) or compression_ok
    upper = high + BREAKOUT_BUFFER if high > 0 else 0.0
    lower = low - BREAKOUT_BUFFER if low > 0 else 0.0
    side = ""
    if armed_now and price > 0:
        if upper > 0 and price >= upper and (_f(futures_vtt_ratio) >= COILED_VTT_RATIO):
            side = "CE"
        elif lower > 0 and price <= lower and (_f(futures_vtt_ratio) >= COILED_VTT_RATIO):
            side = "PE"
    return {
        "engine": "coiled_spring",
        "name": "Coiled Spring",
        "trigger": bool(side),
        "side": side,
        "armed": bool(armed_now),
        "compressed": compression_ok,
        "spot": round(price, 2),
        "box_high": round(high, 2),
        "box_low": round(low, 2),
        "box_width": round(width, 2),
        "compression_minutes": round(_f(compression_minutes), 1),
        "upper_trigger": round(upper, 2),
        "lower_trigger": round(lower, 2),
        "straddle_ltp": round(straddle, 2),
        "straddle_intraday_low": round(intraday_low, 2),
        "futures_oi_rising": bool(futures_oi_rising),
        "futures_vtt_ratio": round(_f(futures_vtt_ratio), 2),
        "cancel_opposite": True,
        "reason": "COILED_SPRING_TRAP" if side else (
            "COILED_SPRING_ARMED" if armed_now else "NO_COMPRESSION"
        ),
        "status": (
            f"🌀 Coiled Spring: {'Armed' if armed_now else 'Watching'} "
            f"@ {upper:,.0f} / {lower:,.0f}"
            + (" 🟢" if side else "")
        ),
        "engine_state": "TRIGGERED" if side else ("ARMED" if armed_now else "SCANNING"),
    }


def opportunity_score(metrics: dict[str, Any] | None) -> float:
    """Blend four 0–100 opportunity components into one 0–100 score."""
    values = metrics or {}
    atr = _normalize_metric(values.get("atr_velocity"), ceiling=20.0)
    obi = _normalize_metric(values.get("obi_strength"), ceiling=3.0)
    parity = _normalize_metric(values.get("parity_edge"), ceiling=10.0)
    kronos = _normalize_metric(values.get("kronos_confidence"), ceiling=100.0)
    return round(_clamp(atr * 0.30 + obi * 0.25 + parity * 0.20 + kronos * 0.25), 2)


def dual_index_rotator(
    nifty_metrics: dict[str, Any] | None = None,
    banknifty_metrics: dict[str, Any] | None = None,
    *,
    previous_winner: str = "",
    scalp_capital: Any = SCALP_SLEEVE,
    sniper_capital: Any = SNIPER_SLEEVE,
) -> dict[str, Any]:
    """Route both sleeve capacities to the higher-scoring index."""
    nifty_score = opportunity_score(nifty_metrics)
    bank_score = opportunity_score(banknifty_metrics)
    winner = "NIFTY" if nifty_score >= bank_score else "BANKNIFTY"
    if abs(nifty_score - bank_score) < 0.01 and previous_winner in {"NIFTY", "BANKNIFTY"}:
        winner = previous_winner
    loser = "BANKNIFTY" if winner == "NIFTY" else "NIFTY"
    winner_score = nifty_score if winner == "NIFTY" else bank_score
    loser_score = bank_score if winner == "NIFTY" else nifty_score
    scalp = max(0.0, min(_f(scalp_capital, SCALP_SLEEVE), SCALP_SLEEVE))
    sniper = max(0.0, min(_f(sniper_capital, SNIPER_SLEEVE), SNIPER_SLEEVE))
    return {
        "engine": "rotator",
        "name": "Dual-Index Rotator",
        "trigger": bool(previous_winner and previous_winner != winner),
        "winner": winner,
        "loser": loser,
        "rotated": bool(previous_winner and previous_winner != winner),
        "previous_winner": str(previous_winner or "").upper(),
        "scores": {"NIFTY": nifty_score, "BANKNIFTY": bank_score},
        "scalp_index": winner,
        "sniper_index": winner,
        "scalp_allocated": round(scalp, 2),
        "sniper_allocated": round(sniper, 2),
        "total_allocated": round(scalp + sniper, 2),
        "sleeve_limits": {"scalp": SCALP_SLEEVE, "sniper": SNIPER_SLEEVE},
        "reason": "INDEX_ROTATION" if previous_winner and previous_winner != winner
                  else "CAPITAL_ALREADY_ON_WINNER",
        "status": (
            f"🔄 Active Index Rotator: {winner} (Score {winner_score:g}) vs "
            f"{loser} (Score {loser_score:g})"
        ),
        "engine_state": "ROTATED" if previous_winner and previous_winner != winner else "ALIGNED",
    }


# ---------------------------------------------------------------------------
# Radar / orchestrator
# ---------------------------------------------------------------------------


def _empty_engine(name: str) -> dict[str, Any]:
    labels = {
        "parity": "⚖️ Parity Edge: Scanning",
        "lead_lag": "⚡ Lead-Lag: Scanning",
        "tsunami": "🏛️ Heavyweights: Waiting for 5/5",
        "coiled_spring": "🌀 Coiled Spring: Watching",
        "rotator": "🔄 Active Index Rotator: Waiting",
    }
    return {
        "engine": name, "name": name.replace("_", " ").title(),
        "trigger": False, "status": labels[name], "engine_state": "WAITING",
    }


def build_parallel_alpha(
    *,
    parity: dict[str, Any] | None = None,
    lead_lag: dict[str, Any] | None = None,
    tsunami: dict[str, Any] | None = None,
    coiled_spring: dict[str, Any] | None = None,
    rotator: dict[str, Any] | None = None,
    pnl: dict[str, Any] | None = None,
    now: datetime | str | float | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    """Assemble the five engine results into the radar/API snapshot."""
    engines = {
        "parity": parity or _empty_engine("parity"),
        "lead_lag": lead_lag or _empty_engine("lead_lag"),
        "tsunami": tsunami or _empty_engine("tsunami"),
        "coiled_spring": coiled_spring or _empty_engine("coiled_spring"),
        "rotator": rotator or _empty_engine("rotator"),
    }
    contributions = {
        key: round(_f((pnl or {}).get(key)), 2) for key in ENGINE_ORDER
    }
    active = [key for key, result in engines.items() if result.get("trigger")]
    radar = [
        {"key": key, "label": engines[key].get("status") or _empty_engine(key)["status"],
         "trigger": bool(engines[key].get("trigger")),
         "state": engines[key].get("engine_state") or "WAITING",
         "pnl": contributions[key]}
        for key in ENGINE_ORDER
    ]
    return {
        "ok": not stale,
        "stale": bool(stale),
        "as_of": _as_ist(now).isoformat(timespec="seconds"),
        "engine_count": len(engines),
        "active_engines": active,
        "active_count": len(active),
        "signals": [
            {"engine": key, "side": engines[key].get("side"),
             "symbol": engines[key].get("symbol") or "NIFTY"}
            for key in active
        ],
        "engines": engines,
        "radar": radar,
        "pnl_contributions": contributions,
        "total_pnl_contribution": round(sum(contributions.values()), 2),
        "parallel_ready": not stale,
        "engine_order": list(ENGINE_ORDER),
    }


@dataclass
class ParallelAlphaEngine:
    """Stateful wrapper preserving the 5/5 transition and index rotation."""

    previous_bull_count: int = 0
    previous_bear_count: int = 0
    previous_winner: str = ""
    last_snapshot: dict[str, Any] = field(default_factory=dict)

    def evaluate(
        self,
        *,
        parity_data: dict[str, Any] | None = None,
        lead_lag_data: dict[str, Any] | None = None,
        tsunami_stocks: Any = None,
        tsunami_direction: str = "",
        coiled_data: dict[str, Any] | None = None,
        rotator_data: dict[str, Any] | None = None,
        pnl: dict[str, Any] | None = None,
        stale: bool = False,
        now: datetime | str | float | None = None,
    ) -> dict[str, Any]:
        parity = (parity_dislocation_snipe(**parity_data)
                  if parity_data is not None else None)
        lead_lag = (lead_lag_snipe(**lead_lag_data)
                    if lead_lag_data is not None else None)
        tsunami = None
        if tsunami_stocks is not None:
            tsunami = heavyweight_tsunami(
                tsunami_stocks,
                previous_bull_count=self.previous_bull_count,
                previous_bear_count=self.previous_bear_count,
                direction=tsunami_direction,
            )
            self.previous_bull_count = int(tsunami.get("bull_count") or 0)
            self.previous_bear_count = int(tsunami.get("bear_count") or 0)
        coiled = coiled_spring_trap(**coiled_data) if coiled_data is not None else None
        rotator = None
        if rotator_data is not None:
            rotator = dual_index_rotator(
                rotator_data.get("nifty_metrics"),
                rotator_data.get("banknifty_metrics"),
                previous_winner=str(rotator_data.get("previous_winner") or self.previous_winner),
                scalp_capital=rotator_data.get("scalp_capital", SCALP_SLEEVE),
                sniper_capital=rotator_data.get("sniper_capital", SNIPER_SLEEVE),
            )
            self.previous_winner = str(rotator.get("winner") or "")
        self.last_snapshot = build_parallel_alpha(
            parity=parity, lead_lag=lead_lag, tsunami=tsunami,
            coiled_spring=coiled, rotator=rotator, pnl=pnl,
            now=now, stale=stale,
        )
        return self.last_snapshot


__all__ = [
    "COMPRESSION_MAX_WIDTH",
    "DEFAULT_HEAVYWEIGHTS",
    "ENGINE_ORDER",
    "PARITY_THRESHOLD_BANKNIFTY",
    "PARITY_THRESHOLD_NIFTY",
    "ParallelAlphaEngine",
    "build_parallel_alpha",
    "coiled_spring_trap",
    "dual_index_rotator",
    "heavyweight_tsunami",
    "lead_lag_snipe",
    "opportunity_score",
    "parity_dislocation_snipe",
    "put_call_parity",
]
