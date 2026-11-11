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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from market.data_sanitizer import FEATURE_NAMES, FeedState, feed_state
from strategy.smc_engine import (
    SmcStateMachine,
    analyze_structure,
    detect_fvgs,
    detect_order_blocks,
    detect_liquidity_sweeps,
)
from strategy.full_spectrum_alpha import chain_metrics

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
AUDIT_STALE_MS = 3_000


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


def _bar_ok(row: Mapping[str, Any]) -> bool:
    values = [_finite(row.get(name)) for name in FEATURE_NAMES]
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
    smc_ok = bool(
        smc
        and (
            smc_structure.get("bos")
            or smc_structure.get("active_bos")
            or float(smc_structure.get("last_swing_high") or 0.0) > 0
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
        smc_detail = (
            f"{smc.get('state', '—')} · {smc.get('structure', {}).get('active_bos') or 'no BOS'}"
            + (f" · zone ₹{wait_zone.get('spot_low')}–₹{wait_zone.get('spot_high')}"
               if wait_zone else " · no exact zone")
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
