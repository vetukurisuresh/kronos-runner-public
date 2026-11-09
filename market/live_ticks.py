#!/usr/bin/env python3
"""Real-time tick payload for the dual-chart terminal.

The live session (``scripts/upstox_live_session.py``) writes the authoritative
snapshot to ``state/live_feed.json`` every second. This module normalises it for
the browser and fills the sub-second gaps with a *bounded micro-tick* so the
forming candle moves at 60 FPS between real updates.

Micro-ticks are explicitly flagged (``synthetic: true``) and can never drift more
than ``MICRO_BAND`` from the last real price; they are rendering sugar, not data.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FEED_FILE = REPO_ROOT / "state" / "live_feed.json"
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"

INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
MICRO_BAND = 0.00025  # 2.5 paise per 100 points: invisible, never a trend

_micro: dict[str, dict[str, float]] = {}


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


def micro_tick(symbol: str, price: float, now: float | None = None) -> tuple[float, bool]:
    """Bounded oscillation around the last real price (render only)."""
    now = now or time.time()
    state = _micro.setdefault(symbol, {"phase": 0.0, "last": price or 0.0})
    if price <= 0:
        return price, False
    state["last"] = price
    state["phase"] += 0.9
    # Two detuned sines give an organic, non-repeating wobble.
    wobble = 0.55 * math.sin(state["phase"]) + 0.45 * math.sin(state["phase"] * 2.37 + 1.1)
    return round(price * (1.0 + wobble * MICRO_BAND), 2), True


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
        "countdown_sec": countdown(),
        "source": "snapshot",
    }


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

    age_ms = int((now - float(feed.get("updated_epoch") or 0)) * 1000)
    live = bool(feed) and age_ms < 120_000

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
        micro_price, synthetic = micro_tick(symbol, price, now)
        # Between 1 Hz updates the display shows the micro-tick, but the candle
        # close always snaps back to the last real trade.
        display = micro_price if age_ms > 900 else price
        current = (spot_meta.get("current_candle") or {})
        if current:
            current = dict(current)
            current["close"] = price
        spot_block.update(
            {
                "symbol": symbol,
                "ltp": price,
                "display": display,
                "synthetic": synthetic and display != price,
                "change": spot_meta.get("change", 0.0),
                "pct_change": spot_meta.get("pct_change", 0.0),
                "vwap": spot_meta.get("vwap") or spot_block["vwap"],
                "atr": spot_meta.get("atr", 0.0),
                "current_candle": current or spot_block["current_candle"],
                "candles_1m": spot_meta.get("candles_1m") or [],
                "countdown_sec": countdown(now),
                "source": "upstox-v3" if feed.get("socket_connected") else "upstox-rest",
            }
        )

    option = dict(option)
    if option:
        option.setdefault("countdown_sec", countdown(now))
        option["zone"] = _zone(float(option.get("ltp") or 0), float(option.get("atp_vwap") or 0))

    return {
        "ok": bool(live or spot_block["ltp"]),
        "ts": round(now, 3),
        "age_ms": age_ms,
        "live": live,
        "socket": {
            "connected": bool(feed.get("socket_connected")),
            "tick_rate": feed.get("tick_rate", 0.0),
            "frames": feed.get("socket_frames", 0),
            "latency_ms": round(float(feed.get("socket_last_frame_age") or 0) * 1000),
        },
        "spot": spot_block,
        "option": option or None,
        "active_trade": trade or None,
        "scanner_xray": scanner,
        "story": feed.get("story") or [],
        "story_at": feed.get("story_at"),
        "stale": not live,
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
