#!/usr/bin/env python3
"""Real-time tick payload for the dual-chart terminal.

The live session (``scripts/upstox_live_session.py``) writes the authoritative
snapshot to ``state/live_feed.json`` every second. This module only normalises
that snapshot for the browser. It never invents a price: when Upstox is
connected, every displayed OHLC is the broker's own value.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from execution.trading_cutoff import cutoff_state
from market.data_sanitizer import feed_state
from market.programmatic_telemetry import build_programmatic_telemetry
from state.telugu_narration_logger import ProgrammaticBrainLogger
from strategy.candle_visual_classifier import classify_candle_visuals
from strategy.master_signal_arbitrator import build_master_arbitrator
from strategy.model_oscillator import build_oscillator_from_feed

REPO_ROOT = Path(__file__).resolve().parents[1]
FEED_FILE = REPO_ROOT / "state" / "live_feed.json"
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"
HEARTBEAT_FILE = REPO_ROOT / "state" / "live_heartbeat.json"
FAST_FILE = REPO_ROOT / "state" / "live_fast.json"
PREDICTOR_STATUS_FILE = REPO_ROOT / "state" / "kronos_predictor_status.json"
OPTION_PIN_FILE = REPO_ROOT / "state" / "live_option_pin.json"
#: The fast publisher writes prices/depth on every socket tick; the heavy chain
#: can take seconds.  A fast frame older than this is not liveness evidence.
FAST_STALE_SEC = 2.0
#: The predictor runs a heavy autoregressive pass; 30 s was shorter than one
#: warm cycle on the 8 GB runner and produced false "WAITING" cards.
PREDICTOR_MAX_AGE_SEC = 900.0

INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
#: A full-spectrum snapshot older than this must never be treated as live.
#: 12 s is the explicit last-known-good hold while the 4 Hz socket is live; the
#: audit still exposes the real chain age so a lag is never disguised as fresh.
FULL_SPECTRUM_STALE_MS = 12_000
LKG_HOLD_MS = 12_000


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _option_pin() -> str:
    """Human watch-leg pin: AUTO, CE or PE.  Never an order instruction."""
    mode = str((_load(OPTION_PIN_FILE) or {}).get("mode") or "AUTO").upper()
    return mode if mode in {"AUTO", "CE", "PE"} else "AUTO"


def _fast_epoch(fast: dict) -> float:
    return _as_float(fast.get("epoch"))


def _apply_fast(feed: dict, fast: dict, now: float) -> dict:
    """Overlay the 4 Hz tick frame on the slower chain snapshot.

    Only volatile values are replaced: LTPs, top-of-book, OBI/TBQ/TSQ and the
    liveness epoch.  Candle history, chains, Greeks and the full spectrum keep
    their own provenance and age stamps.
    """
    merged = dict(feed)
    prices = dict(feed.get("prices") or {})
    for key, value in (fast.get("prices") or {}).items():
        price = _as_float(value)
        if price > 0:
            prices[key] = price
    if prices:
        merged["prices"] = prices

    spot = dict(feed.get("spot") or {}) if isinstance(feed.get("spot"), dict) else {}
    for name, row in (fast.get("spot") or {}).items():
        ltp = _as_float(row.get("ltp"))
        if ltp <= 0:
            continue
        base = dict(spot.get(name) or {})
        base.update({k: v for k, v in row.items() if v is not None})
        base["ltp"] = ltp
        for key in ("current_candle", "current_candle_5m", "current_candle_15m"):
            candle = base.get(key)
            if not isinstance(candle, dict):
                continue
            candle = dict(candle)
            candle["close"] = ltp
            candle["high"] = max(_as_float(candle.get("high"), ltp), ltp)
            candle["low"] = min(_as_float(candle.get("low"), ltp) or ltp, ltp)
            base[key] = candle
        spot[name] = base
    if spot:
        merged["spot"] = spot

    option = dict(feed.get("option") or {}) if isinstance(feed.get("option"), dict) else {}
    for name, row in (fast.get("option") or {}).items():
        if not isinstance(row, dict):
            continue
        base = dict(option.get(name) or {})
        base.update({k: v for k, v in row.items() if v is not None})
        option[name] = base
    if option:
        merged["option"] = option

    depth = dict(feed.get("depth") or {}) if isinstance(feed.get("depth"), dict) else {}
    for name, row in (fast.get("depth") or {}).items():
        if isinstance(row, dict) and row:
            depth[name] = dict(row)
    if depth:
        merged["depth"] = depth

    for key in ("tick_rate", "socket_frames", "socket_frame_age_ms"):
        if fast.get(key) is not None:
            merged[key] = fast[key]
    merged["socket_connected"] = bool(fast.get("socket_connected", True))
    merged["liveness_epoch"] = _as_float(fast.get("epoch"))
    merged["heartbeat_epoch"] = _as_float(fast.get("epoch"))
    merged["fast_age_ms"] = int(max(0.0, now - _fast_epoch(fast)) * 1000.0)
    return merged


def effective_feed(feed: dict, now: float | None = None) -> dict:
    """Overlay the fast socket frame on the slower full-snapshot cycle.

    The session publishes ``state/live_fast.json`` at 4 Hz from its in-memory
    websocket state.  That is the price/liveness authority; the heavy option
    chain keeps its own real age (``updated_epoch``/``generated_epoch``) so a
    genuinely old chain still trips spectrum staleness.
    """
    moment = time.time() if now is None else float(now)
    fast = _load(FAST_FILE)
    fast_epoch = _fast_epoch(fast)
    if fast_epoch and moment - fast_epoch <= FAST_STALE_SEC and fast.get("socket_connected"):
        return _apply_fast(feed, fast, moment)
    beat = _load(HEARTBEAT_FILE)
    try:
        beat_epoch = float(beat.get("epoch") or 0.0)
    except (TypeError, ValueError):
        beat_epoch = 0.0
    try:
        feed_epoch = float(feed.get("updated_epoch") or 0.0)
    except (TypeError, ValueError):
        feed_epoch = 0.0
    if beat_epoch > feed_epoch and moment - beat_epoch <= 2.0 and beat.get("connected"):
        # A real socket frame arrived within the last 2s; the 5s snapshot may
        # still carry socket_connected=False from the previous heavy cycle.
        return {**feed, "socket_connected": True,
                "heartbeat_epoch": beat_epoch,
                "liveness_epoch": beat_epoch,
                "heartbeat_frame_age_ms": beat.get("socket_frame_age_ms")}
    return feed


def pick_price(feed: dict, symbol: str) -> float:
    """REST and socket use different key spellings; accept both."""
    prices = feed.get("prices") or {}
    key = INDEX_KEYS.get(symbol, symbol)
    for candidate in (key, key.replace("|", ":")):
        value = prices.get(candidate)
        if value:
            return float(value)
    return 0.0


def _predictor_plan(feed: dict, symbol: str, now: float) -> dict[str, Any]:
    """Use the live 3-model forecast cache, never a stale cloud plan."""
    if str(symbol).upper() != "NIFTY":
        return {}
    status = _load(PREDICTOR_STATUS_FILE)
    try:
        updated = float(status.get("updated_epoch") or 0.0)
    except (TypeError, ValueError):
        return {}
    if not updated or now - updated > PREDICTOR_MAX_AGE_SEC:
        return {}
    roles = status.get("roles") or {}
    spot_row = ((feed.get("spot") or {}).get(symbol) or {})
    spot = float(spot_row.get("ltp") or pick_price(feed, symbol) or 0.0)
    if spot <= 0:
        return {}
    # Use the fresh model whose median is closest to live spot.  The 102M model
    # remains the primary; the 25M is selected only when its median is closer
    # and the forecast is still inside a sane 2.5% band.  Never use cloud levels.
    candidates: list[tuple[float, str, dict, float, float, float, float]] = []
    for role_name in ("base", "small"):
        row = roles.get(role_name) or {}
        try:
            p10 = float(row.get("p10") or 0.0)
            p50 = float(row.get("p50") or 0.0)
            p90 = float(row.get("p90") or 0.0)
            probability = float(row.get("probability_pct"))
        except (TypeError, ValueError):
            continue
        if not (p10 > 0 and p50 > 0 and p90 > 0):
            continue
        if max(abs(spot - p10), abs(spot - p90)) > spot * 0.025:
            continue
        candidates.append((abs(spot - p50), role_name, row, p10, p50, p90, probability))
    if not candidates:
        return {}
    _, role, chosen, p10, p50, p90, probability = min(candidates, key=lambda item: item[0])
    action = "BUY_CE" if probability >= 55.0 else "BUY_PE" if probability <= 45.0 else "NO_TRADE"
    target1 = p90 if action == "BUY_CE" else p10 if action == "BUY_PE" else p50
    paths_total = int(chosen.get("paths_total") or chosen.get("samples") or 100)
    paths_up = int(chosen.get("paths_up") or 0)
    return {
        "paths_up": paths_up,
        "samples": paths_total,
        "paths_total": paths_total,
        "paths_bearish": int(chosen.get("paths_bearish") or max(0, paths_total - paths_up)),
        "candles_analyzed": chosen.get("candles_analyzed") or 150,
        "buffer_total": chosen.get("buffer_total") or 512,
        "median_trajectory": chosen.get("median_trajectory") or [],
        "projection_source": chosen.get("projection_source") or "",
        "projection_ms": chosen.get("projection_ms"),
        "model_role": role,
        "median": round(p50, 2), "p10": round(p10, 2), "p50": round(p50, 2),
        "p90": round(p90, 2), "spot": round(spot, 2),
        "regime": chosen.get("regime"), "action": action,
        "watch_entry": round(p50, 2), "target1": round(target1, 2),
        "target2": round(p90, 2),
        "stop_loss": round(p10 if action == "BUY_CE" else p90, 2),
        "source": "KRONOS_LIVE_PREDICTOR",
    }


def _anchored_plan(plan: dict, spot: float, atr: float,
                   *, action: str = "NO_TRADE") -> dict[str, Any]:
    """Re-anchor a stale forecast corridor to the live spot.

    A cloud plan cached hours ago (25,044 vs a 25,385 tape) is not a forecast.
    When the stored corridor is absent or more than 1.2% away from the live
    price, rebuild it from the live ATR so the entry/SL/target geometry is
    still an honest volatility band around the current market — never a
    fabricated tick, and never a stale level masquerading as live.
    """
    out = dict(plan or {})
    if spot <= 0:
        return out
    p10, p50, p90 = (_as_float(out.get(key)) for key in ("p10", "p50", "p90"))
    ordered = p10 > 0 and p50 > 0 and p90 > 0 and p10 <= p50 <= p90
    near = ordered and max(abs(spot - p10), abs(spot - p90)) <= spot * 0.012
    if not near:
        band = max(float(atr or 0.0) * 2.5, spot * 0.0015)
        band = min(band, spot * 0.012)
        p10, p50, p90 = spot - band * 0.85, spot, spot + band
        out["reanchored"] = True
        out["source"] = out.get("source") or "LIVE_SPOT_ATR_BAND"
    side = "CE" if action == "BUY_CE" else "PE" if action == "BUY_PE" else ""
    # Enforce a monotonic trade geometry: for a bullish setup the ladder is
    # SL < Entry < T1 < T2.  The predictor only supplies p10/p50/p90; the
    # intermediate T1 is the 55% projection towards p90.
    if side == "PE":
        target1, target2, stop = p50 - (p90 - p50) * 0.55, p10, p90
    else:
        target1, target2, stop = p50 + (p90 - p50) * 0.55, p90, p10
    out.update({
        "p10": round(p10, 2), "p50": round(p50, 2), "p90": round(p90, 2),
        "median": round(p50, 2), "spot": round(spot, 2),
        "watch_entry": round(p50, 2),
        "target1": round(target1, 2), "target2": round(target2, 2),
        "stop_loss": round(stop, 2),
    })
    return out


def fast_overlay(payload: dict, now: float | None = None) -> dict:
    """Patch an already-built SSE snapshot with a new 4 Hz tick frame.

    Used by the dashboard fan-out so a 200-400 ms price change is pushed
    without rebuilding the option chain, narration and candle classifiers.
    """
    moment = time.time() if now is None else float(now)
    fast = _load(FAST_FILE)
    epoch = _fast_epoch(fast)
    if not epoch or moment - epoch > FAST_STALE_SEC:
        return payload
    out = dict(payload)
    out["age_ms"] = max(0, int((moment - epoch) * 1000.0))
    out["ts"] = round(moment, 3)
    out["live"] = True
    out["stale"] = False
    out["as_of"] = fast.get("as_of") or payload.get("as_of")
    # A fresh socket frame is the gate authority.  Without this, a heavy cycle
    # that began during a 3s lull leaves "BLOCKED" frozen in the SSE payload
    # even though every subsequent 4 Hz frame is live.
    gate = {
        "live": True, "allowed": True, "status": "LIVE", "reason": "VERIFIED_LIVE",
        "age_ms": out["age_ms"], "age_sec": round(out["age_ms"] / 1000.0, 3),
        "feed_status": "FEED_LIVE",
    }
    out["data_gate"] = gate
    out["feed_status"] = {
        **(payload.get("feed_status") or {}),
        "status": "FEED_LIVE", "age_ms": out["age_ms"],
        "market_open": (payload.get("feed_status") or {}).get("market_open", True),
        "allow_order_generation": True, "reason": "SANITIZED_FEED_LIVE", "ok": True,
    }
    out["entries_suspended"] = False
    arb = dict(payload.get("master_arbitrator") or {})
    if arb:
        arb["data_gate"] = dict(gate)
        out["master_arbitrator"] = arb
    out["socket"] = {**(payload.get("socket") or {}),
                     "connected": bool(fast.get("socket_connected")),
                     "tick_rate": fast.get("tick_rate", (payload.get("socket") or {}).get("tick_rate", 0.0)),
                     "frames": fast.get("socket_frames", (payload.get("socket") or {}).get("frames", 0)),
                     "latency_ms": fast.get("socket_frame_age_ms", (payload.get("socket") or {}).get("latency_ms", 0))}
    spot = dict(payload.get("spot") or {})
    for name, row in (fast.get("spot") or {}).items():
        ltp = _as_float(row.get("ltp"))
        if ltp <= 0:
            continue
        base = dict(spot.get(name) or {})
        base["ltp"] = ltp
        base["display"] = ltp
        for key in ("current_candle", "current_candle_5m", "current_candle_15m"):
            candle = base.get(key)
            if isinstance(candle, dict):
                candle = dict(candle)
                candle["close"] = ltp
                candle["high"] = max(_as_float(candle.get("high"), ltp), ltp)
                candle["low"] = min(_as_float(candle.get("low"), ltp) or ltp, ltp)
                base[key] = candle
        spot[name] = base
    out["spot"] = spot
    indices = dict(payload.get("indices") or {})
    for name, row in (fast.get("spot") or {}).items():
        ltp = _as_float(row.get("ltp"))
        if ltp <= 0:
            continue
        base = dict(indices.get(name) or {})
        base["ltp"] = round(ltp, 2)
        base["change"] = row.get("change", base.get("change"))
        base["change_pct"] = row.get("pct_change", base.get("change_pct"))
        indices[name] = base
    if indices:
        out["indices"] = indices
    option = dict(payload.get("option") or {})
    for name, row in (fast.get("option") or {}).items():
        if isinstance(row, dict):
            option[name] = {**(option.get(name) or {}), **row}
    if option:
        out["option"] = option
        oc = dict(payload.get("option_chart") or {})
        for name, row in (fast.get("option") or {}).items():
            chosen = ((option.get(name) or {}).get("strike_symbol"))
            if chosen and chosen == oc.get("strike_symbol"):
                for key in ("ltp", "bid", "ask", "obi_5", "delta", "iv"):
                    if row.get(key) is not None:
                        oc[key] = row[key]
        out["option_chart"] = oc
    return out


def fast_delta(symbol: str = "NIFTY", now: float | None = None) -> dict:
    """Small event payload for a named ``tick_delta`` SSE frame.

    The browser merges this into its current snapshot; it is intentionally not
    a full 4,000-node snapshot.  A stale fast file returns an empty object so
    the caller keeps polling instead of inventing a tick.
    """
    moment = time.time() if now is None else float(now)
    fast = _load(FAST_FILE)
    epoch = _fast_epoch(fast)
    if not epoch or moment - epoch > FAST_STALE_SEC:
        return {}
    age_ms = max(0, int((moment - epoch) * 1000.0))
    spot = ((fast.get("spot") or {}).get(symbol) or {}) if isinstance(fast.get("spot"), dict) else {}
    option = ((fast.get("option") or {}).get(symbol) or {}) if isinstance(fast.get("option"), dict) else {}
    indices = {}
    if spot:
        indices[symbol] = {"ltp": spot.get("ltp"), "change": spot.get("change"),
                           "change_pct": spot.get("pct_change")}
    return {
        "fast": True,
        "tick_seq": fast.get("tick_seq"),
        "epoch": epoch,
        "age_ms": age_ms,
        "as_of": fast.get("as_of"),
        "socket_connected": bool(fast.get("socket_connected")),
        "spot": spot,
        "option": option,
        "indices": indices,
        "data_gate": {"live": True, "allowed": True, "status": "LIVE",
                      "reason": "FAST_TICK", "age_ms": age_ms},
        "entries_suspended": False,
    }


def _indices(feed: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    spot = feed.get("spot") if isinstance(feed.get("spot"), dict) else {}
    for name in ("NIFTY", "BANKNIFTY"):
        row = spot.get(name) if isinstance(spot.get(name), dict) else {}
        ltp = row.get("ltp") or pick_price(feed, name)
        if not ltp:
            continue
        out[name] = {
            "ltp": round(float(ltp), 2),
            "change": row.get("change"),
            "change_pct": row.get("pct_change"),
        }
    return out


def _zone(price: float, vwap: float) -> str:
    if not price or not vwap:
        return "UNKNOWN"
    return "ABOVE" if price >= vwap else "BELOW"


def _fallback_spot(state: dict, symbol: str) -> dict:
    block = (state.get("symbols") or {}).get(symbol) or {}
    candles = block.get("candles") or []
    last = candles[-1] if candles else {}
    spot = block.get("spot") or last.get("c") or 0.0
    return {
        "symbol": symbol,
        "ltp": spot,
        "change": 0.0,
        "pct_change": 0.0,
        "vwap": (block.get("vwap") or [spot])[-1] if block else spot,
        "atr": 0.0,
        "current_candle": {
            "time": last.get("t"),
            "open": last.get("o", spot),
            "high": last.get("h", spot),
            "low": last.get("l", spot),
            "close": last.get("c", spot),
            "volume": last.get("v", 0),
        },
        "candles": candles[-512:],
        "candles_1m": (block.get("candles_1m") or candles)[-512:],
        "candles_5m": (block.get("candles_5m") or candles)[-512:],
        "candles_15m": block.get("candles_15m") or [],
        "countdown_sec": countdown(),
        "source": "snapshot",
    }


def _stamp_full_spectrum(feed: dict, now: float) -> tuple[dict[str, Any], bool]:
    """Stamp every full-spectrum snapshot with as_of / age_ms / stale."""
    block = feed.get("full_spectrum") or {}
    if not isinstance(block, dict):
        return {}, False
    feed_epoch = float(feed.get("updated_epoch") or 0.0)
    feed_as_of = feed.get("updated_at")
    stamped: dict[str, Any] = {}
    any_stale = False
    for symbol, raw in block.items():
        if not isinstance(raw, dict):
            stamped[symbol] = raw
            continue
        snap = dict(raw)
        epoch = float(snap.get("generated_epoch") or feed_epoch or 0.0)
        age_ms = int(max(0.0, now - epoch) * 1000.0) if epoch > 0 else None
        fixture = bool(snap.get("fixture") or feed.get("fixture"))
        stale = fixture or age_ms is None or age_ms > FULL_SPECTRUM_STALE_MS
        lkg_held = bool(
            not stale and age_ms is not None and age_ms > 5_000
            and feed.get("socket_connected")
        )
        snap["as_of"] = snap.get("as_of") or feed_as_of
        snap["updated_epoch"] = epoch if epoch > 0 else None
        snap["fixture"] = fixture
        snap["fixture_blocked"] = fixture
        snap["source"] = "offline-fixture" if fixture else (
            snap.get("source") or "upstox-v3"
        )
        snap["age_ms"] = age_ms
        snap["chain_age_ms"] = age_ms
        snap["lkg_held"] = lkg_held
        snap["lkg_window_ms"] = LKG_HOLD_MS
        snap["stale"] = stale
        # LKG keeps the last verified chain visible while the socket still
        # ticks.  A genuinely dead/old chain still suppresses entries.
        snap["entries_suspended"] = bool(snap.get("entries_suspended")) or stale
        any_stale = any_stale or stale
        stamped[symbol] = snap
    return stamped, any_stale


def countdown(now: float | None = None) -> dict[str, int]:
    now = now or time.time()
    five = 300 - int(now) % 300
    one = 60 - int(now) % 60
    return {"bar_5m": five, "bar_1m": one}


def snapshot(symbol: str = "NIFTY", now: float | None = None) -> dict[str, Any]:
    """The payload pushed over SSE and polled by the phone."""
    now = now or time.time()
    feed = effective_feed(_load(FEED_FILE), now)
    state = _load(STATE_FILE)
    full_spectrum, spectrum_stale = _stamp_full_spectrum(feed, now)

    # Socket liveness is the 1s heartbeat; the full snapshot may lag a heavy
    # chain cycle.  Full-spectrum staleness still uses the snapshot epoch.
    live_epoch = float(feed.get("liveness_epoch") or feed.get("updated_epoch") or 0)
    age_ms = max(0, int((now - live_epoch) * 1000))
    flag = feed.get("market_open")
    gate = feed_state(
        feed, now=now,
        market_open=flag if isinstance(flag, bool) else True,
    )
    live = bool(feed) and gate.status == "FEED_LIVE"

    # The session writes per-symbol maps; older feeds used a flat dict.
    def _for(value: dict | None) -> dict:
        value = value or {}
        if isinstance(value, dict) and "ltp" in value:
            return value
        return (value.get(symbol) if isinstance(value, dict) else None) or {}

    # Prefer the session's real spot even when the heartbeat is a few seconds
    # stale.  The zero-fake gate still marks it stale and blocks entries, but
    # the chart must never fall back to months-old cloud candles while a live
    # session is writing today's bars.
    spot_block = {**_fallback_spot(state, symbol), **_for(feed.get("spot"))}

    option = _for(feed.get("option"))
    trade = _for(feed.get("trade"))
    scanner = (feed.get("scanner") or {}).get(symbol) if isinstance(feed.get("scanner"), dict) else feed.get("scanner")

    if live:
        price = pick_price(feed, symbol)
        spot_meta = ((feed.get("spot") or {}).get(symbol) if isinstance(feed.get("spot"), dict) else {}) or {}
        # No synthetic micro-tick: display and close are the exact broker price.
        display = price
        synthetic = False
        current = (spot_meta.get("current_candle") or {})
        if current:
            current = dict(current)
            # The high/low must include the newest tick, not just the LTP: a candle
            # whose OHLC only follows the close draws as a flat line.
            current["high"] = max(float(current.get("high") or price), price)
            current["low"] = min(float(current.get("low") or price), price)
            current["close"] = price
        # The spot chart is a 5m book. The session rolls the forming 5m bucket;
        # pass it through so the browser can update() that bar instead of falling
        # back to the 1m series (which used to take over whenever it was fresher).
        current5 = (spot_meta.get("current_candle_5m") or {})
        if current5:
            current5 = dict(current5)
            current5["high"] = max(float(current5.get("high") or price), price)
            current5["low"] = min(float(current5.get("low") or price), price)
            current5["close"] = price
        current15 = (spot_meta.get("current_candle_15m") or {})
        if current15:
            current15 = dict(current15)
            current15["high"] = max(float(current15.get("high") or price), price)
            current15["low"] = min(float(current15.get("low") or price), price)
            current15["close"] = price
        spot_block.update(
            {
                "symbol": symbol,
                "ltp": price,
                "display": display,
                "synthetic": False,
                "change": spot_meta.get("change", 0.0),
                "pct_change": spot_meta.get("pct_change", 0.0),
                "vwap": spot_meta.get("vwap") or spot_block["vwap"],
                "atr": spot_meta.get("atr", 0.0),
                "current_candle": current or spot_block["current_candle"],
                "current_candle_5m": current5 or None,
                "current_candle_15m": current15 or None,
                "candles_1m": spot_meta.get("candles_1m") or [],
                "candles_5m": spot_meta.get("candles_5m") or [],
                "candles_15m": spot_meta.get("candles_15m") or [],
                "countdown_sec": countdown(now),
                "source": "upstox-v3" if feed.get("socket_connected") else "upstox-rest",
            }
        )

    option = dict(option)
    if option:
        option.setdefault("countdown_sec", countdown(now))
        option["zone"] = _zone(float(option.get("ltp") or 0), float(option.get("atp_vwap") or 0))

    if not live:
        spot_block["source"] = "stale-snapshot"
        spot_block["display_allowed"] = bool(spot_block.get("ltp"))

    # Liveness is the fast 4 Hz socket frame; a heavy chain snapshot is allowed
    # to be seconds old without suspending entries.  A genuinely dead socket
    # still fails closed here and in the arbitrator.
    suspended = not gate.allow_order_generation or spectrum_stale
    programmatic = feed.get("programmatic")
    if not isinstance(programmatic, dict) or not programmatic:
        programmatic = build_programmatic_telemetry(feed, symbol=symbol, now=now)
    predictor_plan = _predictor_plan(feed, symbol, now)
    oscillator = feed.get("model_oscillator")
    if not isinstance(oscillator, dict) or not oscillator:
        oscillator = build_oscillator_from_feed(feed, symbol, plan=predictor_plan or None)
    thoughts = feed.get("model_thoughts_te")
    brain_log = feed.get("brain_log_te")
    if not isinstance(thoughts, list) or not thoughts or not isinstance(brain_log, list):
        narration_logger = ProgrammaticBrainLogger(max_entries=80)
        narration_now = feed.get("updated_epoch") or now
        speed = programmatic.get("speed_telemetry") or {}
        narration = narration_logger.record_cycle(
            feed,
            symbol=symbol,
            oscillator=oscillator,
            scan_ms=speed.get("model_scan_latency_ms") or 0.0,
            sanitize_ms=(speed.get("python_sanitize_us") or 0.0) / 1000.0,
            latency_ms=speed.get("exchange_to_server_latency_ms") or 0.0,
            now=narration_now,
        )
        thoughts, brain_log = narration["thoughts"], narration["log"]

    state_block = (state.get("symbols") or {}).get(symbol) or {}
    plan = dict(state_block.get("plan") or {})
    if predictor_plan:
        plan.update(predictor_plan)
    live_spot = ((feed.get("spot") or {}).get(symbol) or {}) if isinstance(feed.get("spot"), dict) else {}
    source_bars = (
        live_spot.get("candles_5m") or spot_block.get("candles_5m")
        or live_spot.get("candles_1m") or spot_block.get("candles_1m")
        or state_block.get("candles") or []
    )
    spectrum = (full_spectrum.get(symbol) or {}) if isinstance(full_spectrum, dict) else {}
    depth = spectrum.get("depth") or {}
    smc = (feed.get("smc") or {}).get(symbol) if isinstance(feed.get("smc"), dict) else {}
    spot_frames: dict[str, list] = {
        "1m": live_spot.get("candles_1m") or spot_block.get("candles_1m") or source_bars,
        "5m": source_bars,
        "15m": live_spot.get("candles_15m") or spot_block.get("candles_15m") or source_bars,
    }
    spot_by_tf = {
        tf: classify_candle_visuals(
            bars,
            p10=plan.get("p10"), p90=plan.get("p90"),
            obi_5=depth.get("obi_5"), tbq_tsq=depth.get("tbq_tsq"),
            smc=smc if isinstance(smc, dict) else None,
            feed_live=live,
            limit=180,
        )
        for tf, bars in spot_frames.items()
    }
    spot_visuals = spot_by_tf["5m"]
    option_bars = option.get("candles_1m") or option.get("candles_5m") or []
    option_frames: dict[str, list] = {
        "1m": option.get("candles_1m") or option_bars,
        "5m": option.get("candles_5m") or option_bars,
        "15m": option.get("candles_15m") or option_bars,
    }
    option_by_tf = {
        tf: classify_candle_visuals(
            bars,
            p10=option.get("target_1"), p90=option.get("target_2_p90"),
            obi_5=option.get("obi_5"), tbq_tsq=option.get("tbq_tsq"),
            feed_live=live, limit=120,
        )
        for tf, bars in option_frames.items()
    }
    option_visuals = option_by_tf["1m"]
    # The session's embedded readiness can lag the live tunnel probe by one
    # heavy cycle (5/6 vs 6/6 in the same modal).  Use the same runtime builder
    # the /api/readiness endpoint uses so every surface agrees.
    autopilot = feed.get("autopilot")
    try:
        from market.autopilot_readiness import build_from_runtime, load_readiness
        from market.upstox_health import load_access_token, load_token_meta

        runtime = build_from_runtime(
            feed=feed,
            readiness=load_readiness(),
            token_present=bool(load_access_token()),
            token_meta=load_token_meta(),
        )
        if isinstance(runtime, dict) and runtime.get("checks"):
            autopilot = runtime
    except Exception:  # noqa: BLE001 - readiness must never break the tape
        autopilot = autopilot or {}
    autopilot = autopilot if isinstance(autopilot, dict) else {}
    bull = float(oscillator.get("bullish_upside_prob_pct") or 0.0)
    bear = float(oscillator.get("bearish_downside_prob_pct") or 0.0)
    arbitrator = build_master_arbitrator(
        feed, symbol=symbol, now=now, feed_live=live,
    )
    winner = ((arbitrator.get("sleeve_b") or {}).get("selected")
              or (arbitrator.get("sleeve_a") or {}).get("selected") or {})
    direction = "BULL" if bull >= 58.0 else "BEAR" if bear >= 58.0 else "CHOP"
    action = "BUY_CE" if direction == "BULL" else "BUY_PE" if direction == "BEAR" else "NO_TRADE"
    plan = _anchored_plan(plan, _as_float(spot_block.get("ltp") or spot_block.get("display")),
                          _as_float(spot_block.get("atr")), action=action)
    entry = plan.get("watch_entry") or plan.get("p10") or spot_block.get("ltp")
    target1 = plan.get("target1") or plan.get("p65") or plan.get("median")
    target2 = plan.get("target2") or plan.get("p90")
    if direction == "BULL":
        call_text = (f"🟢 KRONOS AI: {bull:.0f}% BULLISH UPSIDE · EARLY BUY ZONE @ "
                     f"{float(entry or 0):,.2f} ➔ TARGET {float(target1 or target2 or 0):,.2f}")
    elif direction == "BEAR":
        call_text = (f"🔴 KRONOS AI: {bear:.0f}% BEARISH DOWNSIDE · EARLY PE ZONE @ "
                     f"{float(entry or 0):,.2f} ➔ TARGET {float(target1 or target2 or 0):,.2f}")
    else:
        call_text = "🟡 SIDEWAYS CHOP — WAITING FOR BREAKOUT"
    chart_call = {
        "direction": direction,
        "side": "CE" if direction == "BULL" else "PE" if direction == "BEAR" else "",
        "bullish_pct": round(bull, 1), "bearish_pct": round(bear, 1),
        "entry": round(float(entry or 0.0), 2),
        "target_1": round(float(target1 or 0.0), 2),
        "target_2": round(float(target2 or 0.0), 2),
        "text": call_text,
        "p10": round(float(plan.get("p10") or 0.0), 2),
        "p90": round(float(plan.get("p90") or 0.0), 2),
        "source": "MASTER_ARBITRATOR" if winner else "MODEL_FUSION",
        "selected_engine": winner.get("engine"),
        "feed_live": live,
    }
    trade_open = str(trade.get("status") or "").upper() == "OPEN"
    # IDLE has no moving bracket.  The only reference line is VWAP/ATP plus one
    # trigger zone anchored to the last *closed* 5m candle.  Showing plan-derived
    # ENTRY/SL/T1/T2 while flat was the root cause of all four lines chasing LTP.
    five_bars = ((feed.get("spot") or {}).get(symbol) or {}).get("candles_5m") or []
    anchor = five_bars[-2] if len(five_bars) >= 2 else (five_bars[-1] if five_bars else {})
    anchor_time = anchor.get("t", anchor.get("time")) if isinstance(anchor, dict) else None
    trigger_price = None
    if isinstance(anchor, dict):
        trigger_price = anchor.get("high") if direction == "BULL" else anchor.get("low")
    chart_levels = {
        "p10": plan.get("p10"), "p50": plan.get("p50"), "p90": plan.get("p90"),
        "median": plan.get("median"),
        "atp": option.get("atp_vwap"),
        "levels_locked": bool(trade_open),
        "levels_state": "POSITION_LOCKED" if trade_open else "IDLE_STATIC",
        "trigger_zone": round(_as_float(trigger_price), 2) if _as_float(trigger_price) > 0 else None,
        "trigger_zone_time": anchor_time,
    }
    if trade_open:
        # Frozen at the exact fill/exit record.  SL only changes when the trade
        # manager books a leg or moves a gear; never from a price tick.
        chart_levels.update({
            "entry": trade.get("entry_price"),
            "sl": trade.get("current_trailing_sl"),
            "t1": trade.get("target_1"),
            "t2": trade.get("target_2_p90"),
        })
    scalp_sleeve = dict(feed.get("scalp_sleeve") or {})
    scalp_top = {
        key: scalp_sleeve.get(key)
        for key in (
            "scalp_capital_allocated", "scalp_capital_deployed",
            "scalp_live_open_pnl", "scalp_realized_pnl_today",
            "scalp_total_pnl_today", "scalp_roi_pct_today",
            "combined_intraday_net_pnl", "scalp_stats",
            "scalp_active", "scalp_last_closed", "scalp_live_ticker",
        )
        if key in scalp_sleeve
    }
    chain_age_ms = max(
        (int(snap.get("age_ms") or 0) for snap in full_spectrum.values()
         if isinstance(snap, dict)), default=0,
    )
    lkg_active = bool(live and not spectrum_stale and chain_age_ms > 5_000)
    data_gate = {
        "live": bool(live),
        "allowed": bool(live),
        "status": "LIVE" if live else "BLOCKED",
        "reason": "VERIFIED_LIVE" if live else gate.reason,
        "age_ms": age_ms,
        "age_sec": round(age_ms / 1000.0, 3),
        "feed_status": gate.status,
        "lkg": lkg_active,
        "chain_age_ms": chain_age_ms,
        "chain_stale": bool(spectrum_stale),
        "lkg_window_ms": LKG_HOLD_MS,
    }
    return {
        "ok": bool(live),
        "ts": round(now, 3),
        "age_ms": age_ms,
        "live": live,
        "data_gate": data_gate,
        "plan": plan,
        "depth": depth,
        "feed_status": gate.as_dict(),
        "order_generation_allowed": bool(gate.allow_order_generation and live),
        "entries_suspended": bool(spectrum_stale),
        "lkg_active": lkg_active,
        "chain_age_ms": chain_age_ms,
        "option_pin": _option_pin(),
        "option_selection": option.get("selection") or {},
        "fixture": bool(feed.get("fixture") or any(
            isinstance(row, dict) and bool(row.get("fixture"))
            for row in full_spectrum.values()
        )),
        "socket": {
            "connected": bool(feed.get("socket_connected")) and live,
            "tick_rate": feed.get("tick_rate", 0.0),
            "frames": feed.get("socket_frames", 0),
            "latency_ms": round(float(feed.get("socket_last_frame_age") or 0) * 1000),
        },
        "spot": spot_block,
        "indices": _indices(feed),
        "option": option or None,
        "active_trade": trade or None,
        "scanner_xray": scanner,
        "programmatic": programmatic,
        "speed_telemetry": programmatic.get("speed_telemetry") or {},
        "data_pipe_health": programmatic.get("data_pipe_health") or [],
        "model_oscillator": oscillator,
        "model_thoughts_te": thoughts,
        "brain_log_te": brain_log,
        "master_arbitrator": arbitrator,
        "chart_call": chart_call,
        "chart_levels": chart_levels,
        "model_projection": {
            "paths_total": plan.get("paths_total"),
            "paths_bullish": plan.get("paths_up"),
            "paths_bearish": plan.get("paths_bearish"),
            "candles_analyzed": plan.get("candles_analyzed"),
            "buffer_total": plan.get("buffer_total"),
            "median_trajectory": plan.get("median_trajectory") or [],
            "p10": plan.get("p10"), "p50": plan.get("p50"), "p90": plan.get("p90"),
            "source": plan.get("projection_source"),
            "projection_ms": plan.get("projection_ms"),
            "model_role": plan.get("model_role"),
        },
        "candle_visuals": {
            "symbol": symbol,
            "spot": spot_visuals,
            "option": option_visuals,
            "spot_by_tf": spot_by_tf,
            "option_by_tf": option_by_tf,
        },
        "option_chart": {
            "strike_symbol": option.get("strike_symbol"),
            "selection": option.get("selection") or {},
            "ltp": option.get("ltp"), "delta": option.get("delta"),
            "gamma": option.get("gamma"), "iv": option.get("iv"),
            "obi_5": option.get("obi_5"), "atp_vwap": option.get("atp_vwap"),
            "visuals": option_visuals, "by_tf": option_by_tf,
        },
        "mtf": feed.get("mtf") or {},
        "smart_entry": feed.get("smart_entry") or {},
        "parity": feed.get("parity") or {},
        "full_spectrum": full_spectrum,
        "full_spectrum_stale": spectrum_stale,
        "option_chain": feed.get("option_chain") or {},
        "kronos_inputs": feed.get("kronos_inputs") or {},
        "trade_mode": feed.get("trade_mode") or {"mode": "PAPER", "real": False},
        "story": feed.get("story") or [],
        "story_at": feed.get("story_at"),
        "hud": feed.get("hud") or {
            "starting_capital": 100_000.0, "realized_today": 0.0,
            "capital": 100_000.0, "trades_today": 0,
            "profit_vault": 0.0, "tradable_capital": 100_000.0,
            "sniper_trades": 0, "daily_lock": False,
        },
        "eod_pnl": feed.get("eod_pnl") or {},
        "sniper_sleeve": feed.get("sniper_sleeve") or {},
        "trigger_badge": (
            (feed.get("trade") or {}).get(symbol, {}) or {}
        ).get("trigger_label") or ((feed.get("quorum") or {}).get(symbol, {}) or {}).get("trigger_label") or "",
        "stale": not live,
        "as_of": feed.get("updated_at"),
        "cutoff": feed.get("cutoff") or cutoff_state(),
        "autopilot_status": autopilot.get("status") or feed.get("autopilot_status"),
        "autopilot_green": autopilot.get("green", feed.get("autopilot_green")),
        "autopilot": autopilot,
        "scalp_sleeve": scalp_sleeve,
        "parallel_alpha": feed.get("parallel_alpha") or {},
        "parallel_alpha_pnl": (feed.get("parallel_alpha") or {}).get("pnl_contributions") or {},
        **scalp_top,
    }


def overlay_lines(trade: dict | None, height: int, top: int, bottom: int,
                  lo: float, hi: float) -> list[dict[str, Any]]:
    """Vertical coordinates for the entry/SL/target lines (tested directly)."""
    if not trade or hi <= lo:
        return []
    rows = [
        ("entry", trade.get("entry_price"), "#4aa3ff", f"BUY {trade.get('qty', 0)} @ ₹{trade.get('entry_price', 0):.2f}"),
        ("sl", trade.get("current_trailing_sl"), "#ff5c5c",
         f"SL ₹{trade.get('current_trailing_sl', 0):.2f} · Gear {trade.get('tsl_gear', 1)}"),
        ("target1", trade.get("target_1"), "#3ddc97",
         f"50% @ ₹{trade.get('target_1', 0):.2f}"),
        ("target2", trade.get("target_2_p90"), "#b57bff",
         f"Final @ ₹{trade.get('target_2_p90', 0):.2f}"),
    ]
    out = []
    span = hi - lo
    for key, price, color, label in rows:
        if price is None:
            continue
        price = float(price)
        y = bottom - (price - lo) / span * (bottom - top)
        out.append({"key": key, "price": price, "color": color, "label": label,
                    "y": round(max(top, min(bottom, y)), 2)})
    return out
