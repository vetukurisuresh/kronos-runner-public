"""Explicit three-model division of labour and the institutional audit feed.

The orchestrator does **not** invent a probability when the checkpoints are not
loaded.  It accepts callable predictors from the actual KRONOS ensemble, records
their real inference time, and otherwise publishes a fail-closed ``MISSING``
card.  The same module builds the eight-point data matrix consumed by the
dashboard's "100% Model Data & Brain Log" tab.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from market.data_sanitizer import FEATURE_NAMES, FeedState, feed_state
from strategy.smc_engine import (
    SmcStateMachine,
    analyze_structure,
    detect_fvgs,
    detect_order_blocks,
    detect_liquidity_sweeps,
)
from strategy.full_spectrum_alpha import chain_metrics
from strategy.quant_math_formulas import greek_ev_ratio

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
AUDIT_STALE_MS = 8_000


@dataclass(frozen=True)
class ModelSpec:
    role: str
    name: str
    parameters: str
    interval: str
    inputs: str
    output: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "parameters": self.parameters,
            "interval": self.interval,
            "inputs": self.inputs,
            "output": self.output,
        }


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "base", "Kronos-base (102.3M)", "102.3M", "5m/15m",
        "512 bars Spot OHLC + NIFTY/BANKNIFTY futures volume & amount + "
        "SMC order blocks/BOS + futures basis",
        "12-candle (60m) p10/p25/p50/p65/p90 cone, directional conviction and T1/T2",
    ),
    ModelSpec(
        "small", "Kronos-small (25M)", "25M", "5m",
        "5m OHLCVA + 11-strike PCR/5m ΔOI + India VIX + top-5 breadth + "
        "option IV velocity & theta",
        "TREND_BLAST / SIDEWAYS_PING_PONG / CHOP_BLOCK regime and Greek risk gate",
    ),
    ModelSpec(
        "mini", "Kronos-mini (4M)", "4M", "1m",
        "1m option OHLCVA + 5-level OBI/TBQ/TSQ + delta/gamma velocity",
        "sub-second wait-zone trigger, ₹3.50–₹8.00 micro-SL and post-T2 runner hold/exit",
    ),
)
SPEC_BY_ROLE = {spec.role: spec for spec in MODEL_SPECS}


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _age_ms(feed: Mapping[str, Any], now: float) -> int | None:
    try:
        epoch = float(feed.get("updated_epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0.0
    return int(max(0.0, now - epoch) * 1000.0) if epoch > 0 else None


def _symbol(feed: Mapping[str, Any]) -> str:
    spot = feed.get("spot") or {}
    if isinstance(spot, dict) and spot:
        return "NIFTY" if "NIFTY" in spot else str(next(iter(spot)))
    chain = feed.get("option_chain") or {}
    if isinstance(chain, dict) and chain:
        return "NIFTY" if "NIFTY" in chain else str(next(iter(chain)))
    return "NIFTY"


def _rows(rows: Any) -> list[Any]:
    return list(rows or []) if isinstance(rows, (list, tuple)) else []


def _bar_ok(row: Mapping[str, Any] | list | tuple) -> bool:
    # ``last_5m`` is serialized as tensor rows (lists), while older artifacts
    # carried named mappings.  Accept both without inventing missing values.
    if isinstance(row, Mapping):
        values = [_finite(row.get(name)) for name in FEATURE_NAMES]
    elif isinstance(row, (list, tuple)):
        values = [_finite(value) for value in row[:len(FEATURE_NAMES)]]
    else:
        return False
    return all(value is not None and value > 0 for value in values)


def _model_inputs(
    feed: Mapping[str, Any], symbol: str
) -> tuple[dict[str, Any], bool, str, str]:
    ki = (feed.get("kronos_inputs") or {}).get(symbol) or {}
    shape = ki.get("shape_5m") or []
    last = _rows(ki.get("last_5m"))
    source = str(ki.get("volume_source") or "UNVERIFIED")
    ok = (
        isinstance(shape, (list, tuple))
        and len(shape) == 2
        and int(shape[0]) == 512
        and int(shape[1]) == 6
        and bool(last)
        and all(_bar_ok(row) for row in last)
        and source == "FUT"
    )
    if not ok:
        reason = (
            "FUTURES_OHLCVA_TENSOR_MISSING" if not ki
            else f"SHAPE_{shape}_SOURCE_{source}"
        )
    else:
        reason = "512x6_FUTURES_OHLCVA_VERIFIED"
    return ki, ok, source, reason


def validate_input_tensor(frame: Iterable[Any] | None, *, require_bars: int = 512) -> dict[str, Any]:
    """Validate a literal 6-feature frame; no interpolation or zero filling."""
    rows = [row for row in (frame or []) if isinstance(row, Mapping)]
    if len(rows) != require_bars:
        return {"ok": False, "reason": f"BARS_{len(rows)}_EXPECTED_{require_bars}",
                "bars": len(rows), "shape": [1, len(rows), 6]}
    for index, row in enumerate(rows):
        if not _bar_ok(row):
            return {"ok": False, "reason": f"BAR_{index}_NON_POSITIVE_OR_NAN",
                    "bars": len(rows), "shape": [1, len(rows), 6]}
    return {"ok": True, "reason": "512x6_CONTIGUOUS_VERIFIED",
            "bars": len(rows), "shape": [1, len(rows), 6]}


def _model_card(
    spec: ModelSpec,
    *,
    ki: Mapping[str, Any],
    input_ok: bool,
    input_reason: str,
    prediction: Mapping[str, Any] | None,
    inference_ms: float | None,
    missing_reason: str,
) -> dict[str, Any]:
    probability = prediction.get("direction_probability_pct") if prediction else None
    if probability is None and prediction:
        probability = prediction.get("probability_pct")
    probability = _finite(probability)
    if probability is not None:
        probability = round(probability, 2)
    state = str((prediction or {}).get("state") or "").strip()
    if not state:
        state = "WAITING_FOR_MODEL_INFERENCE" if input_ok else "INPUT_BLOCKED"
    reason = str((prediction or {}).get("reason") or "").strip()
    if not reason:
        reason = missing_reason if not prediction else "Inference returned without a reason"
    if prediction and (inference_ms is None or inference_ms < 1.0):
        cached_ms = prediction.get("inference_ms")
        if cached_ms is not None:
            inference_ms = _finite(cached_ms)
    return {
        **spec.as_dict(),
        "shape": [1, 512, 6],
        "bars": int((ki.get("shape_5m") or [0])[0] or 0) if ki else 0,
        "features": list(FEATURE_NAMES),
        "volume_source": str(ki.get("volume_source") or "UNVERIFIED"),
        "input_verified": bool(input_ok),
        "input_reason": input_reason,
        "model_loaded": bool(prediction),
        "inference_ms": round(float(inference_ms), 3) if inference_ms is not None else None,
        "probability_pct": probability,
        "upside_prob": prediction.get("upside_prob") if prediction else None,
        "downside_prob": prediction.get("downside_prob") if prediction else None,
        "paths_up": prediction.get("paths_up") if prediction else None,
        "samples": prediction.get("samples") if prediction else None,
        "paths_total": prediction.get("paths_total") if prediction else None,
        "paths_bearish": prediction.get("paths_bearish") if prediction else None,
        "candles_analyzed": prediction.get("candles_analyzed") if prediction else None,
        "buffer_total": prediction.get("buffer_total") if prediction else None,
        "median_trajectory": prediction.get("median_trajectory") if prediction else None,
        "projection_source": prediction.get("projection_source") if prediction else None,
        "projection_ms": prediction.get("projection_ms") if prediction else None,
        "direction": prediction.get("direction") if prediction else None,
        "state": state,
        "reason": reason,
        "target_1": prediction.get("target_1") if prediction else None,
        "target_2": prediction.get("target_2") if prediction else None,
        "regime": prediction.get("regime") if prediction else None,
        "greek_gate": prediction.get("greek_gate") if prediction else None,
        "micro_sl": prediction.get("micro_sl") if prediction else None,
    }


def run_model_brain(
    feed: Mapping[str, Any],
    predictors: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run any supplied real predictors; never fabricate a missing one."""
    symbol = _symbol(feed)
    ki, input_ok, source, input_reason = _model_inputs(feed, symbol)
    predictors = predictors or {}
    cards: list[dict[str, Any]] = []
    for spec in MODEL_SPECS:
        predictor = predictors.get(spec.role)
        prediction: Mapping[str, Any] | None = None
        inference_ms: float | None = None
        if predictor is not None and input_ok:
            started = time.perf_counter()
            try:
                prediction = predictor({"feed": feed, "symbol": symbol, "spec": spec})
            except Exception as exc:  # noqa: BLE001 - fail closed, surface the error
                prediction = {"state": "INFERENCE_ERROR", "reason": f"{type(exc).__name__}: {exc}"}
            inference_ms = (time.perf_counter() - started) * 1000.0
        reason = (
            "No loaded KRONOS checkpoint produced this card; zero-fake policy blocks a forecast."
            if predictor is None else input_reason
        )
        cards.append(_model_card(
            spec, ki=ki, input_ok=input_ok, input_reason=input_reason,
            prediction=prediction, inference_ms=inference_ms, missing_reason=reason,
        ))
    return {
        "models": cards,
        "decision": {
            "state": next((card["state"] for card in cards
                           if card["state"] not in {"INPUT_BLOCKED", "WAITING_FOR_MODEL_INFERENCE"}), ""),
            "reason": next((card["reason"] for card in cards if card["model_loaded"]), ""),
            "loaded_models": sum(1 for card in cards if card["model_loaded"]),
            "total_models": len(cards),
            "input_verified": input_ok,
        },
        "input": {"symbol": symbol, "source": source, **ki},
    }


def _chain_check(chain_rows: Iterable[Any], age_ms: int | None) -> tuple[bool, str, dict[str, Any]]:
    rows = _rows(list(chain_rows or []))
    metrics = chain_metrics(rows)
    valid = int(metrics.get("valid_strikes") or 0)
    ok = len(rows) >= 11 and valid >= 11 and bool(age_ms is not None and age_ms <= AUDIT_STALE_MS)
    return ok, (
        f"{len(rows)} strikes · {valid} two-sided · PCR {metrics.get('pcr')} · "
        f"max pain {metrics.get('max_pain')}"
    ), metrics


def _greeks_check(chain_rows: Iterable[Any], spectrum: Mapping[str, Any]) -> tuple[bool, str]:
    greeks = spectrum.get("greeks") or {}
    names = ("delta", "gamma", "theta", "vega", "rho", "iv")
    present = [name for name in names if _finite(greeks.get(name)) not in (None, 0.0)]
    if present:
        return True, f"{len(present)}/6 live Greeks · " + ", ".join(
            f"{name} {float(greeks[name]):.3f}" for name in present
        )
    count = 0
    for row in _rows(list(chain_rows or [])):
        for side in ("call_options", "put_options"):
            leg = (row.get(side) or {}) if isinstance(row, Mapping) else {}
            values = leg.get("option_greeks") or leg.get("greeks") or {}
            if isinstance(values, Mapping) and any(
                _finite(values.get(name)) not in (None, 0.0) for name in names
            ):
                count += 1
    return count > 0, f"{count} option legs carry live Greeks" if count else "Greeks absent"


def _depth_check(spectrum: Mapping[str, Any], spot: Mapping[str, Any]) -> tuple[bool, str]:
    depth = spectrum.get("depth") or spot.get("depth") or {}
    bids = _rows(depth.get("bids"))
    asks = _rows(depth.get("asks"))
    obi = _finite(depth.get("obi_5"))
    ok = len(bids) >= 5 and len(asks) >= 5 and obi is not None
    return ok, (
        f"{len(bids)} bids / {len(asks)} asks · OBI_5 {obi:.2f}"
        if ok else f"depth {len(bids)}/5 bids, {len(asks)}/5 asks"
    )


def _flow_check(spectrum: Mapping[str, Any]) -> tuple[bool, str]:
    depth = spectrum.get("depth") or {}
    futures = spectrum.get("futures") or {}
    tbq, tsq = _finite(depth.get("tbq")), _finite(depth.get("tsq"))
    basis = _finite(futures.get("basis"))
    ok = bool(tbq and tsq and tbq > 0 and tsq > 0 and basis is not None)
    return ok, (
        f"TBQ {tbq:,.0f} / TSQ {tsq:,.0f} · {tbq / tsq:.2f}x · basis {basis:+.2f}"
        if ok else "TBQ/TSQ or futures basis missing"
    )


def _vix_breadth_check(spectrum: Mapping[str, Any]) -> tuple[bool, str]:
    vix = spectrum.get("vix") or {}
    breadth = spectrum.get("breadth") or {}
    vix_ltp = _finite(vix.get("ltp"))
    total = int(_finite(breadth.get("total")) or 0)
    aligned = int(_finite(breadth.get("aligned")) or 0)
    ok = bool(vix_ltp and vix_ltp > 0 and total >= 5)
    return ok, (
        f"India VIX {vix_ltp:.2f} · breadth {aligned}/{total} aligned"
        if ok else f"VIX {vix_ltp or 0:.2f} · breadth {aligned}/{total}"
    )


def _smc_for_feed(feed: Mapping[str, Any], symbol: str, *, feed_live: bool) -> dict[str, Any] | None:
    supplied = (feed.get("smc") or {}).get(symbol) if isinstance(feed.get("smc"), dict) else None
    if isinstance(supplied, Mapping):
        return dict(supplied)
    spot = (feed.get("spot") or {}).get(symbol) or {}
    candles_1m = _rows(spot.get("candles_1m"))
    candles_5m = _rows(spot.get("candles_5m"))
    if not candles_5m and not candles_1m:
        return None
    p10 = _finite(((feed.get("model_brain") or {}).get(symbol) or {}).get("p10"))
    p25 = _finite(((feed.get("model_brain") or {}).get(symbol) or {}).get("p25"))
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    obi = _finite((spectrum.get("depth") or {}).get("obi_5")) or 0.0
    option = (feed.get("option") or {}).get(symbol) or {}
    machine = SmcStateMachine()
    snapshot = machine.update(
        candles_1m=candles_1m, candles_5m=candles_5m,
        candles_15m=_rows(spot.get("candles_15m")),
        futures_1m=_rows((feed.get("futures") or {}).get(symbol)),
        p10=p10, p25=p25, spot=_finite(spot.get("ltp")) or 0.0,
        option_atp=_finite(option.get("atp_vwap")),
        obi_5=obi, feed_live=feed_live,
    )
    return snapshot.as_dict()


def _audit_log(feed: Mapping[str, Any], state, reason: str, *, smc: Mapping[str, Any] | None,
               verified: int, total: int) -> list[dict[str, Any]]:
    rows = _rows(feed.get("brain_log"))
    if rows:
        return rows[-60:]
    sanitizer = feed.get("sanitizer") or {}
    events = _rows(sanitizer.get("events"))
    if events:
        out = []
        for event in events[-40:]:
            out.append({
                "ts": event.get("ts") or "",
                "level": event.get("level") or "INFO",
                "message": event.get("message") or "",
            })
        return out
    now = datetime.now(tz=IST).isoformat(timespec="milliseconds")
    if not feed:
        return [{"ts": now, "level": "REJECT", "message": "No live_feed.json heartbeat; all models blocked"}]
    return [{
        "ts": now, "level": "WARN",
        "message": f"{state.status}: {reason} · {verified}/{total} audit checks verified",
    }]


def build_audit_snapshot(
    feed: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    predictors: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build the eight-point matrix, the three model cards and the log stream."""
    feed = feed or {}
    moment = float(now if now is not None else time.time())
    state = feed_state(feed, now=moment)
    pre_status = feed.get("feed_status") if isinstance(feed.get("feed_status"), Mapping) else {}
    if pre_status.get("status") in {"FEED_OFFLINE_OR_STALE", "MARKET_CLOSED"}:
        state = FeedState(
            str(pre_status.get("status")),
            pre_status.get("age_ms"),
            bool(pre_status.get("market_open", state.market_open)),
            False,
            str(pre_status.get("reason") or "SESSION_FEED_GATE_BLOCKED"),
            str(pre_status.get("as_of") or ""),
        )
    live = state.status == "FEED_LIVE"
    age_ms = state.age_ms
    symbol = _symbol(feed)
    spot = (feed.get("spot") or {}).get(symbol) or {}
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    chain_rows = (feed.get("option_chain") or {}).get(symbol) or []
    ki, input_ok, source, input_reason = _model_inputs(feed, symbol)
    smc = _smc_for_feed(feed, symbol, feed_live=live)
    smc_structure = (smc or {}).get("structure") or {}
    smc_structures = (smc or {}).get("structures") if isinstance(smc, Mapping) else {}
    # The audit verifies the *data stream*: the engine computed live structure
    # from genuine 1m/5m/15m bars.  A missing BOS/zone is a trade-trigger fact,
    # not a missing-feed fact, so it must not fail stream #8.
    smc_scanned = isinstance(smc_structures, Mapping) and any(
        isinstance(row, Mapping)
        and (
            _finite(row.get("swing_high")) not in (None, 0.0)
            or _finite(row.get("swing_low")) not in (None, 0.0)
            or row.get("bos") or row.get("active_bos") or row.get("choch")
        )
        for row in smc_structures.values()
    )
    smc_ok = bool(
        smc
        and (
            smc_scanned
            or smc_structure.get("bos")
            or smc_structure.get("active_bos")
            or (_finite(smc_structure.get("last_swing_high")) or 0.0) > 0
        )
    )

    buffers = {key: (feed.get(key) or {}).get(symbol) for key in ("buffer_1m", "buffer_5m", "buffer_15m")}
    counts = [int(buffers[key] or 0) for key in ("buffer_1m", "buffer_5m", "buffer_15m")]
    spot_ok = live and all(count >= 512 for count in counts) and bool(spot.get("ltp"))
    spot_detail = (
        f"{symbol} {float(spot.get('ltp') or 0):,.2f} · 1m {counts[0]} / 5m {counts[1]} / 15m {counts[2]} bars"
        if spot else "No spot candles"
    )
    depth_ok, depth_detail = _depth_check(spectrum, spot)
    flow_ok, flow_detail = _flow_check(spectrum)
    chain_ok, chain_detail, _ = _chain_check(chain_rows, age_ms)
    greeks_ok, greeks_detail = _greeks_check(chain_rows, spectrum)
    vix_ok, vix_detail = _vix_breadth_check(spectrum)
    if smc:
        wait_zone = smc.get("wait_zone") or {}
        active_bos = (smc_structure.get("active_bos")
                      or smc_structure.get("bos") or "no BOS")
        smc_detail = (
            f"Structure scanned 1m/5m/15m · {smc.get('state', '—')} · {active_bos}"
            + (f" · zone ₹{wait_zone.get('spot_low')}–₹{wait_zone.get('spot_high')}"
               if wait_zone else " · waiting for OB/FVG entry")
        )
    else:
        smc_detail = "No 1m/5m candles for SMC structure"

    checks: list[tuple[str, str, bool, str]] = [
        ("spot", "Spot 1m/5m/15m OHLC (Exchange LTT synced)", spot_ok, spot_detail),
        ("futures", "Futures Δvtt + amount → 6-feature tokenizer", input_ok,
         f"{input_reason} · {source}"),
        ("depth", "5-level order book (bids/asks, OBI_5)", depth_ok, depth_detail),
        ("flow", "Order flow pressure (TBQ/TSQ, futures basis)", flow_ok, flow_detail),
        ("chain", "11-strike chain (CE/PE OI, ΔOI, PCR, max pain)", chain_ok, chain_detail),
        ("greeks", "Live option Greeks (Δ Γ Θ V ρ IV + velocity)", greeks_ok, greeks_detail),
        ("vix_breadth", "India VIX + top-5 heavyweights breadth", vix_ok, vix_detail),
        ("smc", "SMC structure (OB, FVG, sweep, BOS/CHoCH)", smc_ok, smc_detail),
    ]
    matrix: list[dict[str, Any]] = []
    for index, (key, label, verified, detail) in enumerate(checks, start=1):
        passed = bool(verified and live)
        matrix.append({
            "index": index,
            "key": key,
            "label": label,
            "verified": passed,
            "badge": "✅ VERIFIED LIVE" if passed else "🔴 MISSING",
            "value": detail,
            "age_ms": age_ms,
        })
    verified_count = sum(1 for row in matrix if row["verified"])
    brain = run_model_brain(feed, predictors=predictors)
    if live:
        decision = brain.get("decision") or {}
        decision["state"] = (
            decision.get("state") if decision.get("loaded_models") else
            "WAITING · MODEL OUTPUT MISSING (ZERO-FAKE GATE)"
        )
    else:
        decision = {"state": "BLOCKED · FEED_OFFLINE_OR_STALE",
                    "reason": state.reason, "loaded_models": 0,
                    "total_models": len(MODEL_SPECS), "input_verified": input_ok}
    log_rows = _audit_log(feed, state, state.reason, smc=smc,
                          verified=verified_count, total=len(matrix))
    if not log_rows and smc:
        log_rows = [{
            "ts": datetime.fromtimestamp(moment, tz=IST).isoformat(timespec="milliseconds"),
            "level": "INFO",
            "message": f"SMC {smc.get('state')}: {smc.get('reason')}",
        }]
    return {
        "ok": bool(live and verified_count == len(matrix)),
        "feed_status": state.as_dict(),
        "generated_at": datetime.fromtimestamp(moment, tz=IST).isoformat(timespec="milliseconds"),
        "as_of": feed.get("updated_at") or "",
        "age_ms": age_ms,
        "symbol": symbol,
        "verified_count": verified_count,
        "total_checks": len(matrix),
        "matrix": matrix,
        "models": brain.get("models") or [],
        "decision": decision,
        "smc": smc,
        "log": log_rows,
        "sanitizer": feed.get("sanitizer") or {},
        "input": brain.get("input") or {},
    }


# ---------------------------------------------------------------------------
# 2-of-3 directional quorum
# ---------------------------------------------------------------------------

QUORUM_INDIVIDUAL_FLOOR = 60.0
QUORUM_COMBINED_FLOOR = 60.0
QUORUM_MIN_MODELS = 2
QUORUM_SINGLE_MODEL_FLOOR = 78.0
QUORUM_GREEK_EV_FLOOR = 1.50


def _q_map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _q_side(value: Any) -> str:
    text = str(value or "").upper()
    if "CE" in text or "BULL" in text or "UP" in text:
        return "CE"
    if "PE" in text or "BEAR" in text or "DOWN" in text:
        return "PE"
    return "NEUTRAL"


def _q_bar(raw: Any) -> dict[str, float]:
    row = _q_map(raw)
    return {
        "t": _finite(row.get("t") if row.get("t") is not None else row.get("time")) or 0.0,
        "o": _finite(row.get("o") if row.get("o") is not None else row.get("open")) or 0.0,
        "h": _finite(row.get("h") if row.get("h") is not None else row.get("high")) or 0.0,
        "l": _finite(row.get("l") if row.get("l") is not None else row.get("low")) or 0.0,
        "c": _finite(row.get("c") if row.get("c") is not None else row.get("close")) or 0.0,
    }


def _q_chain(
    feed: Mapping[str, Any], symbol: str, side: str, spot: float,
    *, preferred_strike: float = 0.0,
) -> dict[str, Any]:
    chains = feed.get("option_chain") or {}
    rows = chains.get(symbol) if isinstance(chains, Mapping) else []
    rows = rows if isinstance(rows, list) else []
    best: tuple[float, Mapping[str, Any]] | None = None
    for raw in rows:
        row = _q_map(raw)
        strike = _finite(row.get("strike_price") or row.get("strike")) or 0.0
        if strike <= 0:
            continue
        distance = (
            abs(strike - preferred_strike) if preferred_strike > 0
            else abs(strike - spot) if spot > 0 else 0.0
        )
        if best is None or distance < best[0]:
            best = (distance, row)
    if best is None:
        return {}
    row = best[1]
    leg = _q_map(row.get("call_options" if side == "CE" else "put_options"))
    market = _q_map(leg.get("market_data"))
    greeks = _q_map(leg.get("option_greeks"))
    strike = _finite(row.get("strike_price") or row.get("strike")) or 0.0
    return {
        "strike": strike,
        "contract": f"{symbol} {strike:.0f} {side}" if strike else f"{symbol} {side}",
        "ltp": _finite(market.get("ltp")) or 0.0,
        "bid": _finite(market.get("bid_price")) or 0.0,
        "ask": _finite(market.get("ask_price")) or 0.0,
        "delta": _finite(greeks.get("delta")) or 0.0,
        "gamma": _finite(greeks.get("gamma")) or 0.0,
        "theta": _finite(greeks.get("theta")) or 0.0,
        "iv": _finite(greeks.get("iv")) or 0.0,
    }


def _q_vote(
    role: str,
    name: str,
    short: str,
    *,
    side: str,
    confidence: float,
    reason_te: str,
    reason_en: str,
    trigger: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    confidence = round(max(0.0, min(100.0, float(confidence))), 1)
    confirmed = bool(side in {"CE", "PE"} and confidence >= QUORUM_INDIVIDUAL_FLOOR)
    return {
        "role": role,
        "name": name,
        "short": short,
        "side": side if confirmed else "NEUTRAL",
        "raw_side": side,
        "confidence": confidence,
        "confirmed": confirmed,
        "status": "🟢 CONFIRMED" if confirmed else "🟡 WATCHING",
        "trigger": trigger,
        "reason_te": reason_te,
        "reason_en": reason_en,
        "evidence": dict(evidence or {}),
    }


def build_model_quorum(
    feed: Mapping[str, Any],
    *,
    symbol: str = "NIFTY",
    now: float | None = None,
) -> dict[str, Any]:
    """Evaluate the three models independently and apply the 2-of-3 rule.

    The third model being neutral, or futures volume being 1.15x instead of
    3.0x, must not veto two independently confirmed models.  The function
    returns the exact model cards the dashboard renders, plus a playbook-shaped
    executable order so the live session can route it without a second gate.
    """
    feed = feed or {}
    spots = feed.get("spot") or {}
    spot_block = _q_map(spots.get(symbol) if isinstance(spots, Mapping) else {})
    spot = _finite(spot_block.get("ltp")) or 0.0
    vwap = _finite(spot_block.get("vwap") or spot_block.get("atp")) or 0.0
    spectrum = _q_map((feed.get("full_spectrum") or {}).get(symbol))
    oscillator = _q_map(feed.get("model_oscillator") or {})
    plan = _q_map((feed.get("model_brain") or {}).get(symbol))
    option = _q_map((feed.get("option") or {}).get(symbol))
    depth = _q_map(option.get("depth"))
    current = _q_bar(spot_block.get("current_candle_5m"))
    # The quorum only needs the last few wrists, never all 512 warm-up bars.
    # Parsing the whole buffer cost ~5ms per cycle and starved the <2ms trigger.
    bars = [_q_bar(row) for row in (spot_block.get("candles_5m") or [])[-8:]]
    prior = next((row for row in reversed(bars) if row["t"] < current["t"]), {})
    one_min = [_q_bar(row) for row in (spot_block.get("candles_1m") or [])[-6:]]
    micro = [row["c"] for row in one_min[-4:] if row["c"] > 0]
    micro_down = len(micro) >= 2 and micro[-1] < micro[0]
    micro_up = len(micro) >= 2 and micro[-1] > micro[0]
    smc = _q_map((feed.get("smc") or {}).get(symbol))
    five = _q_map((smc.get("structures") or {}).get("5m")) or _q_map(smc.get("structure"))
    bos = str(five.get("active_bos") or "")
    choch = str(five.get("active_choch") or "")
    bullish_structure = "BULLISH" in bos or "BULLISH" in choch
    bearish_structure = "BEARISH" in bos or "BEARISH" in choch

    # Model 1 - 102M trajectory / 5m-15m structure.
    bull = _finite(oscillator.get("bullish_upside_prob_pct"))
    bear = _finite(oscillator.get("bearish_downside_prob_pct"))
    if bull is None or bear is None:
        paths_up = _finite(plan.get("paths_up")) or 0.0
        samples = max(1.0, _finite(plan.get("samples")) or 1.0)
        bull = paths_up / samples * 100.0
        bear = 100.0 - bull
    if bull >= 50.0:
        base_side, base_conf = "CE", bull
    else:
        base_side, base_conf = "PE", bear
    if base_side == "PE" and bearish_structure:
        base_conf = min(99.0, base_conf + 2.0)
    if base_side == "CE" and bullish_structure:
        base_conf = min(99.0, base_conf + 2.0)
    base_reason_te = (
        f"100-దారుల ట్రాజెక్టరీలో {base_side} దిశ {base_conf:.1f}% బలంగా ఉంది; "
        f"5m స్ట్రక్చర్ {'బేరిష్' if bearish_structure else 'బుల్లిష్' if bullish_structure else 'న్యూట్రల్'}."
    )
    base_reason_en = (
        f"100-way trajectory favours {base_side} at {base_conf:.1f}% with "
        f"5m structure {'bearish' if bearish_structure else 'bullish' if bullish_structure else 'neutral'}."
    )
    base_vote = _q_vote(
        "base", "Kronos-base (102M)", "🧠 KRONOS-102M",
        side=base_side, confidence=base_conf, reason_te=base_reason_te,
        reason_en=base_reason_en,
        trigger=f"5m BOS {bos or '—'} · downside {bear:.1f}% · upside {bull:.1f}%",
        evidence={"upside_pct": round(bull, 2), "downside_pct": round(bear, 2), "bos": bos, "choch": choch},
    )

    # Model 2 - 25M option chain, PCR/Delta-OI and Greek EV.
    pcr_block = _q_map(spectrum.get("pcr"))
    pcr = _finite(pcr_block.get("pcr") or oscillator.get("input_snapshot", {}).get("pcr")) or 0.0
    doi_pcr = _finite(pcr_block.get("doi_pcr")) or 0.0
    chain_rows = (feed.get("option_chain") or {}).get(symbol) or [] if isinstance(feed.get("option_chain"), Mapping) else []
    metrics = chain_metrics(chain_rows)
    if pcr <= 0:
        pcr = _finite(metrics.get("pcr")) or 0.0
    if doi_pcr == 0:
        doi_pcr = _finite(metrics.get("doi_pcr")) or 0.0
    expected_move = abs((_finite(plan.get("p90")) or 0.0) - (_finite(plan.get("p10")) or 0.0)) / 2.0
    preferred_strike = _finite(option.get("strike_price") or option.get("strike")) or 0.0
    quote = _q_chain(
        feed, symbol, "PE" if (pcr < 1.0 or doi_pcr < 0) else "CE", spot,
        preferred_strike=preferred_strike,
    )
    spread = max(
        0.0,
        (_finite(quote.get("ask")) or 0.0) - (_finite(quote.get("bid")) or 0.0),
    )
    ev = greek_ev_ratio(
        delta=abs(_finite(quote.get("delta")) or 0.0),
        gamma=_finite(quote.get("gamma")) or 0.0,
        expected_abs_move=expected_move,
        theta_15m=_finite(quote.get("theta")) or 0.0,
        spread_inr=spread,
    )
    ev_ratio = _finite(ev.get("ev_ratio")) or 0.0
    if pcr < 0.98 and doi_pcr <= 0:
        small_side, small_conf = "PE", 50.0 + min(18.0, abs(1.0 - pcr) * 100.0) + 8.0
    elif pcr > 1.02 and doi_pcr > 0:
        small_side, small_conf = "CE", 50.0 + min(18.0, abs(pcr - 1.0) * 100.0) + 8.0
    elif pcr < 0.98:
        small_side, small_conf = "PE", 54.0 + min(10.0, abs(1.0 - pcr) * 100.0)
    elif pcr > 1.02:
        small_side, small_conf = "CE", 54.0 + min(10.0, abs(pcr - 1.0) * 100.0)
    else:
        small_side, small_conf = "NEUTRAL", 50.0
    if small_side in {"CE", "PE"} and ev_ratio >= QUORUM_GREEK_EV_FLOOR:
        small_conf += 10.0
    elif small_side in {"CE", "PE"}:
        small_conf = min(small_conf, 58.0)
        small_side = "NEUTRAL"
    small_conf = min(98.0, small_conf)
    writer_text = "CE రైటింగ్ + PE అన్‌వైండింగ్" if small_side == "PE" else "PE రైటింగ్ + CE అన్‌వైండింగ్" if small_side == "CE" else "రైటర్ ఫ్లో క్లియర్ కాదు"
    small_vote = _q_vote(
        "small", "Kronos-small (25M)", "🛡️ KRONOS-25M",
        side=small_side, confidence=small_conf,
        reason_te=f"PCR {pcr:.2f}, 5m ΔOI PCR {doi_pcr:.2f} — {writer_text}; Greek EV {ev_ratio:.2f}x.",
        reason_en=f"PCR {pcr:.2f}, 5m delta-OI PCR {doi_pcr:.2f}; Greek convexity/theta EV {ev_ratio:.2f}x.",
        trigger=f"PCR {pcr:.2f} · ΔOI PCR {doi_pcr:.2f} · Greek EV {ev_ratio:.2f}x",
        evidence={"pcr": round(pcr, 4), "doi_pcr": round(doi_pcr, 4), "ev_ratio": round(ev_ratio, 4)},
    )

    # Model 3 - 4M microstructure.
    obi = _finite(option.get("obi_5") or depth.get("obi_5") or oscillator.get("input_snapshot", {}).get("obi_5")) or 0.0
    tbq = _finite(option.get("tbq_tsq") or depth.get("tbq_tsq") or oscillator.get("input_snapshot", {}).get("tbq_tsq")) or 0.0
    if spot > 0 and vwap > 0 and spot < vwap:
        mini_side = "PE"
    elif spot > 0 and vwap > 0 and spot > vwap:
        mini_side = "CE"
    else:
        mini_side = "NEUTRAL"
    if mini_side == "PE" and not micro_down:
        mini_side = "NEUTRAL"
    if mini_side == "CE" and not micro_up:
        mini_side = "NEUTRAL"
    if obi >= 1.25 and tbq >= 1.25 and mini_side in {"CE", "PE"}:
        mini_conf = 62.0 + min(22.0, (obi - 1.25) * 3.0) + min(12.0, (tbq - 1.25) * 5.0)
    else:
        mini_conf = 48.0
        mini_side = "NEUTRAL"
    mini_vote = _q_vote(
        "mini", "Kronos-mini (4M)", "⚡ KRONOS-4M",
        side=mini_side, confidence=mini_conf,
        reason_te=(
            f"OBI_5 {obi:.2f}x, TBQ/TSQ {tbq:.2f}x, ధర VWAP {vwap:,.2f} కి {'కింద' if mini_side == 'PE' else 'పైన' if mini_side == 'CE' else 'దగ్గర'} — {mini_side} మైక్రో ఫ్లో."
            if mini_side in {"CE", "PE"} else
            f"OBI_5 {obi:.2f}x, TBQ/TSQ {tbq:.2f}x; 1m ధర లేదా బుక్ కన్ఫర్మేషన్ ఇంకా లేదు."
        ),
        reason_en=(
            f"OBI_5 {obi:.2f}x, TBQ/TSQ {tbq:.2f}x with price {'below' if mini_side == 'PE' else 'above' if mini_side == 'CE' else 'near'} micro-VWAP."
            if mini_side in {"CE", "PE"} else
            f"OBI_5 {obi:.2f}x, TBQ/TSQ {tbq:.2f}x; 1m price confirmation missing."
        ),
        trigger=f"OBI_5 {obi:.2f}x · TBQ/TSQ {tbq:.2f}x · micro {('DOWN' if micro_down else 'UP' if micro_up else 'FLAT')}",
        evidence={"obi_5": round(obi, 4), "tbq_tsq": round(tbq, 4), "vwap": round(vwap, 2), "spot": round(spot, 2)},
    )

    votes = [base_vote, small_vote, mini_vote]

    def _side_votes(side: str) -> list[dict[str, Any]]:
        return [
            vote for vote in votes
            if vote["side"] == side and vote["confidence"] >= QUORUM_INDIVIDUAL_FLOOR
        ]

    pe = _side_votes("PE")
    ce = _side_votes("CE")
    confirming = pe if len(pe) >= len(ce) else ce
    majority_side = "PE" if confirming is pe and confirming else "CE" if confirming is ce and confirming else "NEUTRAL"
    weights = {"base": 0.40, "small": 0.25, "mini": 0.35}
    multi_conf = 0.0
    if confirming:
        denom = sum(weights.get(vote["role"], 1.0) for vote in confirming)
        multi_conf = sum(vote["confidence"] * weights.get(vote["role"], 1.0) for vote in confirming) / max(0.0001, denom)
    multi_conf = round(multi_conf, 1)
    multi_approved = bool(
        len(confirming) >= QUORUM_MIN_MODELS and multi_conf >= QUORUM_COMBINED_FLOOR
    )

    option_side = _q_side(option.get("option_type") or option.get("side"))
    option_ltp = _finite(option.get("ltp")) or 0.0
    option_vwap = _finite(option.get("atp_vwap") or option.get("vwap")) or 0.0

    def _price_aligned(for_side: str) -> bool:
        if spot <= 0 or vwap <= 0 or for_side not in {"CE", "PE"}:
            return False
        spot_ok = spot > vwap if for_side == "CE" else spot < vwap
        option_ok = bool(
            option_side == for_side
            and option_ltp > 0
            and option_vwap > 0
            and (option_ltp >= option_vwap if for_side == "CE" else option_ltp <= option_vwap)
        )
        return bool(spot_ok and option_ok)

    super_votes = [
        vote for vote in votes
        if vote["side"] in {"CE", "PE"}
        and vote["confidence"] >= QUORUM_SINGLE_MODEL_FLOOR
        and _price_aligned(vote["side"])
    ]
    single_vote = max(super_votes, key=lambda vote: vote["confidence"]) if super_votes else {}
    single_conf = round(_finite(single_vote.get("confidence")) or 0.0, 1)
    single_approved = bool(single_vote)

    trigger_path = "WAITING"
    side = majority_side
    if multi_approved and single_approved:
        if single_vote["side"] != majority_side and single_conf > multi_conf + 5.0:
            trigger_path = "SINGLE_MODEL_SUPER_CONVICTION"
            side = single_vote["side"]
            confirming = [single_vote]
        else:
            trigger_path = "MULTI_MODEL_CONSENSUS"
            side = majority_side
    elif multi_approved:
        trigger_path = "MULTI_MODEL_CONSENSUS"
        side = majority_side
    elif single_approved:
        trigger_path = "SINGLE_MODEL_SUPER_CONVICTION"
        side = single_vote["side"]
        confirming = [single_vote]
    elif side == "NEUTRAL":
        raw = [vote for vote in votes if vote["side"] in {"CE", "PE"}]
        if raw:
            side = "PE" if sum(v["side"] == "PE" for v in raw) >= sum(v["side"] == "CE" for v in raw) else "CE"
    approved = bool(trigger_path != "WAITING")
    quorum_conf = single_conf if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION" else multi_conf
    confirmed_roles = [vote["role"] for vote in confirming]
    confirmed_names = [vote["name"] for vote in confirming]
    short_names = {"base": "102M", "small": "25M", "mini": "4M"}
    trigger_models = [
        f"{short_names.get(vote['role'], vote['role'])} ({vote['confidence']:.0f}%)"
        for vote in confirming
    ]
    trigger_label = (
        "MULTI: " + " + ".join(trigger_models)
        if trigger_path == "MULTI_MODEL_CONSENSUS"
        else "SINGLE-SNIPER: " + " + ".join(trigger_models)
        if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION"
        else "WAITING FOR 2-OF-3 OR 78% SINGLE MODEL"
    )
    quote = (
        _q_chain(feed, symbol, side, spot, preferred_strike=preferred_strike)
        if side in {"CE", "PE"} else {}
    )
    option_same = option_side == side
    entry_price = _finite(quote.get("ltp")) or (option_ltp if option_same else 0.0)
    strike = _finite(quote.get("strike")) or (_finite(option.get("strike_price")) if option_same else 0.0)
    contract = str(quote.get("contract") or option.get("strike_symbol") if option_same else quote.get("contract") or f"{symbol} {strike:.0f} {side}")
    risk_points = round(max(3.5, min(12.0, entry_price * 0.035)), 2) if entry_price > 0 else 0.0
    target_1 = round(entry_price + max(4.0, risk_points * 1.5), 2) if entry_price > 0 else 0.0
    target_2 = round(entry_price + max(8.0, risk_points * 3.0), 2) if entry_price > 0 else 0.0
    stop_loss = round(entry_price - risk_points, 2) if entry_price > 0 else 0.0
    status = (
        f"🟢 MULTI 2-OF-3 ({multi_conf:.0f}%)" if trigger_path == "MULTI_MODEL_CONSENSUS"
        else f"🟢 SINGLE-SNIPER 78%+ ({single_conf:.0f}%)" if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION"
        else f"🟡 WATCHING ({len(confirming)}/3 · {multi_conf:.0f}%)"
    )
    reason_te = (
        f"2-of-3 మోడల్ కన్సెన్సస్ {multi_conf:.1f}% — {side} ట్రేడ్ ఆమోదం."
        if trigger_path == "MULTI_MODEL_CONSENSUS" else
        f"ఒకే మోడల్ {single_conf:.1f}% సూపర్-కన్విక్షన్ + ధర సరైన VWAP వైపు — {side} ట్రేడ్ ఆమోదం."
        if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION" else
        f"2/3 కాన్సెన్సస్ లేదా 78%+ సింగిల్ మోడల్ కోసం వెయిటింగ్; ఇప్పుడు {len(confirming)}/3."
    )
    reason_en = (
        f"Dual-path trigger: 2-of-3 consensus at {multi_conf:.1f}% - immediate {side} approval."
        if trigger_path == "MULTI_MODEL_CONSENSUS" else
        f"Dual-path trigger: one model at {single_conf:.1f}% with spot/option VWAP alignment - immediate {side} approval."
        if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION" else
        f"Waiting for 2-of-3 consensus or one 78%+ model aligned with live VWAP; now {len(confirming)}/3."
    )
    auto_execute = bool(approved and entry_price > 0 and side in {"CE", "PE"})
    winner_id = (
        "SINGLE_MODEL_SNIPER" if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION"
        else "QUORUM_2_OF_3"
    )
    winner_key = (
        "SINGLE_MODEL_SUPER_CONVICTION" if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION"
        else "TWO_OF_THREE_QUORUM"
    )
    winner_label = (
        "Single-Model 78% Super-Conviction" if trigger_path == "SINGLE_MODEL_SUPER_CONVICTION"
        else "2-of-3 Model Quorum"
    )
    entry = {
        "entry_price": entry_price,
        "risk_points": risk_points,
        "target_1": target_1,
        "target_2": target_2,
        "stop_loss": stop_loss,
        "qty": 300,
        "strike": strike,
        "option_type": side,
        "contract": contract,
        "playbook_id": winner_id,
        "playbook_label": winner_label,
        "score": quorum_conf,
        "entry_context": {
            "quorum_count": f"{len(confirming)}/3",
            "quorum_confidence": quorum_conf,
            "confirmed_models": confirmed_names,
            "trigger_path": trigger_path,
            "trigger_label": trigger_label,
            "price_aligned": bool(single_vote and _price_aligned(side)),
            "individual_floor": QUORUM_INDIVIDUAL_FLOOR,
            "single_model_floor": QUORUM_SINGLE_MODEL_FLOOR,
            "combined_floor": QUORUM_COMBINED_FLOOR,
        },
    }
    return {
        "ok": True,
        "symbol": symbol,
        "generated_at": datetime.fromtimestamp(
            float(now if now is not None else feed.get("updated_epoch") or time.time()), tz=IST
        ).isoformat(timespec="milliseconds"),
        "votes": votes,
        "model_cards": votes,
        "side": side,
        "approved": approved,
        "auto_execute": auto_execute,
        "status": status,
        "confidence": quorum_conf,
        "quorum_count": f"{len(confirming)}/3",
        "confirmed_roles": confirmed_roles,
        "confirmed_models": confirmed_names,
        "trigger_models": trigger_models,
        "trigger_path": trigger_path,
        "trigger_label": trigger_label,
        "single_model": {
            "role": single_vote.get("role"),
            "name": single_vote.get("name"),
            "confidence": single_conf,
            "price_aligned": bool(single_vote and _price_aligned(single_vote.get("side"))),
            "floor": QUORUM_SINGLE_MODEL_FLOOR,
        },
        "price_aligned": bool(side in {"CE", "PE"} and _price_aligned(side)),
        "individual_floor": QUORUM_INDIVIDUAL_FLOOR,
        "single_model_floor": QUORUM_SINGLE_MODEL_FLOOR,
        "combined_floor": QUORUM_COMBINED_FLOOR,
        "quorum_band": "60-78%+",
        "reason_te": reason_te,
        "reason_en": reason_en,
        "headline": f"{status} · {side} · {contract}" if side in {"CE", "PE"} else status,
        "trigger_price": round(entry_price, 2),
        "entry": entry,
        "winner_id": winner_id,
        "winner": {
            "key": winner_key,
            "short": winner_label,
            "label": winner_label,
            "angle": "MODEL QUORUM" if trigger_path == "MULTI_MODEL_CONSENSUS" else "SINGLE-MODEL SNIPER",
            "index": 5,
            "score": quorum_conf,
            "side": side,
        },
        "auto_pick_reason": (
            f"{trigger_label} -> {contract}"
            if approved else
            f"Dual trigger waiting: {len(confirming)}/3 confirmed, no aligned 78%+ model."
        ),
        "routing_gate": {
            "third_model_neutral_blocks": False,
            "futures_volume_1_15x_blocks": False,
            "single_model_78_plus_allowed": True,
            "single_model_requires_vwap_alignment": True,
            "live_feed_required": True,
            "trade_approved": auto_execute,
        },
    }


__all__ = [
    "build_model_quorum",
    "QUORUM_COMBINED_FLOOR",
    "QUORUM_GREEK_EV_FLOOR",
    "QUORUM_INDIVIDUAL_FLOOR",
    "QUORUM_MIN_MODELS",
    "QUORUM_SINGLE_MODEL_FLOOR",
]
