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
MAX_PREDICT_AGE_SEC = max(float(os.environ.get("KRONOS_AUDIT_MAX_AGE_SEC", "120") or 120), 15.0)

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
    forecast = ensemble.forecast(role, candles)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    up = round(float(forecast.up_pct), 2)
    down = round(100.0 - up, 2)
    direction = 1 if up >= 50.0 else -1
    target_1 = forecast.p50
    target_2 = forecast.p90 if direction > 0 else forecast.p10
    return {
        "state": "LIVE_INFERENCE",
        "reason": (
            f"{role} · {forecast.samples} Monte Carlo paths · "
            f"p10 {forecast.p10:.2f} / p50 {forecast.p50:.2f} / "
            f"p90 {forecast.p90:.2f} · {elapsed_ms:.0f}ms"
        ),
        "direction": direction,
        "direction_probability_pct": up,
        "probability_pct": up,
        "upside_prob": up,
        "downside_prob": down,
        "confidence": round(float(forecast.conviction), 2),
        "paths_up": int(forecast.paths_up),
        "samples": int(forecast.samples),
        "target_1": round(float(target_1), 2),
        "target_2": round(float(target_2), 2),
        "p10": round(float(forecast.p10), 2),
        "p50": round(float(forecast.p50), 2),
        "p90": round(float(forecast.p90), 2),
        "regime": "BULLISH" if up >= 55.0 else "BEARISH" if up <= 45.0 else "NEUTRAL",
        "inference_ms": round(elapsed_ms, 1),
        "model": role,
    }


def _refresh_once() -> None:
    global _LAST_ERROR
    try:
        feed = json.loads(FEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not feed or not feed.get("socket_connected"):
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
