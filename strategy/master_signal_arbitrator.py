"""Zero-conflict master signal arbitrator for the dual-sleeve desk.

Every engine remains independently useful, but only one candidate can own each
risk sleeve at a time.  The arbitrator ranks candidates with a single
institutional edge score and fails closed when the feed is stale, a fixture, or
materially zero.  It never creates a candidate to fill an empty sleeve.

Edge score
----------
``edge_score = 100 * roi_factor * confidence_factor * flow_factor``

where each factor is clamped to ``[0, 1]``.  The factors are returned with the
score so the dashboard and the readiness report can audit every decision.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from config.settings import OPTION_MIN_CANDLES, QUORUM_CONFIDENCE_FLOOR, SPOT_MIN_5M_CANDLES
from model.tri_model_orchestrator import build_model_quorum
from strategy.intra_candle_sniper import option_candle_readiness
from strategy.multi_angle_playbook_engine import evaluate_playbooks

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SNIPER_THRESHOLD = 78.0
SCALP_THRESHOLD = 72.0
SNIPER_SLEEVE_RS = 65_000.0
SCALP_SLEEVE_RS = 35_000.0
SLEEVE_B_ENGINES = (
    "REVERSAL_SMC_SNIPER",
    "HEAVYWEIGHT_5_OF_5_TSUNAMI",
    "COILED_SPRING_BREAKOUT",
)
SLEEVE_A_ENGINES = (
    "PARITY_DISLOCATION_SNIPE",
    "LEAD_LAG_3S_SNIPE",
    "ADAPTIVE_VELOCITY_PING_PONG",
)
LIVE_STALE_AFTER_SEC = 12.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _pct(value: Any, default: float = 0.0) -> float:
    out = _f(value, default)
    return _clamp(out / 100.0)


def _flow_velocity(obi_5: Any, tbq_tsq: Any, futures_ratio: Any) -> float:
    """Normalise order-flow velocity to 0..1 without inventing a print."""
    obi = max(0.0, _f(obi_5, 1.0))
    tbq = max(0.0, _f(tbq_tsq, 1.0))
    futures = max(0.0, _f(futures_ratio, 1.0))
    if obi <= 0 and tbq <= 0 and futures <= 0:
        return 0.0
    # 1.00 is neutral. A 1.50 book and 1.5x futures volume is a full-velocity
    # print; lower activity scales linearly.
    score = (min(obi, 2.0) - 1.0) * 0.45 + (min(tbq, 2.0) - 1.0) * 0.35 + (
        min(futures, 3.0) - 1.0
    ) * 0.20
    return _clamp(score + 0.35)


def edge_score(
    net_expected_roi_pct: Any,
    probability_confidence_pct: Any,
    order_flow_velocity: Any,
) -> dict[str, Any]:
    """Return the exact 0-100 score and all three factors."""
    roi = _clamp(_f(net_expected_roi_pct) / 2.5)
    confidence = _pct(probability_confidence_pct)
    flow = _clamp(_f(order_flow_velocity))
    score = round(100.0 * roi * confidence * flow, 2)
    return {
        "edge_score": score,
        "roi_factor": round(roi, 4),
        "confidence_factor": round(confidence, 4),
        "flow_factor": round(flow, 4),
    }


def _core_market_fields(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    spots = feed.get("spot") or {}
    spot_block = spots.get(symbol) if isinstance(spots, Mapping) else {}
    spot_block = spot_block if isinstance(spot_block, Mapping) else {}
    options = feed.get("option") or {}
    option_block = options.get(symbol) if isinstance(options, Mapping) else {}
    if not isinstance(option_block, Mapping):
        option_block = options if isinstance(options, Mapping) else {}
    spot_ltp = _f(spot_block.get("ltp") or spot_block.get("display"))
    option_ltp = _f(option_block.get("ltp") or (option_block.get("market_data") or {}).get("ltp"))
    return {
        "spot_ltp": round(spot_ltp, 2),
        "option_ltp": round(option_ltp, 2),
        "spot_present": spot_ltp > 0,
        "option_present": option_ltp > 0,
    }


def _data_gate(
    feed: Mapping[str, Any],
    *,
    now: float,
    symbol: str,
    feed_live: bool,
) -> dict[str, Any]:
    # Choke #5: one stale field among dozens may not zero every candidate.
    # The only liveness facts are the live Spot LTP, live Option LTP and the
    # websocket LKG heartbeat.  The heavy chain snapshot may lag up to 12s.
    epoch = _f(feed.get("liveness_epoch") or feed.get("updated_epoch"))
    age_sec = max(0.0, now - epoch) if epoch > 0 else None
    fixture = bool(feed.get("fixture"))
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    fixture = fixture or bool(spectrum.get("fixture"))
    zero_feed = not bool(feed.get("spot") or feed.get("full_spectrum"))
    core = _core_market_fields(feed, symbol)
    last_good_age_ok = age_sec is not None and age_sec <= LIVE_STALE_AFTER_SEC
    live = bool(
        feed_live
        and not fixture
        and core["spot_present"]
        and core["option_present"]
        and last_good_age_ok
    )
    lkg_reason = (
        f"CORE_FIELDS_LKG_{age_sec:.3f}S_LE_12S" if live else
        f"LKG_AGE_{age_sec:.3f}S_GT_12S" if age_sec is not None else
        "NO_FEED_HEARTBEAT"
    )
    if live:
        reason = "SANITIZED_LIVE_FEED"
    elif fixture:
        reason = "FIXTURE_BLOCKED"
    elif zero_feed:
        reason = "ZERO_OR_MISSING_MARKET_DATA"
    elif not core["spot_present"] or not core["option_present"]:
        reason = "CORE_SPOT_OR_OPTION_LTP_MISSING"
    elif not feed_live:
        reason = "FEED_OFFLINE_OR_STALE"
    elif age_sec is None:
        reason = "NO_FEED_HEARTBEAT"
    else:
        reason = lkg_reason
    return {
        "allowed": live,
        "live": live,
        "status": "LIVE" if live else "BLOCKED",
        "reason": reason,
        "lkg_detail": lkg_reason,
        "age_sec": round(age_sec, 3) if age_sec is not None else None,
        "lkg_window_sec": LIVE_STALE_AFTER_SEC,
        "core_fields": core,
        "field_lag_applies_to_scores": False,
        "fixture": fixture,
        "zero_feed": zero_feed,
    }


def _candidate(
    *,
    engine: str,
    sleeve: str,
    side: str = "",
    trigger: bool = False,
    data_ok: bool = True,
    net_roi_pct: Any = 0.0,
    confidence_pct: Any = 0.0,
    obi_5: Any = 1.0,
    tbq_tsq: Any = 1.0,
    futures_ratio: Any = 1.0,
    entry: Any = 0.0,
    target_1: Any = 0.0,
    target_2: Any = 0.0,
    reason: str = "",
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    score = edge_score(net_roi_pct, confidence_pct, _flow_velocity(obi_5, tbq_tsq, futures_ratio))
    # A minor field lag must not erase the real candidate score.  Side, trigger
    # and entry are retained and ranked; the 12s core gate decides execution.
    valid = bool(trigger and side in {"CE", "PE"} and _f(entry) > 0)
    return {
        "engine": engine,
        "sleeve": sleeve,
        "side": side if side in {"CE", "PE"} else "",
        "trigger": bool(trigger),
        "data_ok": bool(data_ok),
        "valid": valid,
        "net_expected_roi_after_friction_pct": round(_f(net_roi_pct), 4),
        "probability_confidence_pct": round(_f(confidence_pct), 2),
        "order_flow_velocity": round(_flow_velocity(obi_5, tbq_tsq, futures_ratio), 4),
        "entry": round(_f(entry), 2),
        "target_1": round(_f(target_1), 2),
        "target_2": round(_f(target_2), 2),
        "reason": reason or ("SIGNAL_READY" if valid else "NO_TRIGGER"),
        "source": dict(source or {}),
        **score,
    }


def _sniper_candidate(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    all_sniper = feed.get("sniper") or {}
    sniper = all_sniper.get(symbol) if isinstance(all_sniper, dict) else {}
    sniper = sniper or ((feed.get("full_spectrum") or {}).get(symbol) or {}).get("sniper") or {}
    if not sniper:
        sniper = (feed.get("smc") or {}).get(symbol) or {}
    trigger = bool(sniper.get("trigger") or sniper.get("bottom", {}).get("trigger")
                   or sniper.get("top", {}).get("trigger"))
    side = str(sniper.get("side") or sniper.get("action") or "").upper()
    if side not in {"CE", "PE"}:
        side = "PE" if "PE" in side else "CE" if "CE" in side else ""
    entry = sniper.get("entry") or sniper.get("option_ltp") or 0.0
    targets = sniper.get("targets") or {}
    stop = _f(sniper.get("sl_points") or 7.5)
    target = _f(targets.get("target_1") or sniper.get("target_1") or entry)
    roi = ((target - _f(entry)) / stop * 100.0) if _f(entry) > 0 and stop > 0 and target >= _f(entry) else 0.0
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    depth = spectrum.get("depth") or {}
    futures = spectrum.get("futures") or {}
    confidence = sniper.get("confidence") or (sniper.get("signature_count", 0) / 5 * 100)
    return _candidate(
        engine="REVERSAL_SMC_SNIPER",
        sleeve="B",
        side=side,
        trigger=trigger,
        data_ok=bool(sniper.get("data_ok", True)),
        net_roi_pct=roi,
        confidence_pct=confidence,
        obi_5=depth.get("obi_5"), tbq_tsq=depth.get("tbq_tsq"),
        futures_ratio=futures.get("vtt_ratio"),
        entry=entry, target_1=target,
        target_2=targets.get("target_2_p90") or sniper.get("target_2_p90"),
        reason=str(sniper.get("reason") or "WAITING_FOR_REVERSAL"),
        source=sniper,
    )


def _parallel_candidate(
    feed: Mapping[str, Any],
    symbol: str,
    key: str,
    engine: str,
    sleeve: str,
) -> dict[str, Any]:
    parallel = feed.get("parallel_alpha") or {}
    source = (parallel.get("engines") or {}).get(key) or {}
    trigger = bool(source.get("trigger"))
    side = str(source.get("side") or "").upper()
    entry = source.get("entry") or source.get("spot") or 0.0
    target = source.get("target_1") or 0.0
    stop = source.get("sl_points") or source.get("stop_points") or 6.0
    roi = ((_f(target) - _f(entry)) / _f(stop) * 100.0) if _f(entry) > 0 and _f(stop) > 0 and _f(target) > _f(entry) else 0.0
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    depth = spectrum.get("depth") or {}
    futures = spectrum.get("futures") or {}
    confidence = source.get("confidence_pct")
    if confidence is None:
        confidence = source.get("confidence")
    return _candidate(
        engine=engine,
        sleeve=sleeve,
        side=side,
        trigger=trigger,
        data_ok=bool(source.get("data_ok", True)),
        net_roi_pct=roi,
        confidence_pct=confidence or 0.0,
        obi_5=depth.get("obi_5"), tbq_tsq=depth.get("tbq_tsq"),
        futures_ratio=futures.get("vtt_ratio"),
        entry=entry, target_1=target, target_2=source.get("target_2"),
        reason=str(source.get("reason") or "NO_PARALLEL_TRIGGER"),
        source=source,
    )


def _scalp_candidate(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    scalper = (feed.get("scalper") or {}).get(symbol) or {}
    decision = scalper.get("decision") or scalper
    trigger = bool(decision.get("trigger") or decision.get("tradeable"))
    side = str(decision.get("side") or "").upper()
    entry = decision.get("entry") or scalper.get("entry") or 0.0
    target = decision.get("target") or decision.get("target_1") or 0.0
    stop = decision.get("sl_points") or 4.0
    roi = ((_f(target) - _f(entry)) / _f(stop) * 100.0) if _f(entry) > 0 and _f(target) > _f(entry) else 0.0
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    depth = spectrum.get("depth") or {}
    futures = spectrum.get("futures") or {}
    confidence = decision.get("confidence_pct") or decision.get("confidence") or 0.0
    return _candidate(
        engine="ADAPTIVE_VELOCITY_PING_PONG",
        sleeve="A",
        side=side,
        trigger=trigger,
        data_ok=bool(decision.get("data_ok", True)),
        net_roi_pct=roi,
        confidence_pct=confidence,
        obi_5=depth.get("obi_5"), tbq_tsq=depth.get("tbq_tsq"),
        futures_ratio=futures.get("vtt_ratio"),
        entry=entry, target_1=target, target_2=decision.get("target_2"),
        reason=str(decision.get("reason") or "WAITING_FOR_SCALP_REGIME"),
        source=decision,
    )


def _spot_candles_5m(feed: Mapping[str, Any], symbol: str) -> list:
    spot = ((feed.get("spot") or {}).get(symbol) or {}) if isinstance(feed.get("spot"), Mapping) else {}
    rows = spot.get("candles_5m") if isinstance(spot, Mapping) else None
    return list(rows or [])


def entry_readiness_gate(
    feed: Mapping[str, Any],
    *,
    symbol: str = "NIFTY",
    side: str = "",
    quorum: Mapping[str, Any] | None = None,
    data_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The strict 4-point visual and structural gate before any order fires.

    1. The Spot chart has loaded >= 150 five-minute candles and the newest one
       confirms the direction against VWAP (and the previous 5m high/low).
    2. The selected ITM option chart has >= 30 real, non-zero OHLC candles.
    3. A 2-of-3 model quorum at >= 62% plus option VWAP / OBI / TBQ print.
    4. No duplicate entry while a position is OPEN.

    Every point is returned with the observed value so the desk can show the
    exact reason it is holding instead of a generic "blocked" badge.
    """
    feed = feed or {}
    quorum = quorum or {}
    gate = data_gate or {}
    leg = str(side or quorum.get("side") or "").upper()
    leg = leg if leg in {"CE", "PE"} else ""
    spot_block = ((feed.get("spot") or {}).get(symbol) or {}) if isinstance(feed.get("spot"), Mapping) else {}
    spot_block = spot_block if isinstance(spot_block, Mapping) else {}
    bars = _spot_candles_5m(feed, symbol)
    spot = _f(spot_block.get("ltp") or spot_block.get("display"))
    vwap = _f(spot_block.get("vwap") or spot_block.get("atp"))
    upper = 0.0
    lower = 0.0
    if len(bars) >= 2 and isinstance(bars[-2], Mapping):
        upper = _f(bars[-2].get("h") if bars[-2].get("h") is not None else bars[-2].get("high"))
        lower = _f(bars[-2].get("l") if bars[-2].get("l") is not None else bars[-2].get("low"))
    if leg == "CE":
        structure_ok = bool(spot > 0 and vwap > 0 and spot >= vwap and (upper <= 0 or spot >= upper))
    elif leg == "PE":
        structure_ok = bool(spot > 0 and vwap > 0 and spot <= vwap and (lower <= 0 or spot <= lower))
    else:
        structure_ok = False
    spot_point = {
        "key": "spot_5m",
        "label": "Spot 5m candle formation",
        "pass": bool(len(bars) >= SPOT_MIN_5M_CANDLES and structure_ok),
        "value": (f"{len(bars)}/{SPOT_MIN_5M_CANDLES} 5m bars · "
                  f"spot {spot:,.2f} vs VWAP {vwap:,.2f}"
                  if spot and vwap else f"{len(bars)}/{SPOT_MIN_5M_CANDLES} 5m bars"),
    }

    options = feed.get("option") or {}
    option = options.get(symbol) if isinstance(options, Mapping) else {}
    option = option if isinstance(option, Mapping) else {}
    chart = option_candle_readiness(option)
    option_point = {
        "key": "option_chart",
        "label": "ITM option candles formed",
        "pass": bool(chart.get("ready")),
        "value": f"{chart.get('formed', 0)}/{OPTION_MIN_CANDLES} {chart.get('timeframe') or '—'} candles · {chart.get('reason')}",
    }

    votes = quorum.get("votes") or quorum.get("model_cards") or []
    confirmed = [
        vote for vote in votes
        if isinstance(vote, Mapping) and bool(vote.get("confirmed"))
        and str(vote.get("side") or "").upper() == leg
    ] if leg else []
    confidence = _f(quorum.get("confidence"))
    depth = option.get("depth") if isinstance(option.get("depth"), Mapping) else {}
    obi = _f(option.get("obi_5") or (depth or {}).get("obi_5"))
    tbq = _f(option.get("tbq_tsq") or (depth or {}).get("tbq_tsq"))
    option_vwap = _f(option.get("atp_vwap") or option.get("vwap"))
    option_ltp = _f(option.get("ltp"))
    above_atp = bool(option_vwap <= 0 or option_ltp >= option_vwap)
    quorum_ok = bool(
        len(confirmed) >= 2 or (bool(quorum.get("approved")) and confidence >= QUORUM_CONFIDENCE_FLOOR)
    )
    quorum_point = {
        "key": "quorum_flow",
        "label": "2-of-3 quorum + option book",
        "pass": bool(quorum_ok and confidence >= QUORUM_CONFIDENCE_FLOOR
                     and above_atp and obi >= 1.20 and tbq >= 1.25),
        "value": (f"{quorum.get('quorum_count') or f'{len(confirmed)}/3'} @ {confidence:.0f}% · "
                  f"OBI {obi:.2f}x · TBQ/TSQ {tbq:.2f}x"),
    }

    trade = ((feed.get("trade") or {}).get(symbol) or {}) if isinstance(feed.get("trade"), Mapping) else {}
    open_trade = str((trade or {}).get("status") or "").upper() == "OPEN"
    duplicate_point = {
        "key": "no_duplicate",
        "label": "Single active position",
        "pass": not open_trade,
        "value": (f"OPEN {trade.get('option_symbol') or trade.get('strike_symbol') or ''}".strip()
                  if open_trade else "FLAT · ENTRY SLOT FREE"),
    }

    points = [spot_point, option_point, quorum_point, duplicate_point]
    allowed = bool(gate.get("allowed", True)) and all(point["pass"] for point in points)
    blocking = [point["key"] for point in points if not point["pass"]]
    return {
        "allowed": allowed,
        "status": "READY_TO_FIRE" if allowed else "HOLD",
        "symbol": symbol,
        "side": leg,
        "points": points,
        "points_passed": sum(1 for point in points if point["pass"]),
        "points_total": len(points),
        "blocking": blocking,
        "reason": ("ALL_4_POINTS_VERIFIED" if allowed else
                   "DATA_GATE_BLOCKED" if not gate.get("allowed", True) else
                   "HOLDING_ON_" + "_".join(key.upper() for key in blocking)),
        "summary": f"{sum(1 for p in points if p['pass'])}/{len(points)} gate points verified",
    }


def _exit_guard(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    trade = (feed.get("trade") or {}).get(symbol) or {}
    open_trade = str(trade.get("status") or "").upper() == "OPEN"
    if not open_trade:
        return {"active": False, "action": "NONE", "reason": "NO_OPEN_POSITION"}
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    depth = spectrum.get("depth") or {}
    obi = _f(depth.get("obi_5"), 1.0)
    plan = (((feed.get("model_brain") or {}).get(symbol)) or {})
    p90 = _f(plan.get("p90"))
    spot = _f(((feed.get("spot") or {}).get(symbol) or {}).get("ltp") or
              ((feed.get("full_spectrum") or {}).get(symbol) or {}).get("spot"))
    smc = (feed.get("smc") or {}).get(symbol) or {}
    choch = str((smc.get("structure") or {}).get("active_choch") or "").upper()
    if obi < 0.72:
        return {"active": True, "action": "EXIT_NOW", "reason": "OBI_FLIP_BELOW_0_72",
                "obi_5": round(obi, 3), "p90": p90 or None, "spot": spot or None}
    if p90 > 0 and spot >= p90 and "BEAR" in choch:
        return {"active": True, "action": "EXIT_NOW", "reason": "P90_BEARISH_CHOCH",
                "obi_5": round(obi, 3), "p90": p90, "spot": spot}
    return {"active": False, "action": "HOLD", "reason": "NO_EXIT_TRIGGER",
            "obi_5": round(obi, 3), "p90": p90 or None, "spot": spot or None}


@dataclass
class MasterSignalArbitrator:
    """Rank all valid candidates and choose at most one per sleeve."""

    sniper_threshold: float = SNIPER_THRESHOLD
    scalp_threshold: float = SCALP_THRESHOLD

    def evaluate(
        self,
        feed: Mapping[str, Any] | None,
        *,
        symbol: str = "NIFTY",
        now: float | None = None,
        feed_live: bool = True,
        latency_us: float | None = None,
    ) -> dict[str, Any]:
        moment = float(now if now is not None else time.time())
        feed = feed or {}
        gate = _data_gate(feed, now=moment, symbol=symbol, feed_live=feed_live)
        candidates_b = [
            _sniper_candidate(feed, symbol),
            _parallel_candidate(feed, symbol, "tsunami", "HEAVYWEIGHT_5_OF_5_TSUNAMI", "B"),
            _parallel_candidate(feed, symbol, "coiled_spring", "COILED_SPRING_BREAKOUT", "B"),
        ]
        candidates_a = [
            _parallel_candidate(feed, symbol, "parity", "PARITY_DISLOCATION_SNIPE", "A"),
            _parallel_candidate(feed, symbol, "lead_lag", "LEAD_LAG_3S_SNIPE", "A"),
            _scalp_candidate(feed, symbol),
        ]

        def choose(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
            ranked = sorted(rows, key=lambda item: item["edge_score"], reverse=True)
            if not gate["allowed"]:
                for row in ranked:
                    row["blocked_reason"] = "BLOCKED_BY_DATA_GATE"
                    row["selected"] = False
                return {
                    "selected": None, "blocked": True,
                    "reason": "BLOCKED_BY_DATA_GATE",
                    "threshold": threshold,
                    "candidates": ranked,
                }
            eligible = [row for row in ranked if row["valid"] and row["edge_score"] >= threshold]
            winner = eligible[0] if eligible else None
            for row in ranked:
                row["selected"] = bool(winner and row is winner)
                row["blocked_reason"] = "" if row["valid"] else "NO_VALID_TRIGGER"
            return {
                "selected": winner,
                "blocked": False,
                "reason": winner["engine"] if winner else "NO_CANDIDATE_ABOVE_EDGE_THRESHOLD",
                "threshold": threshold,
                "candidates": ranked,
            }

        sleeve_b = choose(candidates_b, self.sniper_threshold)
        sleeve_a = choose(candidates_a, self.scalp_threshold)
        exit_guard = _exit_guard(feed, symbol)
        playbooks = evaluate_playbooks(
            feed, symbol=symbol, now=moment, data_gate=gate,
        )
        quorum = build_model_quorum(feed, symbol=symbol, now=moment)
        wanted_side = (
            (sleeve_b["selected"] or {}).get("side")
            or (sleeve_a["selected"] or {}).get("side")
            or quorum.get("side") or ""
        )
        readiness = entry_readiness_gate(
            feed, symbol=symbol, side=wanted_side, quorum=quorum, data_gate=gate,
        )
        return {
            "ok": gate["allowed"],
            "symbol": symbol,
            "as_of": datetime.fromtimestamp(moment, tz=IST).isoformat(timespec="milliseconds"),
            "data_gate": gate,
            "sleeves": {
                "B": {"capital_rs": SNIPER_SLEEVE_RS, **sleeve_b},
                "A": {"capital_rs": SCALP_SLEEVE_RS, **sleeve_a},
            },
            "sleeve_b": sleeve_b,
            "sleeve_a": sleeve_a,
            "exit_guard": exit_guard,
            "playbooks": playbooks,
            "playbook": playbooks.get("winner"),
            "auto_execute": bool(playbooks.get("auto_execute")),
            "auto_pick_reason": playbooks.get("auto_pick_reason"),
            "quorum": quorum,
            "quorum_auto_execute": bool(quorum.get("auto_execute")),
            "trigger_path": quorum.get("trigger_path"),
            "trigger_label": quorum.get("trigger_label"),
            "single_model_path": quorum.get("trigger_path") == "SINGLE_MODEL_SUPER_CONVICTION",
            "readiness": readiness,
            "entry_ready": bool(readiness["allowed"]),
            "trade_approved": bool(
                gate["allowed"]
                and readiness["allowed"]
                and (playbooks.get("auto_execute") or quorum.get("auto_execute"))
            ),
            "latency_us": round(float(latency_us or 0.0), 2),
            "engine_order": {
                "B": list(SLEEVE_B_ENGINES),
                "A": list(SLEEVE_A_ENGINES),
            },
            "reason": (
                "BLOCKED_BY_DATA_GATE" if not gate["allowed"]
                else "ARBITRATION_ACTIVE"
            ),
        }


def build_master_arbitrator(
    feed: Mapping[str, Any] | None,
    *,
    symbol: str = "NIFTY",
    now: float | None = None,
    feed_live: bool = True,
    latency_us: float | None = None,
) -> dict[str, Any]:
    return MasterSignalArbitrator().evaluate(
        feed, symbol=symbol, now=now, feed_live=feed_live, latency_us=latency_us,
    )


__all__ = [
    "MasterSignalArbitrator",
    "SCALP_SLEEVE_RS",
    "SCALP_THRESHOLD",
    "SLEEVE_A_ENGINES",
    "SLEEVE_B_ENGINES",
    "SNIPER_SLEEVE_RS",
    "SNIPER_THRESHOLD",
    "build_master_arbitrator",
    "entry_readiness_gate",
    "edge_score",
]
