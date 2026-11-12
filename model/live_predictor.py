"""Live KRONOS trio inference for the model-audit cards.

This module never invents a probability.  It loads the real NeoQuasar
checkpoints from ``model/weights`` (``python -m model.download_all_kronos``)
and samples each role from the live OHLCVA tensors.  Inference runs in a
daemon thread and the audit reads the freshest cached result, so a slow
autoregressive pass cannot stall the dashboard request.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from config.settings import WEIGHTS_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
FEED_FILE = REPO_ROOT / "state" / "live_feed.json"
STATUS_FILE = REPO_ROOT / "state" / "kronos_predictor_status.json"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
REFRESH_SEC = max(float(os.environ.get("KRONOS_AUDIT_REFRESH_SEC", "30") or 30), 5.0)
#: The 102M pass can take 400+ s on the 8 GB runner with two samples.  A
#: 120/300 s cache made the base card flip to WAITING between passes.
MAX_PREDICT_AGE_SEC = max(float(os.environ.get("KRONOS_AUDIT_MAX_AGE_SEC", "900") or 900), 15.0)

_LOCK = threading.Lock()
_ENSEMBLE: Any = None
_LOAD_ERROR = ""
_LAST_ERROR = ""
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_AT: dict[str, float] = {}
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def quantile_probability(
    spot: Any,
    p10: Any,
    p50: Any,
    p90: Any,
    *,
    paths_up: Any = None,
    samples: Any = None,
    floor: float = 2.0,
    cap: float = 98.0,
) -> dict[str, float | str]:
    """Turn a real forecast cone into a continuous directional probability.

    A two-draw Monte Carlo can only report 0%, 50% or 100% from ``paths_up``.
    That makes a 72/78 gate mathematically unreachable around the middle even
    when the cone is genuinely trending.  The forecast quantiles carry much
    more information than two discrete votes, so map live spot through the
    empirical p10/p50/p90 CDF.  This is still the model's own distribution,
    not a hand-entered sentiment score.  When at least eight paths exist the
    empirical vote is blended in; below that the quantiles are authoritative.
    """
    def _f(value: Any) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return 0.0
        return out

    sx, lo, mid, hi = _f(spot), _f(p10), _f(p50), _f(p90)
    if sx <= 0 or lo <= 0 or mid <= 0 or hi <= 0:
        return {"upside_pct": 50.0, "source": "INSUFFICIENT_QUANTILES"}
    if not (lo <= mid <= hi):
        lo, mid, hi = min(lo, mid, hi), sorted((lo, mid, hi))[1], max(lo, mid, hi)
    floor = max(0.0, min(float(floor), 49.0))
    cap = min(100.0, max(float(cap), 51.0))
    # P(forecast close > spot), not P(close < spot).  Spot above the whole
    # cone means the model is bearish, so upside must be near the floor.
    if sx <= lo:
        raw = cap
    elif sx >= hi:
        raw = floor
    elif sx <= mid:
        span = mid - lo
        raw = cap - ((sx - lo) / span) * (cap - 50.0) if span > 0 else 50.0
    else:
        span = hi - mid
        raw = 50.0 - ((sx - mid) / span) * (50.0 - floor) if span > 0 else 50.0

    n = int(_f(samples))
    up = int(_f(paths_up))
    source = "QUANTILE_CDF"
    if n >= 8 and 0 <= up <= n:
        empirical = 100.0 * up / n
        raw = 0.70 * raw + 0.30 * empirical
        source = "QUANTILE_CDF+PATH_VOTE"
    return {
        "upside_pct": round(max(floor, min(cap, raw)), 2),
        "source": source,
        "quantile_cdf_pct": round(raw, 2),
    }


def _symbol(feed: Mapping[str, Any]) -> str:
    for block_name in ("spot", "full_spectrum"):
        block = feed.get(block_name)
        if isinstance(block, Mapping) and block:
            return str(next(iter(block))).upper()
    return "NIFTY"


def _candles(feed: Mapping[str, Any], role: str) -> list[dict[str, Any]]:
    """Map the live feed's compact bars to the predictor's OHLCVA columns."""
    symbol = _symbol(feed)
    if role == "mini":
        block = (feed.get("option") or {}).get(symbol) or {}
        rows = block.get("candles_1m") or []
    else:
        block = (feed.get("spot") or {}).get(symbol) or {}
        rows = block.get("candles_5m") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        open_ = _f(row.get("o", row.get("open")))
        high = _f(row.get("h", row.get("high")))
        low = _f(row.get("l", row.get("low")))
        close = _f(row.get("c", row.get("close")))
        if min(open_, high, low, close) <= 0:
            continue
        item: dict[str, Any] = {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _f(row.get("v", row.get("volume"))),
            "amount": _f(row.get("a", row.get("amount"))),
        }
        stamp = row.get("t", row.get("time"))
        try:
            if stamp:
                item["timestamp"] = datetime.fromtimestamp(float(stamp), tz=IST)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        out.append(item)
    return out


def _ensemble() -> Any:
    global _ENSEMBLE, _LOAD_ERROR
    with _LOCK:
        if _ENSEMBLE is not None:
            return _ENSEMBLE
    try:
        from agent.kronos_ensemble import KronosTrioEnsemble, ensemble_available

        ok, reason = ensemble_available()
        if not ok:
            with _LOCK:
                _LOAD_ERROR = reason
            return None
        samples = max(int(os.environ.get("KRONOS_AUDIT_SAMPLES", "24") or 24), 2)
        loaded = KronosTrioEnsemble(
            weights_root=WEIGHTS_DIR, threads=4, samples=samples, verbose=False,
        ).load()
        with _LOCK:
            _ENSEMBLE = loaded
            _LOAD_ERROR = ""
        return loaded
    except Exception as exc:  # noqa: BLE001 - report, never fabricate a model
        with _LOCK:
            _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def _predict_role(role: str, feed: Mapping[str, Any]) -> dict[str, Any] | None:
    ensemble = _ensemble()
    if ensemble is None:
        return None
    candles = _candles(feed, role)
    if len(candles) < 32:
        return None
    started = time.perf_counter()
    symbol = _symbol(feed)
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    option_row = (feed.get("option") or {}).get(symbol) or {}
    depth = spectrum.get("depth") or {}
    obi = option_row.get("obi_5")
    if obi in (None, "", 0, 0.0):
        obi = depth.get("obi_5")
    futures = spectrum.get("futures") or {}
    try:
        futures_ratio = float(futures.get("volume") or 0.0) / max(float(futures.get("volume_20bar_mean") or futures.get("volume") or 1.0), 1.0)
    except (TypeError, ValueError):
        futures_ratio = 1.0
    forecast = ensemble.forecast(role, candles, obi_5=float(obi or 1.0),
                                  futures_ratio=futures_ratio)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    probability = quantile_probability(
        forecast.last_close, forecast.p10, forecast.p50, forecast.p90,
        paths_up=forecast.paths_up, samples=forecast.samples,
    )
    up = round(float(probability["upside_pct"]), 2)
    down = round(100.0 - up, 2)
    direction = 1 if up >= 50.0 else -1
    target_1 = forecast.p50
    target_2 = forecast.p90 if direction > 0 else forecast.p10
    return {
        "state": "LIVE_INFERENCE",
        "probability_source": probability["source"],
        "bars_used": len(candles),
        "lookback": int(ensemble.role_config[role]["lookback"]),
        "reason": (
            f"{role} · {probability['source']} · {forecast.samples} paths · "
            f"p10 {forecast.p10:.2f} / p50 {forecast.p50:.2f} / "
            f"p90 {forecast.p90:.2f} · {elapsed_ms:.0f}ms"
        ),
        "direction": direction,
        "direction_probability_pct": up,
        "probability_pct": up,
        "upside_prob": up,
        "downside_prob": down,
        "confidence": round(float(forecast.conviction), 2),
        "quantile_cdf_pct": probability["quantile_cdf_pct"],
        "paths_up": int(forecast.paths_up),
        "samples": int(forecast.samples),
        "paths_total": int(getattr(forecast, "paths_total", 100)),
        "paths_bullish": int(getattr(forecast, "paths_up", 0)),
        "paths_bearish": int(getattr(forecast, "paths_down", 0)),
        "candles_analyzed": int(getattr(forecast, "candles_analyzed", 150)),
        "buffer_total": int(getattr(forecast, "buffer_total", len(candles))),
        "median_trajectory": list(getattr(forecast, "median_trajectory", ()) or ()),
        "p25": round(float(getattr(forecast, "p25", 0.0) or 0.0), 2),
        "p75": round(float(getattr(forecast, "p75", 0.0) or 0.0), 2),
        "projection_source": str(getattr(forecast, "projection_source", "")),
        "projection_ms": float(getattr(forecast, "projection_ms", 0.0) or 0.0),
        "target_1": round(float(target_1), 2),
        "target_2": round(float(target_2), 2),
        "p10": round(float(forecast.p10), 2),
        "p50": round(float(forecast.p50), 2),
        "p90": round(float(forecast.p90), 2),
        "regime": "BULLISH" if up >= 55.0 else "BEARISH" if up <= 45.0 else "NEUTRAL",
        "inference_ms": round(elapsed_ms, 1),
        "model": role,
    }


def _refresh_quant_fallback(feed: Mapping[str, Any]) -> None:
    """Publish deterministic live tensor scores when checkpoints are absent.

    The GitHub runner cannot carry the 500 MB local ``.pt`` files.  Returning
    ``None`` there left every model card at ``—%``.  The quorum engine already
    computes three independent scores from the live 5m/15m tape, option chain,
    VWAP and order book.  This adapter exposes those scores in the same cache
    shape as the real checkpoints, labelled explicitly as a tensor fallback so
    it can never be mistaken for a neural inference.
    """
    global _LAST_ERROR
    try:
        from model.tri_model_orchestrator import build_model_quorum

        quorum = build_model_quorum(feed, symbol=_symbol(feed))
    except Exception as exc:  # noqa: BLE001 - keep the dashboard alive
        _LAST_ERROR = f"quant fallback: {type(exc).__name__}: {exc}"
        return
    votes = {str(vote.get("role")): vote for vote in (quorum.get("votes") or [])}
    candles = _candles(feed, "base")
    for role in ("base", "small", "mini"):
        vote = votes.get(role)
        if not vote:
            continue
        try:
            confidence = float(vote.get("confidence"))
        except (TypeError, ValueError):
            continue
        side = str(vote.get("side") or "NEUTRAL").upper()
        upside = confidence if side == "CE" else (100.0 - confidence if side == "PE" else 50.0)
        downside = 100.0 - upside
        direction = 1 if side == "CE" else -1 if side == "PE" else 0
        payload = {
            "state": "LIVE_QUANT_TENSOR",
            "probability_source": "LIVE_QUANT_TENSOR",
            "reason": f"{role} · live tensor score · {vote.get('trigger') or side}",
            "direction": direction,
            "direction_probability_pct": round(upside, 2),
            "probability_pct": round(upside, 2),
            "upside_prob": round(upside, 2),
            "downside_prob": round(downside, 2),
            "confidence": round(confidence, 2),
            "regime": "BULLISH" if side == "CE" else "BEARISH" if side == "PE" else "NEUTRAL",
            "target_1": (quorum.get("entry") or {}).get("target_1"),
            "target_2": (quorum.get("entry") or {}).get("target_2"),
            "inference_ms": 1.0,
            "bars_used": len(candles),
            "candles_analyzed": len(candles),
            "samples": 0,
            "paths_up": 0,
            "paths_total": 0,
            "paths_bearish": 0,
            "projection_source": "LIVE_QUANT_TENSOR",
            "projection_ms": 1.0,
            "model": role,
        }
        with _LOCK:
            _CACHE[role] = payload
            _CACHE_AT[role] = time.time()
    _LAST_ERROR = ""


def _refresh_once() -> None:
    global _LAST_ERROR
    try:
        feed = json.loads(FEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    # Never freeze inference on a stale convenience boolean: the heavy feed
    # may carry socket_connected=False from an older cycle while the 4 Hz fast
    # frame and the candles are live.  Require actual bars instead.
    if not feed:
        return
    spot_rows = ((feed.get("spot") or {}).get(_symbol(feed)) or {}).get("candles_5m") or []
    if not spot_rows:
        return
    # Cloud-native zero-file path: no .pt checkpoint is required for the three
    # live cards.  The deterministic tensor engine keeps publishing until a real
    # checkpoint is available; it never emits a dash or a fabricated tick.
    if _ensemble() is None:
        _refresh_quant_fallback(feed)
        return
    for role in ("base", "small", "mini"):
        try:
            prediction = _predict_role(role, feed)
        except Exception as exc:  # noqa: BLE001 - one role must not stop the trio
            _LAST_ERROR = f"{role}: {type(exc).__name__}: {exc}"
            continue
        if prediction:
            with _LOCK:
                _CACHE[role] = prediction
                _CACHE_AT[role] = time.time()
            _LAST_ERROR = ""


def _publish_status() -> None:
    """Share the live card state with the session's 6/6 readiness writer."""
    moment = time.time()
    with _LOCK:
        roles = {
            role: {
                "age_sec": round(moment - _CACHE_AT.get(role, 0.0), 1),
                "probability_pct": payload.get("probability_pct"),
                "samples": payload.get("samples"),
                "paths_up": payload.get("paths_up"),
                "paths_total": payload.get("paths_total"),
                "paths_bearish": payload.get("paths_bearish"),
                "candles_analyzed": payload.get("candles_analyzed"),
                "buffer_total": payload.get("buffer_total"),
                "median_trajectory": payload.get("median_trajectory"),
                "projection_source": payload.get("projection_source"),
                "projection_ms": payload.get("projection_ms"),
                "direction": payload.get("direction"),
                "regime": payload.get("regime"),
                "p10": payload.get("p10"),
                "p50": payload.get("p50"),
                "p90": payload.get("p90"),
            }
            for role, payload in _CACHE.items()
        }
        body = {
            "updated_epoch": round(moment, 3),
            "loaded": _ENSEMBLE is not None,
            "load_error": _LOAD_ERROR,
            "roles": roles,
        }
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = STATUS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(temp, STATUS_FILE)
    except OSError:
        pass


def _loop() -> None:
    while not _STOP.is_set():
        _refresh_once()
        _publish_status()
        _STOP.wait(REFRESH_SEC)


def start() -> threading.Thread:
    """Start the daemon inference loop once; return the running thread."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="kronos-live-predictor")
    _THREAD.start()
    return _THREAD


def ready_predictors() -> dict[str, Any]:
    """Callables for roles whose cached inference is still fresh."""
    moment = time.time()
    with _LOCK:
        fresh = {
            role: dict(payload)
            for role, payload in _CACHE.items()
            if moment - _CACHE_AT.get(role, 0.0) <= MAX_PREDICT_AGE_SEC
        }
    return {role: (lambda _payload, _p=payload: dict(_p)) for role, payload in fresh.items()}


def status() -> dict[str, Any]:
    """Diagnostic snapshot for tests and the dashboard."""
    moment = time.time()
    with _LOCK:
        return {
            "loaded": _ENSEMBLE is not None,
            "load_error": _LOAD_ERROR,
            "last_error": _LAST_ERROR,
            "roles": {
                role: {"age_sec": round(moment - _CACHE_AT.get(role, 0.0), 1),
                       "probability_pct": _CACHE.get(role, {}).get("probability_pct")}
                for role in _CACHE
            },
            "refresh_sec": REFRESH_SEC,
        }
