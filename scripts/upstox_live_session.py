#!/usr/bin/env python3
"""Live Upstox session for the dual-chart terminal.

Every second this publishes ``state/live_feed.json`` with:

* spot OHLC for NIFTY / BANK NIFTY (socket ticks + REST quotes)
* the auto-selected ATM CE/PE option (chain LTP, OI, Greeks, 1m candles)
* a gear-based paper trade on the option (entry, trailing SL, T1, p90 target)
* the eight X-ray scanner checks

Every third second the same payload is mirrored to Firebase
``/kronos_state/tick.json`` so the phone terminal updates while this runs.

With ``--edge`` (used by the GitHub runner) it also hosts ``dashboard/server.py``
on 127.0.0.1:8080 and a Cloudflare quick tunnel, then publishes the public URL
to Firebase ``/system/live_stream_url.json`` so the static Pages terminal can
find the runner without a laptop keeping a tunnel alive.

    python scripts/upstox_live_session.py --minutes 300
    python scripts/upstox_live_session.py --minutes 200 --edge
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

from execution.pro_money_manager import (  # noqa: E402
    book_partials,
    close_remaining,
    init_partial_state,
)
from market.option_picker import choose_side, gear_update, new_trade, pick_atm  # noqa: E402
from market.upstox_health import load_access_token  # noqa: E402
from market.upstox_tick_engine import (  # noqa: E402
    UpstoxTickEngine,
    reconnect_forever,
    retry,
)
from state.telugu_story import StoryTeller  # noqa: E402

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
        self.reconnect_gap = 0.0
        self.prices: dict[str, float] = {}

    def _connect(self):  # pragma: no cover - network
        import websocket

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
        return ws

    def _consume(self, ws) -> None:  # pragma: no cover - network
        import websocket

        ws.settimeout(5)
        while not self.stop.is_set():
            try:
                message = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not message:
                raise ConnectionError("Upstox feed closed")
            self.frames += 1
            self.last_frame = time.time()
            self._decode(message)

    def _on_drop(self, exc: Exception) -> None:
        self.last_error = type(exc).__name__

    def _on_reconnect(self) -> None:
        now = time.time()
        self.reconnect_gap = max(0.0, now - self.last_frame) if self.last_frame else 0.0
        print(f"✅ Upstox feed restored after {self.reconnect_gap:.0f}s gap", flush=True)

    def take_gap(self) -> float:
        gap, self.reconnect_gap = self.reconnect_gap, 0.0
        return gap

    def run(self) -> None:  # pragma: no cover - network
        reconnect_forever(
            self._connect,
            self._consume,
            self.stop,
            on_drop=self._on_drop,
            on_reconnect=self._on_reconnect,
        )

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

    def put(self, path: str, body: dict, timeout: float = 12.0) -> int:
        """PUT any path; returns the HTTP status (0 when publishing failed)."""
        try:
            if not self.token or time.time() - self.refreshed > 2700:
                self._refresh()
            response = requests.put(f"{self.url}{path}", params={"access_token": self.token},
                                    json=body, timeout=timeout)
            return response.status_code
        except Exception:  # noqa: BLE001 - publishing must never stop the feed
            self.token = ""
            return 0

    def push(self, payload: dict) -> None:
        self.put("/kronos_state/tick.json", payload)


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


CANDLE_URL = "https://api.upstox.com/v2/historical-candle/intraday/{key}/1minute"


@retry(tries=3, delay=1.5)
def _get_json(url: str, *, params: dict, headers: dict, timeout: float) -> dict:
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


@retry(tries=3, delay=1.5)
def _fetch_candle_rows(instrument_key: str, headers: dict) -> list:
    response = requests.get(CANDLE_URL.format(key=instrument_key), headers=headers, timeout=12)
    response.raise_for_status()
    return ((response.json().get("data") or {}).get("candles") or [])


def fetch_candles(instrument_key: str, headers: dict, limit: int = 60) -> list[dict]:
    """Today's 1-minute candles, oldest first, so the option pane opens full."""
    if not instrument_key:
        return []
    try:
        rows = _fetch_candle_rows(instrument_key, headers)
    except Exception:  # noqa: BLE001 - the retry already used its three attempts
        return []
    out: list[dict] = []
    for row in reversed(rows):
        try:
            out.append({"time": row[0], "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])})
        except (IndexError, TypeError, ValueError):
            continue
    return out[-limit:]


def strike_step(rows: list[dict]) -> float:
    strikes = sorted({float(r.get("strike_price") or 0) for r in rows if r.get("strike_price")})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else 50.0


HUD_FILE = REPO_ROOT / "state" / "live_hud.json"
DEFAULT_CAPITAL = 100_000.0


def empty_hud() -> dict:
    return {
        "date": datetime.now(tz=IST).date().isoformat(),
        "starting_capital": DEFAULT_CAPITAL,
        "realized_today": 0.0,
        "capital": DEFAULT_CAPITAL,
        "trades_today": 0,
        "t1_bookings": 0,
        "t2_bookings": 0,
    }


def load_hud() -> dict:
    """Today's realised ledger survives a session restart; a new day starts flat."""
    today = datetime.now(tz=IST).date().isoformat()
    try:
        raw = json.loads(HUD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_hud()
    if raw.get("date") != today:
        return empty_hud()
    hud = empty_hud()
    hud.update({k: raw.get(k, hud[k]) for k in hud})
    hud["date"] = today
    hud["capital"] = round(float(hud["starting_capital"]) + float(hud["realized_today"]), 2)
    return hud


def save_hud(hud: dict) -> None:
    try:
        HUD_FILE.parent.mkdir(parents=True, exist_ok=True)
        HUD_FILE.write_text(json.dumps(hud, indent=1), encoding="utf-8")
    except OSError:
        pass


def add_hud_leg(hud: dict, leg: dict) -> None:
    """Roll a booked T1/T2/SL leg into the day HUD."""
    hud["realized_today"] = round(float(hud.get("realized_today") or 0.0)
                                  + float(leg.get("pnl") or 0.0), 2)
    hud["capital"] = round(float(hud.get("starting_capital") or DEFAULT_CAPITAL)
                           + hud["realized_today"], 2)
    if leg.get("leg") == "T1":
        hud["t1_bookings"] = int(hud.get("t1_bookings") or 0) + 1
    elif leg.get("leg") == "T2":
        hud["t2_bookings"] = int(hud.get("t2_bookings") or 0) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live Upstox session")
    parser.add_argument("--minutes", type=float, default=300.0)
    parser.add_argument("--no-firebase", action="store_true")
    parser.add_argument("--edge", action="store_true",
                        help="host the SSE server + a Cloudflare quick tunnel (GitHub runner)")
    parser.add_argument("--edge-port", type=int, default=8080)
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
    story = StoryTeller(limit=20)
    hud = load_hud()
    story_started = False
    story_update_at = 0.0
    story_gear: dict[str, int] = {}
    story_stop: dict[str, float] = {}
    edge = None
    if args.edge:
        from market.cloud_edge import CloudEdge

        edge = CloudEdge(publisher, port=args.edge_port)
        print(f"edge bridge: {edge.start()}", flush=True)

    expiries: dict[str, str] = {}
    option_state: dict[str, dict] = {}
    spot_state: dict[str, dict] = {}
    oi_hist: dict[str, deque] = {name: deque(maxlen=400) for name in KEYS}
    last_chain: dict[str, float] = {}
    # The chain is refetched every ~2s; without these caches the payload (and
    # the browser pane) would blink empty on every cycle in between.
    last_option: dict[str, dict] = {}
    last_trade: dict[str, dict] = {}
    last_scanner: dict[str, list] = {}
    published = 0.0

    def _bar_epoch(value) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return 0

    def _merge_backfill(state: dict, bars: list[dict]) -> None:
        """Merge REST 1m high/low into the live 1m and 5m buckets."""
        for bar in bars:
            try:
                stamp = _bar_epoch(bar.get("time"))
                high = float(bar["high"])
                low = float(bar["low"])
                close = float(bar["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if stamp <= 0:
                continue
            for bucket_name in ("1m", "cur"):
                bucket = state.get(bucket_name)
                if not bucket or int(bucket.get("time") or 0) != stamp:
                    continue
                bucket["high"] = max(float(bucket.get("high") or high), high)
                bucket["low"] = min(float(bucket.get("low") or low), low)
                bucket["close"] = close
            five = state.get("5m")
            if five:
                start = int(five.get("time") or 0)
                if start <= stamp < start + 300:
                    five["high"] = max(float(five.get("high") or high), high)
                    five["low"] = min(float(five.get("low") or low), low)
                    five["close"] = close

    def backfill_missed(gap: float) -> None:
        """A >5 s socket gap gets repaired from the 1-minute REST candles."""
        print(f"⚠️ Feed gap {gap:.0f}s - backfilling 1m REST high/low", flush=True)
        for name, key in KEYS.items():
            bars = fetch_candles(key, headers, limit=15)
            if bars:
                _merge_backfill(spot_state.get(name) or {}, bars)
        for state in option_state.values():
            key = state.get("instrument_key")
            if not key:
                continue
            bars = fetch_candles(key, headers, limit=15)
            if bars:
                _merge_backfill(state, bars)
    started = time.time()
    deadline = started + args.minutes * 60
    print(f"live session started ({args.minutes} min)")

    while time.time() < deadline:
        cycle = time.time()
        gap = socket.take_gap()
        if gap >= 5.0:
            backfill_missed(gap)
        payload: dict = {"updated_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
                         "updated_epoch": cycle, "prices": {}, "spot": {},
                         "option": dict(last_option), "trade": dict(last_trade),
                         "scanner": dict(last_scanner)}

        # ---- spot quotes (indices + heavyweights in one call) ----
        quotes: dict[str, dict] = {}
        keys = list(KEYS.values()) + list(HEAVYWEIGHTS.values())
        try:
            quotes = _get_json(QUOTES_URL, params={"instrument_key": ",".join(keys)},
                               headers=headers, timeout=10).get("data") or {}
        except Exception:  # noqa: BLE001 - the retry already used its three attempts
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
                    payload_contract = _get_json(CONTRACT_URL, params={"instrument_key": key},
                                                 headers=headers, timeout=10)
                    dates = sorted({row.get("expiry") for row in (payload_contract.get("data") or [])
                                    if row.get("expiry")})
                    today = datetime.now(tz=IST).date().isoformat()
                    expiries[key] = next((d for d in dates if d >= today), dates[-1] if dates else "")
                except Exception:  # noqa: BLE001
                    expiries[key] = expiries.get(key, "")
            if not expiries.get(key) or cycle - last_chain.get(name, 0) < 2.0:
                continue
            last_chain[name] = cycle
            try:
                rows = _get_json(CHAIN_URL, params={"instrument_key": key,
                                                    "expiry_date": expiries[key]},
                                 headers=headers, timeout=12).get("data") or []
            except Exception:  # noqa: BLE001 - the retry already used its three attempts
                continue
            atm = pick_atm(rows, spot)
            if not atm:
                continue
            side = choose_side(plan.get("paths_up"), plan.get("samples"), plan.get("action"))
            # Stay on the current strike until spot walks a full step away; re-picking
            # on every 50-point wobble wiped the option's candle history.
            prev = option_state.get(name) or {}
            step = float(prev.get("step") or 0.0)
            if prev.get("strike") and step and abs(spot - float(prev["strike"])) <= step:
                kept = next((row for row in rows
                             if float(row.get("strike_price") or 0) == float(prev["strike"])), None)
                if kept:
                    atm = kept
                    side = prev.get("side") or side
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
                                              "trade": None, "strike": strike, "side": side,
                                              "step": strike_step(rows)}
                state["hist"].extend(fetch_candles(leg.get("instrument_key"), headers))
            state["instrument_key"] = leg.get("instrument_key") or ""
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
            legs: list[dict] = []
            ranges = [c["high"] - c["low"] for c in hist[-14:]] or [max(ltp * 0.01, 0.5)]
            atr_opt = round(sum(ranges) / len(ranges), 2)
            # A premium option needs a floor: a 0.8-point stop dies on the first tick.
            risk = round(min(max(atr_opt * 1.5, ltp * 0.04), 10.0), 2)
            trade = state.get("trade")
            if not trade and len(hist) >= 5:
                trade = new_trade(ltp, risk)
                init_partial_state(trade)
                state["trade"] = trade
                if name == "NIFTY":
                    hud["trades_today"] = int(hud.get("trades_today") or 0) + 1
                    save_hud(hud)
            if not trade:
                trade = {"status": "WATCHING", "entry_price": round(ltp, 2),
                         "initial_sl": round(ltp - risk, 2),
                         "current_trailing_sl": round(ltp - risk, 2), "tsl_gear": 1,
                         "target_1": round(ltp + 1.5 * risk, 2),
                         "target_2_p90": round(ltp + 3 * risk, 2), "qty": 300,
                         "risk_points": risk, "live_pnl_rs": 0.0, "live_pnl_pct": 0.0}
                init_partial_state(trade)
            else:
                low_5m = min((c["low"] for c in hist[-5:]), default=None)
                legs = book_partials(trade, ltp)
                if str(trade.get("status") or "").upper() == "OPEN":
                    gear_update(trade, ltp, low_5m)
                    if (str(trade.get("status") or "").upper() == "CLOSED"
                            and int(trade.get("qty_open") or 0) > 0):
                        stop_leg = close_remaining(
                            trade,
                            float(trade.get("current_trailing_sl") or ltp),
                            reason="SL",
                        )
                        if stop_leg:
                            legs.append(stop_leg)
                if name == "NIFTY" and legs:
                    for leg in legs:
                        add_hud_leg(hud, leg)
                    save_hud(hud)
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

            # ---- Story-Mode Telugu narration (NIFTY is the narrative desk) ----
            if name == "NIFTY":
                now_story = time.time()
                five_min = list((spot_state.get("NIFTY") or {}).get("hist5") or [])
                day_high = max((c["high"] for c in five_min), default=spot)
                day_low = min((c["low"] for c in five_min), default=spot)
                support = min((c["low"] for c in five_min[-12:]), default=spot)
                resistance = max((c["high"] for c in five_min[-12:]), default=spot)
                ranges5 = [c["high"] - c["low"] for c in five_min[-14:]]
                atr5 = round(sum(ranges5) / len(ranges5), 2) if ranges5 else 0.0
                if not story_started:
                    story.opening(spot=spot, support=support, resistance=resistance,
                                  paths_up=plan.get("paths_up"), samples=plan.get("samples"),
                                  side=side)
                    story_started = True
                    story_update_at = now_story
                elif now_story - story_update_at > 300:
                    if trade.get("status") == "OPEN":
                        story.market_update(spot=spot, high=day_high, low=day_low, atr=atr5,
                                            paths_up=plan.get("paths_up"),
                                            samples=plan.get("samples"),
                                            pnl=trade.get("live_pnl_rs", 0.0), side=side)
                    else:
                        story.waiting(spot=spot, support=support, resistance=resistance,
                                      reason="KRONOS ఇంకా కన్ఫర్మేషన్ ఇవ్వలేదు",
                                      paths_up=plan.get("paths_up"),
                                      samples=plan.get("samples"), side=side)
                    story_update_at = now_story
                for leg in legs:
                    if leg.get("leg") in ("T1", "T2"):
                        story.partial_booked(
                            name=str(leg.get("leg")),
                            qty=int(leg.get("qty") or 0),
                            price=float(leg.get("price") or 0.0),
                            pnl=float(leg.get("pnl") or 0.0),
                            remaining=int(leg.get("remaining") or 0),
                            stop=float(leg.get("stop") or 0.0),
                        )
                if trade.get("status") == "OPEN" and not state.get("story_trade"):
                    story.trade_taken(symbol=symbol, entry=ltp,
                                      stop=trade.get("current_trailing_sl", 0.0),
                                      target1=trade.get("target_1", 0.0),
                                      target2=trade.get("target_2_p90", 0.0),
                                      risk=trade.get("risk_points", 0.0),
                                      paths_up=plan.get("paths_up"),
                                      samples=plan.get("samples"), heavy=len(heavy), side=side,
                                      qty=int(trade.get("qty") or 0))
                    state["story_trade"] = True
                    story_gear[name] = int(trade.get("tsl_gear") or 1)
                    story_stop[name] = float(trade.get("current_trailing_sl") or 0.0)
                if trade.get("status") == "OPEN":
                    gear_now = int(trade.get("tsl_gear") or 1)
                    stop_now = float(trade.get("current_trailing_sl") or 0.0)
                    if name in story_gear and gear_now != story_gear[name]:
                        story.gear_moved(gear=gear_now,
                                         old_stop=story_stop.get(name, stop_now),
                                         new_stop=stop_now, ltp=ltp,
                                         pnl=trade.get("live_pnl_rs", 0.0))
                    story_gear[name] = gear_now
                    story_stop[name] = stop_now
                elif trade.get("status") == "CLOSED":
                    hits = state.setdefault("story_hits", set())
                    # A T2 winner must not be narrated as a stop-out.
                    if "sl" not in hits and trade.get("exit_reason") != "T2":
                        story.stop_hit(price=float(trade.get("exit_price") or ltp),
                                       pnl=float(trade.get("realized_pnl_rs") or 0.0))
                        hits.add("sl")

        payload.update({
            "socket_connected": socket.streaming(),
            "socket_frames": socket.frames,
            "socket_last_frame_age": round(time.time() - socket.last_frame, 1) if socket.last_frame else None,
            "socket_error": socket.last_error,
            "tick_rate": round(socket.frames / max(cycle - started, 1), 2),
            "buffer_1m": {n: len(engine.windows(k, 1)) for n, k in KEYS.items()},
            "buffer_5m": {n: len(engine.windows(k, 5)) for n, k in KEYS.items()},
            "market_open": any(v["ltp"] > 0 for v in payload["spot"].values()),
            "story": story.snapshot(),
            "story_at": round(time.time(), 3),
            "hud": hud,
        })
        last_option.update(payload["option"])
        last_trade.update(payload["trade"])
        last_scanner.update(payload["scanner"])
        HEARTBEAT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        if edge:
            edge.tick()

        if not args.no_firebase and cycle - published > 3:
            published = cycle
            publisher.push({
                "ts": round(cycle, 3),
                "spot": payload["spot"],
                "option": payload["option"],
                "trade": payload["trade"],
                "scanner": payload["scanner"],
                "story": payload["story"],
                "hud": hud,
                "socket": {"rate": payload["tick_rate"], "connected": socket.streaming()},
            })

        time.sleep(max(0.0, 1.0 - (time.time() - cycle)))

    socket.stop.set()
    story.session_summary(spot=float((payload["spot"].get("NIFTY") or {}).get("ltp") or 0.0),
                          pnl=float((payload["trade"].get("NIFTY") or {}).get("live_pnl_rs") or 0.0),
                          trades=1 if (payload["trade"].get("NIFTY") or {}).get("entry_price") else 0,
                          high=max((payload["spot"].get("NIFTY") or {}).get("ltp") or 0.0,
                                   float((payload["spot"].get("NIFTY") or {}).get("ltp") or 0.0)),
                          low=float((payload["spot"].get("NIFTY") or {}).get("ltp") or 0.0))
    if edge:
        edge.stop()
    print("live session finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
