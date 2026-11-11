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

INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
#: A full-spectrum snapshot older than this must never be treated as live.
FULL_SPECTRUM_STALE_MS = 5000


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pick_price(feed: dict, symbol: str) -> float:
    """REST and socket use different key spellings; accept both."""
    prices = feed.get("prices") or {}
    key = INDEX_KEYS.get(symbol, symbol)
    for candidate in (key, key.replace("|", ":")):
        value = prices.get(candidate)
        if value:
            return float(value)
    return 0.0


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
        snap["as_of"] = snap.get("as_of") or feed_as_of
        snap["updated_epoch"] = epoch if epoch > 0 else None
        snap["fixture"] = fixture
        snap["fixture_blocked"] = fixture
        snap["source"] = "offline-fixture" if fixture else (
            snap.get("source") or "upstox-v3"
        )
        snap["age_ms"] = age_ms
        snap["stale"] = stale
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
    feed = _load(FEED_FILE)
    state = _load(STATE_FILE)
    full_spectrum, spectrum_stale = _stamp_full_spectrum(feed, now)

    age_ms = max(0, int((now - float(feed.get("updated_epoch") or 0)) * 1000))
    flag = feed.get("market_open")
    gate = feed_state(
        feed, now=now,
        market_open=flag if isinstance(flag, bool) else True,
    )
    live = bool(feed) and gate.status == "FEED_LIVE"

    spot_block = _fallback_spot(state, symbol)
    # The session writes per-symbol maps; older feeds used a flat dict.
    def _for(value: dict | None) -> dict:
        value = value or {}
        if isinstance(value, dict) and "ltp" in value:
            return value
        return (value.get(symbol) if isinstance(value, dict) else None) or {}

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

    suspended = bool(feed.get("entries_suspended")) or not gate.allow_order_generation
    programmatic = feed.get("programmatic")
    if not isinstance(programmatic, dict) or not programmatic:
        programmatic = build_programmatic_telemetry(feed, symbol=symbol, now=now)
    oscillator = feed.get("model_oscillator")
    if not isinstance(oscillator, dict) or not oscillator:
        oscillator = build_oscillator_from_feed(feed, symbol)
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
    plan = state_block.get("plan") or {}
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
    bull = float(oscillator.get("bullish_upside_prob_pct") or 0.0)
    bear = float(oscillator.get("bearish_downside_prob_pct") or 0.0)
    arbitrator = build_master_arbitrator(
        feed, symbol=symbol, now=now, feed_live=live,
    )
    winner = ((arbitrator.get("sleeve_b") or {}).get("selected")
              or (arbitrator.get("sleeve_a") or {}).get("selected") or {})
    direction = "BULL" if bull >= 58.0 else "BEAR" if bear >= 58.0 else "CHOP"
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
        "source": "MASTER_ARBITRATOR" if winner else "MODEL_FUSION",
        "selected_engine": winner.get("engine"),
        "feed_live": live,
    }
    chart_levels = {
        "entry": trade.get("entry_price") or entry,
        "sl": trade.get("current_trailing_sl") or plan.get("stop_loss"),
        "t1": trade.get("target_1") or plan.get("target1"),
        "t2": trade.get("target_2_p90") or plan.get("target2") or plan.get("p90"),
        "atp": option.get("atp_vwap"),
    }
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
    return {
        "ok": bool(live),
        "ts": round(now, 3),
        "age_ms": age_ms,
        "live": live,
        "feed_status": gate.as_dict(),
        "order_generation_allowed": bool(gate.allow_order_generation and live),
        "entries_suspended": bool(suspended or spectrum_stale),
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
        "candle_visuals": {
            "symbol": symbol,
            "spot": spot_visuals,
            "option": option_visuals,
            "spot_by_tf": spot_by_tf,
            "option_by_tf": option_by_tf,
        },
        "option_chart": {
            "strike_symbol": option.get("strike_symbol"),
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
        "stale": not live,
        "as_of": feed.get("updated_at"),
        "cutoff": feed.get("cutoff") or cutoff_state(),
        "autopilot_status": feed.get("autopilot_status"),
        "autopilot_green": feed.get("autopilot_green"),
        "autopilot": feed.get("autopilot") or {},
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
