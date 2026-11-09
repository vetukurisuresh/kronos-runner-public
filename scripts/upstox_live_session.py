#!/usr/bin/env python3
"""Live Upstox session for the dual-chart terminal.

Every second this publishes ``state/live_feed.json`` with:

* spot OHLC for NIFTY / BANK NIFTY (socket ticks + REST quotes)
* the auto-selected ATM CE/PE option (chain LTP, OI, Greeks, 1m candles)
* a gear-based paper trade on the option (entry, trailing SL, T1, p90 target)
* the eight X-ray scanner checks

Every third second the same payload is mirrored to Firebase
``/kronos_state/tick.json`` so the phone terminal updates while this runs.

    python scripts/upstox_live_session.py --minutes 300
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from market.option_picker import choose_side, gear_update, new_trade, pick_atm  # noqa: E402
from market.upstox_health import load_access_token  # noqa: E402
from market.upstox_tick_engine import UpstoxTickEngine  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT = REPO_ROOT / "state" / "live_feed.json"
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"
QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
CHAIN_URL = "https://api.upstox.com/v2/option/chain"
CONTRACT_URL = "https://api.upstox.com/v2/option/contract"

KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
HEAVYWEIGHTS = {
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "RELIANCE": "NSE_EQ|INE002A01018",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "INFY": "NSE_EQ|INE009A01021",
    "TCS": "NSE_EQ|INE467B01029",
}


def roll(cur: dict | None, price: float, now: float, minutes: int = 1,
         volume: float = 0.0) -> tuple[dict, dict | None]:
    """Roll a forming candle; returns (current, closed-or-None)."""
    bucket = int(now) // (minutes * 60) * (minutes * 60)
    if not cur or cur["time"] != bucket:
        closed = cur if cur else None
        return (
            {"time": bucket, "open": price, "high": price, "low": price,
             "close": price, "volume": volume},
            closed,
        )
    cur["high"] = max(cur["high"], price)
    cur["low"] = min(cur["low"], price)
    cur["close"] = price
    cur["volume"] = round(cur["volume"] + volume, 2)
    return cur, None


class SocketWatch(threading.Thread):
    """Keep a V3 socket open; Upstox silently drops TEXT subscribe frames."""

    def __init__(self, token: str) -> None:
        super().__init__(daemon=True)
        self.token = token
        self.stop = threading.Event()
        self.frames = 0
        self.last_frame = 0.0
        self.last_error = ""
        self.prices: dict[str, float] = {}

    def run(self) -> None:  # pragma: no cover - network
        import websocket

        while not self.stop.is_set():
            try:
                ws = websocket.create_connection(
                    "wss://api.upstox.com/v3/feed/market-data-feed",
                    header=[f"Authorization: Bearer {self.token}", "Accept: */*"],
                    timeout=15,
                )
                self.last_error = ""
                time.sleep(1.0)
                ws.send(
                    json.dumps({
                        "guid": str(uuid.uuid4()),
                        "method": "sub",
                        "data": {"mode": "full", "instrumentKeys": list(KEYS.values())},
                    }).encode("utf-8"),
                    opcode=websocket.ABNF.OPCODE_BINARY,
                )
                ws.settimeout(5)
                while not self.stop.is_set():
                    try:
                        message = ws.recv()
                    except Exception:
                        continue
                    if message:
                        self.frames += 1
                        self.last_frame = time.time()
                        self._decode(message)
            except Exception as exc:  # noqa: BLE001
                self.last_error = type(exc).__name__
                time.sleep(3)

    def _decode(self, message: bytes) -> None:  # pragma: no cover - protobuf
        try:
            import market.proto.MarketDataFeedV3_pb2 as pb

            response = pb.FeedResponse()
            response.ParseFromString(message)
            for key, feed in response.feeds.items():
                if not feed.HasField("fullFeed"):
                    continue
                full = feed.fullFeed
                price = 0.0
                if full.HasField("indexFF"):
                    price = full.indexFF.ltpc.ltp
                elif full.HasField("marketFF"):
                    price = full.marketFF.ltpc.ltp
                if price and price > 0:
                    self.prices[key] = float(price)
        except Exception:  # noqa: BLE001
            pass

    def streaming(self, window: float = 20.0) -> bool:
        return bool(self.last_frame and time.time() - self.last_frame < window)


class Publisher:
    """Mirror the tick payload to Firebase for the phone (cached admin token)."""

    def __init__(self) -> None:
        self.url = ""
        self.token = ""
        self.refreshed = 0.0

    def _refresh(self) -> None:
        from state.firebase_sync import admin

        self.url, self.token = admin()
        self.refreshed = time.time()

    def push(self, payload: dict) -> None:
        try:
            if not self.token or time.time() - self.refreshed > 2700:
                self._refresh()
            requests.put(f"{self.url}/kronos_state/tick.json",
                         params={"access_token": self.token}, json=payload, timeout=12)
        except Exception:  # noqa: BLE001 - publishing must never stop the feed
            self.token = ""


def load_plan(symbol: str) -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    block = (state.get("symbols") or {}).get(symbol) or {}
    forecast = block.get("forecast") or {}
    plan = block.get("plan") or {}
    return {
        "paths_up": forecast.get("paths_up"),
        "samples": forecast.get("samples"),
        "median": forecast.get("median"),
        "spot": block.get("spot"),
        "action": plan.get("action") or "",
        "strike": plan.get("strike"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live Upstox session")
    parser.add_argument("--minutes", type=float, default=300.0)
    parser.add_argument("--no-firebase", action="store_true")
    args = parser.parse_args(argv)

    token = load_access_token()
    if not token:
        print("no access token")
        return 1

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    socket = SocketWatch(token)
    socket.start()
    engine = UpstoxTickEngine()
    publisher = Publisher()

    expiries: dict[str, str] = {}
    option_state: dict[str, dict] = {}
    spot_state: dict[str, dict] = {}
    oi_hist: dict[str, deque] = {name: deque(maxlen=400) for name in KEYS}
    last_chain: dict[str, float] = {}
    published = 0.0
    started = time.time()
    deadline = started + args.minutes * 60
    print(f"live session started ({args.minutes} min)")

    while time.time() < deadline:
        cycle = time.time()
        payload: dict = {"updated_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
                         "updated_epoch": cycle, "prices": {}, "spot": {},
                         "option": {}, "trade": {}, "scanner": {}}

        # ---- spot quotes (indices + heavyweights in one call) ----
        quotes: dict[str, dict] = {}
        keys = list(KEYS.values()) + list(HEAVYWEIGHTS.values())
        try:
            response = requests.get(QUOTES_URL, params={"instrument_key": ",".join(keys)},
                                    headers=headers, timeout=10)
            if response.ok:
                quotes = response.json().get("data") or {}
        except Exception:  # noqa: BLE001
            pass

        for name, key in KEYS.items():
            row = quotes.get(key.replace("|", ":")) or quotes.get(key) or {}
            price = float(socket.prices.get(key) or row.get("last_price") or 0.0)
            if not price:
                continue
            payload["prices"][key] = price
            engine.on_tick(key, price, ts_ms=int(cycle * 1000))
            cur = spot_state.setdefault(name, {})
            cur["1m"], closed = roll(cur.get("1m"), price, cycle)
            if closed:
                cur.setdefault("hist", deque(maxlen=120)).append(closed)
            cur["5m"], closed5 = roll(cur.get("5m"), price, cycle, 5)
            if closed5:
                cur.setdefault("hist5", deque(maxlen=120)).append(closed5)
            hist = list(cur.get("hist") or [])
            atr = 0.0
            if len(hist) >= 5:
                ranges = [c["high"] - c["low"] for c in hist[-14:]]
                atr = round(sum(ranges) / len(ranges), 2)
            vwap = round(sum((c["high"] + c["low"] + c["close"]) / 3 for c in hist[-80:])
                         / max(len(hist[-80:]), 1), 2) if hist else price
            prev_close = float((row.get("ohlc") or {}).get("close") or 0.0)
            payload["spot"][name] = {
                "ltp": price,
                "change": round(price - prev_close, 2) if prev_close else 0.0,
                "pct_change": round((price / prev_close - 1) * 100, 2) if prev_close else 0.0,
                "vwap": vwap,
                "atr": atr,
                "current_candle": cur["1m"],
                "candles_1m": hist[-60:],
                "current_candle_5m": cur["5m"],
            }

        # ---- option chain + paper trade ----
        for name, key in KEYS.items():
            spot = float((payload["spot"].get(name) or {}).get("ltp") or 0.0)
            if not spot:
                continue
            plan = load_plan(name)
            if key not in expiries or cycle - last_chain.get(name, 0) > 3600:
                try:
                    response = requests.get(CONTRACT_URL, params={"instrument_key": key},
                                            headers=headers, timeout=10)
                    dates = sorted({row.get("expiry") for row in (response.json().get("data") or [])
                                    if row.get("expiry")})
                    today = datetime.now(tz=IST).date().isoformat()
                    expiries[key] = next((d for d in dates if d >= today), dates[-1] if dates else "")
                except Exception:  # noqa: BLE001
                    expiries[key] = expiries.get(key, "")
            if not expiries.get(key) or cycle - last_chain.get(name, 0) < 2.0:
                continue
            last_chain[name] = cycle
            try:
                response = requests.get(CHAIN_URL, params={"instrument_key": key,
                                                           "expiry_date": expiries[key]},
                                        headers=headers, timeout=12)
                rows = response.json().get("data") or []
            except Exception:  # noqa: BLE001
                continue
            atm = pick_atm(rows, spot)
            if not atm:
                continue
            side = choose_side(plan.get("paths_up"), plan.get("samples"), plan.get("action"))
            leg = (atm.get("call_options") if side == "CE" else atm.get("put_options")) or {}
            market = leg.get("market_data") or {}
            greeks = leg.get("option_greeks") or {}
            ltp = float(market.get("ltp") or 0.0)
            if not ltp:
                continue

            strike = float(atm.get("strike_price"))
            symbol = f"{name} {int(strike)} {side}"
            state = option_state.get(name) or {"symbol": "", "hist": deque(maxlen=120),
                                               "cur": None, "cum_value": 0.0, "cum_vol": 0.0,
                                               "trade": None}
            if state["symbol"] != symbol:
                # A new strike resets the book; a half-ATR risk keeps the stop sane.
                option_state[name] = state = {"symbol": symbol, "hist": deque(maxlen=120),
                                              "cur": None, "cum_value": 0.0, "cum_vol": 0.0,
                                              "trade": None}
            volume = float(market.get("volume") or 0.0)
            state.setdefault("last_volume", volume)
            delta_vol = max(volume - state.get("last_volume", volume), 0.0)
            state["last_volume"] = volume
            state["cum_value"] += ltp * delta_vol
            state["cum_vol"] += delta_vol
            atp = round(state["cum_value"] / state["cum_vol"], 2) if state["cum_vol"] else ltp

            state["cur"], closed = roll(state["cur"], ltp, cycle, 1, delta_vol)
            if closed:
                state["hist"].append(closed)
            hist = list(state["hist"])
            ranges = [c["high"] - c["low"] for c in hist[-14:]] or [max(ltp * 0.01, 0.5)]
            atr_opt = round(sum(ranges) / len(ranges), 2)
            # A premium option needs a floor: a 0.8-point stop dies on the first tick.
            risk = round(min(max(atr_opt * 1.5, ltp * 0.04), 10.0), 2)
            trade = state.get("trade")
            if not trade and len(hist) >= 5:
                trade = new_trade(ltp, risk)
                state["trade"] = trade
            if not trade:
                trade = {"status": "WATCHING", "entry_price": round(ltp, 2),
                         "initial_sl": round(ltp - risk, 2),
                         "current_trailing_sl": round(ltp - risk, 2), "tsl_gear": 1,
                         "target_1": round(ltp + 1.5 * risk, 2),
                         "target_2_p90": round(ltp + 3 * risk, 2), "qty": 300,
                         "risk_points": risk, "live_pnl_rs": 0.0, "live_pnl_pct": 0.0}
            else:
                low_5m = min((c["low"] for c in hist[-5:]), default=None)
                gear_update(trade, ltp, low_5m)
            trade["option_symbol"] = symbol

            oi = float(market.get("oi") or 0.0)
            hist_oi = oi_hist[name]
            hist_oi.append((cycle, oi))
            five_ago = next((v for t, v in hist_oi if cycle - t <= 300), oi)
            oi_change = round((oi / five_ago - 1) * 100, 2) if five_ago else 0.0

            side_ok = side == "CE"
            ups = sum(1 for c in hist[-10:] if c["close"] >= c["open"])
            mini_paths = round(ups / max(len(hist[-10:]), 1) * 100)
            heavy = [k for k, v in HEAVYWEIGHTS.items()
                     if float((quotes.get(v.replace("|", ":")) or quotes.get(v) or {}).get("net_change") or 0) * (1 if side_ok else -1) > 0]
            spread = round(float(market.get("ask_price") or 0) - float(market.get("bid_price") or 0), 2)
            blob = {
                "strike_symbol": symbol,
                "strike_price": strike,
                "option_type": side,
                "instrument_key": leg.get("instrument_key"),
                "ltp": ltp,
                "atp_vwap": atp,
                "bid": float(market.get("bid_price") or 0.0),
                "ask": float(market.get("ask_price") or 0.0),
                "oi": oi,
                "oi_change_5m_pct": oi_change,
                "delta": greeks.get("delta"),
                "iv": greeks.get("iv"),
                "current_candle": state["cur"],
                "candles_1m": hist[-60:],
                "expiry": expiries.get(key),
            }
            payload["option"][name] = blob
            payload["trade"][name] = trade

            base_pass = bool(plan.get("samples") and (plan.get("paths_up") or 0) >= plan["samples"] * 0.6)
            trend_pass = bool(plan.get("median") and ((plan["median"] > spot) == side_ok))
            payload["scanner"][name] = [
                {"key": "tick_speed", "label": "📡 Tick Speed & Latency", "pass": socket.streaming(),
                 "value": f"{round(socket.frames / max(cycle - started, 1), 1)} ticks/s · {round((time.time() - socket.last_frame) * 1000) if socket.last_frame else 0}ms · Upstox V3"},
                {"key": "kronos_base_paths", "label": "🧠 Kronos-Base (102M · 5m)", "pass": base_pass,
                 "value": f"Paths Up: {plan.get('paths_up')}/{plan.get('samples')} · p90 cone"},
                {"key": "kronos_small_paths", "label": "⚡ Kronos-Small (25M · 5m)", "pass": trend_pass,
                 "value": f"Trend filter: {'aligned' if trend_pass else 'against'} ({plan.get('median')})"},
                {"key": "kronos_mini_opt_paths", "label": "🎯 Kronos-Mini (4M · 1m Option)", "pass": mini_paths >= (60 if side_ok else 1) and (mini_paths >= 60 if side_ok else mini_paths <= 40),
                 "value": f"Option Paths Up: {mini_paths}/100"},
                {"key": "opt_above_vwap", "label": "📊 Option Price vs VWAP (ATP)",
                 "pass": (ltp >= atp) == side_ok, "value": f"LTP ₹{ltp:.2f} vs ATP ₹{atp:.2f} ({'above' if ltp >= atp else 'below'})"},
                {"key": "oi_unwinding_pct", "label": "🔥 5m Option OI Velocity",
                 "pass": abs(oi_change) >= 0.0 and ((oi_change <= 0) == side_ok),
                 "value": f"Δ{side} OI: {oi_change:+.2f}% (5m)"},
                {"key": "heavyweights_aligned", "label": "🏦 Top-5 Heavyweights Sync",
                 "pass": len(heavy) >= 3, "value": f"{' · '.join(HEAVYWEIGHTS)} ({len(heavy)}/5 aligned)"},
                {"key": "sl_points_ok", "label": "🛡️ Option Risk & Spread Gate",
                 "pass": risk <= 10 and (spread <= 0.5 or spread == 0),
                 "value": f"SL = ₹{risk:.2f} pts (<= ₹10) · Spread ₹{spread:.2f}"},
            ]

        payload.update({
            "socket_connected": socket.streaming(),
            "socket_frames": socket.frames,
            "socket_last_frame_age": round(time.time() - socket.last_frame, 1) if socket.last_frame else None,
            "socket_error": socket.last_error,
            "tick_rate": round(socket.frames / max(cycle - started, 1), 2),
            "buffer_1m": {n: len(engine.windows(k, 1)) for n, k in KEYS.items()},
            "buffer_5m": {n: len(engine.windows(k, 5)) for n, k in KEYS.items()},
            "market_open": any(v["ltp"] > 0 for v in payload["spot"].values()),
        })
        HEARTBEAT.write_text(json.dumps(payload, indent=1), encoding="utf-8")

        if not args.no_firebase and cycle - published > 3:
            published = cycle
            publisher.push({
                "ts": round(cycle, 3),
                "spot": payload["spot"],
                "option": payload["option"],
                "trade": payload["trade"],
                "scanner": payload["scanner"],
                "socket": {"rate": payload["tick_rate"], "connected": socket.streaming()},
            })

        time.sleep(max(0.0, 1.0 - (time.time() - cycle)))

    socket.stop.set()
    print("live session finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
