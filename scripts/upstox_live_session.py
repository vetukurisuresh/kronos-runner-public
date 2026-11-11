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
import os
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

from execution.capital_sleeve_router import CapitalSleeveRouter  # noqa: E402
from execution.house_money_compounder import HOUSE_MONEY_FILE, HouseMoneyCompounder  # noqa: E402
from execution.order_router import MANUAL_CLOSE_REASON, ModeRejected, OrderRouter  # noqa: E402
from execution.scalp_sleeve_ledger import ScalpSleeveLedger  # noqa: E402
from execution.trading_cutoff import (  # noqa: E402
    SQUARE_OFF_REASON,
    cutoff_state,
)
from execution.pro_money_manager import (  # noqa: E402
    HERO_GUARD_FILE,
    HeroTradeDesk,
    SniperRiskGuard,
    close_remaining,
    expectancy_stats,
    init_partial_state,
    manage_hero_trade,
    manage_post_t2_super_trend,
)
from market.option_picker import choose_side, gear_update, new_trade, pick_atm  # noqa: E402
from market.autopilot_readiness import (  # noqa: E402
    build_checks,
    publish_readiness,
    write_readiness,
)
from market.upstox_health import load_access_token, load_token_meta, verify_token  # noqa: E402
from market.upstox_tick_engine import (  # noqa: E402
    UpstoxTickEngine,
    fetch_official_backfill,
    fetch_upstox_candles,
    reconnect_forever,
    retry,
)
from market.upstox_stream import Candle  # noqa: E402
from market.data_sanitizer import feed_state, is_market_open  # noqa: E402
from market.programmatic_telemetry import ProgrammaticTelemetry  # noqa: E402
from state.telugu_narration_logger import ProgrammaticBrainLogger  # noqa: E402
from strategy.model_oscillator import build_oscillator_from_feed  # noqa: E402
from market.upstox_full_decoder import (  # noqa: E402
    FullSpectrumBook,
    decode_frame,
    option_window_keys,
    select_front_month_futures,
    select_option_window,
)
from model.kronos_inputs import (  # noqa: E402
    build_option_model_input,
    build_spot_model_input,
    tensor_rows,
)
from strategy.full_spectrum_alpha import (  # noqa: E402
    build_snapshot as build_full_spectrum,
    greeks_gate,
    structure_gaps,
    zero_chop_gate,
)
from strategy.adaptive_scalper import (  # noqa: E402
    brain_banner,
    classify_regime,
    decision_snapshot,
    manage_scalp_trade,
    normalize_scalper_mode,
    scalp_signal,
)
from strategy.continuous_scalp_engine import (  # noqa: E402
    BANKNIFTY_LOT_SIZE,
    NIFTY_LOT_SIZE,
    detect_scalp_flip,
    evaluate_continuous_scalp,
    init_scalp_trade,
    manage_continuous_scalp,
    select_scalp_contract,
)
from strategy.expiry_zero_to_hero import (  # noqa: E402
    expiry_context,
    expiry_window,
    hero_scan_snapshot,
    preemptive_trigger,
    rank_hero_candidates,
)
from strategy.greek_target_mapper import map_greek_targets  # noqa: E402
from strategy.mtf_confluence import analyze as analyze_mtf, ema as ema_value  # noqa: E402
from strategy.parallel_alpha_engines import (  # noqa: E402
    SCALP_SLEEVE,
    SNIPER_SLEEVE,
    build_parallel_alpha,
    coiled_spring_trap,
    dual_index_rotator,
    heavyweight_tsunami,
    lead_lag_snipe,
    parity_dislocation_snipe,
)
from strategy.smc_engine import SmcStateMachine  # noqa: E402
from strategy.reversal_sniper import (  # noqa: E402
    detect_bottom_reversal,
    detect_top_reversal,
    sniper_state,
)
from strategy.smart_order_pricer import book_imbalance, smart_limit_price  # noqa: E402
from state.paper_trade_ledger import (  # noqa: E402
    MissedSetup,
    PaperLedger,
    paper_trade_from_live,
    today_ist,
)
from state.telugu_story import StoryTeller  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT = REPO_ROOT / "state" / "live_feed.json"
EXPIRY_HERO_MODE_FILE = REPO_ROOT / "state" / "expiry_hero_mode.json"
SCALPER_MODE_FILE = REPO_ROOT / "state" / "scalper_mode.json"
HERO_DESK_FILE = REPO_ROOT / HERO_GUARD_FILE
SCALPER_STATE_FILE = REPO_ROOT / HOUSE_MONEY_FILE
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"
QUOTES_URL = "https://api.upstox.com/v2/market-quote/quotes"
CHAIN_URL = "https://api.upstox.com/v2/option/chain"
CONTRACT_URL = "https://api.upstox.com/v2/option/contract"

KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}
INDIA_VIX = "NSE_INDEX|India VIX"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
HEAVYWEIGHTS = {
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "RELIANCE": "NSE_EQ|INE002A01018",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "INFY": "NSE_EQ|INE009A01021",
    "TCS": "NSE_EQ|INE467B01029",
}


def load_nse_instruments(timeout: float = 15.0) -> list[dict]:
    """Read the official NSE instruments master (gzip JSON)."""
    import gzip

    response = requests.get(INSTRUMENTS_URL, timeout=timeout,
                            headers={"User-Agent": "kronos-institutional"})
    response.raise_for_status()
    raw = response.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, list) else list((payload or {}).values())


def resolve_futures(token: str) -> dict[str, dict]:
    """Resolve NIFTY/BANKNIFTY front-month futures from the Upstox master."""
    del token  # The public master does not require the access token.
    try:
        rows = load_nse_instruments()
    except Exception as exc:  # noqa: BLE001 - the desk remains usable without it
        print(f"futures master unavailable: {type(exc).__name__}", flush=True)
        return {}
    return {
        name: row
        for name in KEYS
        if (row := select_front_month_futures(rows, name))
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

    def __init__(self, token: str, initial_keys: list[str] | None = None) -> None:
        super().__init__(daemon=True)
        self.token = token
        self.stop = threading.Event()
        self.frames = 0
        self.last_frame = 0.0
        self.bytes_total = 0
        self.last_ltt_ms = 0.0
        self.last_error = ""
        self.reconnect_gap = 0.0
        self.prices: dict[str, float] = {}
        self.ticks: dict[str, dict] = {}
        self.tick_queue: list[tuple[str, dict]] = []
        self.extra_keys: set[str] = set(initial_keys or [])
        self.books = FullSpectrumBook()
        self.ws = None
        self.lock = threading.Lock()

    def _connect(self):  # pragma: no cover - network
        import websocket

        ws = websocket.create_connection(
            "wss://api.upstox.com/v3/feed/market-data-feed",
            header=[f"Authorization: Bearer {self.token}", "Accept: */*"],
            timeout=15,
        )
        self.last_error = ""
        time.sleep(1.0)
        self.ws = ws
        ws.send(
            json.dumps({
                "guid": str(uuid.uuid4()),
                "method": "sub",
                "data": {"mode": "full",
                         "instrumentKeys": list(dict.fromkeys(list(KEYS.values()) + list(self.extra_keys)))},
            }).encode("utf-8"),
            opcode=websocket.ABNF.OPCODE_BINARY,
        )
        return ws

    def subscribe(self, keys: list[str]) -> None:
        """Subscribe the ATM option without dropping the index feed."""
        fresh = [key for key in keys if key and key not in self.extra_keys]
        if not fresh:
            return
        self.extra_keys.update(fresh)
        ws = self.ws
        if ws is None:
            return
        try:
            import websocket
            ws.send(
                json.dumps({"guid": str(uuid.uuid4()), "method": "sub",
                            "data": {"mode": "full", "instrumentKeys": fresh}}).encode("utf-8"),
                opcode=websocket.ABNF.OPCODE_BINARY,
            )
        except Exception:  # noqa: BLE001 - reconnect re-subscribes from extra_keys
            pass

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
            self.bytes_total += len(message)
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
            frame = decode_frame(message)
            for key, tick in frame.ticks:
                try:
                    self.last_ltt_ms = max(self.last_ltt_ms, float(tick.get("ts_ms") or 0.0))
                except (TypeError, ValueError):
                    pass
                price = float(tick.get("price") or 0.0)
                if price <= 0:
                    continue
                self.books.update(key, tick)
                with self.lock:
                    self.prices[key] = price
                    self.ticks[key] = dict(tick)
                    self.tick_queue.append((key, dict(tick)))
        except Exception:  # noqa: BLE001
            pass

    def take_ticks(self) -> list[tuple[str, dict]]:
        with self.lock:
            rows, self.tick_queue = self.tick_queue, []
        return rows

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
        "p10": forecast.get("p10"),
        "p50": forecast.get("median"),
        "p65": forecast.get("p65"),
        "p90": forecast.get("p90"),
        "dispersion": forecast.get("dispersion"),
        "regime": forecast.get("regime") or "",
        "spot": block.get("spot"),
        "action": plan.get("action") or "",
        "strike": plan.get("strike"),
        "traffic": plan.get("traffic") or "",
        "watch_entry": plan.get("watch_entry"),
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


def ui_bars(candles: list) -> list[dict]:
    """Render engine candles with the browser's short OHLC keys."""
    return [
        {"t": candle.start, "o": round(candle.open, 2), "h": round(candle.high, 2),
         "l": round(candle.low, 2), "c": round(candle.close, 2),
         "v": round(candle.volume, 2), "a": round(getattr(candle, "amount", 0.0), 2)}
        for candle in candles
    ]


def bar_range(bar: dict) -> float | None:
    """High-low range for either long-key REST bars or short-key UI bars."""
    if not isinstance(bar, dict):
        return None
    high = bar.get("high", bar.get("h"))
    low = bar.get("low", bar.get("l"))
    try:
        high_f, low_f = float(high), float(low)
    except (TypeError, ValueError):
        return None
    if high_f <= 0 or low_f <= 0 or high_f < low_f:
        return None
    return high_f - low_f


def fused_ui_bars(
    engine: UpstoxTickEngine, key: str, minutes: int, futures_key: str = ""
) -> list[dict]:
    """Keep broker OHLC, replace index flow with front-month futures flow."""
    bars = ui_bars(engine.windows(key, minutes))
    if not futures_key or not bars:
        return bars
    funded = build_spot_model_input(
        engine.windows(key, minutes), engine.windows(futures_key, minutes), limit=512
    )
    flow = {int(row["time"]): row for row in funded}
    for bar in bars:
        row = flow.get(int(bar["t"]))
        if row:
            bar["v"] = row["volume"]
            bar["a"] = row["amount"]
    return bars


def p25(candles: list) -> float:
    """25th-percentile low from the official 1m option buffer."""
    lows = sorted(candle.low for candle in candles if candle.low > 0)
    if not lows:
        return 0.0
    return round(lows[max(0, int(len(lows) * 0.25) - 1)], 2)


def warm_engine(
    engine: UpstoxTickEngine, token: str, extra: dict[str, str] | None = None
) -> dict:
    """Seed all three official intervals before the first socket tick."""
    counts: dict[str, dict[str, int]] = {}
    targets = dict(KEYS)
    targets.update(extra or {})
    for name, key in targets.items():
        counts[name] = {}
        for minutes in (1, 5, 15):
            try:
                bars = fetch_official_backfill(
                    key, minutes, access_token=token, days=30, timeout=15,
                )
                counts[name][f"{minutes}m"] = engine.seed(key, minutes, bars)
            except Exception as exc:  # noqa: BLE001 - a REST outage is logged, never invented
                counts[name][f"{minutes}m"] = 0
                print(f"warmup {name} {minutes}m unavailable: {type(exc).__name__}", flush=True)
    print(f"official warmup: {counts}", flush=True)
    return counts


def verify_parity(engine: UpstoxTickEngine, key: str, token: str) -> dict:
    """Compare the latest sealed 1m bar with Upstox's official intraday bar."""
    try:
        bars = fetch_upstox_candles(
            key, 1, access_token=token, intraday=True, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": type(exc).__name__}
    if not bars:
        return {"ok": False, "reason": "no official 1m bar"}
    latest = bars[-1]
    ours = next((bar for bar in engine.windows(key, 1) if bar.start == latest.start), None)
    if ours is None:
        return {"ok": False, "reason": "bar not sealed yet", "start": latest.start}
    match = all(
        abs(float(a) - float(b)) < 0.005
        for a, b in ((ours.open, latest.open), (ours.high, latest.high),
                     (ours.low, latest.low), (ours.close, latest.close))
    )
    if not match:
        engine.reconcile(key, 1, latest)
    return {
        "ok": True, "match": match, "start": latest.start,
        "ours": [ours.open, ours.high, ours.low, ours.close],
        "upstox": [latest.open, latest.high, latest.low, latest.close],
        "checked_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
    }


def strike_step(rows: list[dict]) -> float:
    strikes = sorted({float(r.get("strike_price") or 0) for r in rows if r.get("strike_price")})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else 50.0


HUD_FILE = REPO_ROOT / "state" / "live_hud.json"
RISK_FILE = REPO_ROOT / "state" / "sniper_risk_guard.json"
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
        "profit_vault": 0.0,
        "vault_swept_today": 0.0,
        "tradable_capital": DEFAULT_CAPITAL,
        "sniper_trades": 0,
        "daily_lock": False,
        "sniper_stats": {},
        "expectancy": {},
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
    hud["profit_vault"] = round(float(hud.get("profit_vault") or 0.0), 2)
    hud["tradable_capital"] = round(hud["capital"] - hud["profit_vault"], 2)
    return hud


def save_hud(hud: dict) -> None:
    try:
        HUD_FILE.parent.mkdir(parents=True, exist_ok=True)
        HUD_FILE.write_text(json.dumps(hud, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_expiry_hero_mode() -> str:
    """Read the UI override; AUTO keeps the exchange contract date in charge."""
    try:
        raw = json.loads(EXPIRY_HERO_MODE_FILE.read_text(encoding="utf-8"))
        mode = str(raw.get("mode") or "AUTO").strip().upper().replace("-", "_").replace(" ", "_")
        return "FORCE_ON" if mode in {"ON", "FORCE", "FORCEON", "FORCE_ON", "TRUE"} else (
            "OFF" if mode in {"OFF", "FALSE", "0"} else "AUTO"
        )
    except (OSError, ValueError, TypeError):
        return "AUTO"


def load_scalper_mode() -> str:
    """Read the UI scalper mode, defaulting to the all-weather auto brain."""
    try:
        raw = json.loads(SCALPER_MODE_FILE.read_text(encoding="utf-8"))
        return normalize_scalper_mode(raw.get("mode"))
    except (OSError, ValueError, TypeError):
        return "AUTO"


def add_scalp_hud_leg(hud: dict, leg: dict) -> None:
    """Bank a fast-scalp leg; the 50% vault sweep happens once at closure."""
    pnl = float(leg.get("pnl") or 0.0)
    hud["realized_today"] = round(float(hud.get("realized_today") or 0.0) + pnl, 2)
    hud["capital"] = round(
        float(hud.get("starting_capital") or DEFAULT_CAPITAL) + hud["realized_today"], 2
    )
    hud["scalp_booked_legs"] = int(hud.get("scalp_booked_legs") or 0) + 1
    if str(leg.get("leg") or "") == "SCALP_S1":
        hud["scalp_s1_books"] = int(hud.get("scalp_s1_books") or 0) + 1
    hud["tradable_capital"] = round(
        hud["capital"] - float(hud.get("profit_vault") or 0.0), 2
    )


def settle_scalp_trade(
    hud: dict,
    house_money: HouseMoneyCompounder,
    trade: dict,
    now: datetime,
    state_file: Path | str = SCALPER_STATE_FILE,
) -> dict:
    """Settle one closed scalp and roll the vault, pool and next-size meters."""
    total_pnl = float(trade.get("realized_pnl_rs") or 0.0)
    settled = house_money.register_trade(total_pnl, now)
    hud["scalper_trades"] = house_money.trades
    hud["scalper_vault"] = house_money.vault
    hud["house_money_pool"] = house_money.house_money_pool
    hud["scalper_realized"] = house_money.realized
    hud["scalper_next_qty"] = house_money.next_qty
    hud["scalper_status"] = house_money.status(now)
    hud["scalper_cooldown"] = house_money.cooldown_remaining(now)
    hud["scalper_last_pnl"] = round(total_pnl, 2)
    house_money.save(state_file)
    return settled


def add_hero_hud_leg(hud: dict, leg: dict) -> None:
    """Roll a Hero leg into the HUD without the sniper's 50% vault sweep.

    The Hero desk sweeps 60% of the completed trade once, at peak exit.  Adding
    each leg through ``add_hud_leg`` would otherwise mix in the sniper's 50%
    sweep and understate the Hero vault.
    """
    pnl = float(leg.get("pnl") or 0.0)
    hud["realized_today"] = round(float(hud.get("realized_today") or 0.0) + pnl, 2)
    hud["capital"] = round(
        float(hud.get("starting_capital") or DEFAULT_CAPITAL) + hud["realized_today"], 2
    )
    hud["hero_booked_legs"] = int(hud.get("hero_booked_legs") or 0) + 1
    if str(leg.get("leg") or "").startswith("HERO_2X"):
        hud["hero_2x_locks"] = int(hud.get("hero_2x_locks") or 0) + 1
    elif str(leg.get("leg") or "").startswith("HERO_P90"):
        hud["hero_p90_books"] = int(hud.get("hero_p90_books") or 0) + 1
    hud["tradable_capital"] = round(
        hud["capital"] - float(hud.get("profit_vault") or 0.0), 2
    )


def add_hud_leg(hud: dict, leg: dict, guard: SniperRiskGuard | None = None) -> None:
    """Roll a booked T1/T2/SL leg into the day HUD and lock 50% of profit."""
    pnl = float(leg.get("pnl") or 0.0)
    hud["realized_today"] = round(float(hud.get("realized_today") or 0.0) + pnl, 2)
    hud["capital"] = round(float(hud.get("starting_capital") or DEFAULT_CAPITAL)
                           + hud["realized_today"], 2)
    if guard is not None:
        swept = guard.register_leg(leg)
        hud["profit_vault"] = float(swept.get("profit_vault") or 0.0)
        hud["sniper_stats"] = guard.stats()
        guard.save(RISK_FILE)
    elif pnl > 0:
        sweep = round(pnl * 0.50, 2)
        hud["profit_vault"] = round(float(hud.get("profit_vault") or 0.0) + sweep, 2)
        hud["vault_swept_today"] = round(float(hud.get("vault_swept_today") or 0.0) + sweep, 2)
    hud["tradable_capital"] = round(hud["capital"] - float(hud.get("profit_vault") or 0.0), 2)
    leg_name = str(leg.get("leg") or "")
    if leg_name == "T1" or leg_name.startswith("SUPER_T1"):
        hud["t1_bookings"] = int(hud.get("t1_bookings") or 0) + 1
    elif leg_name == "T2" or leg_name.startswith("SUPER_T2"):
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
    futures = resolve_futures(token)
    futures_keys = {
        name: str(row.get("instrument_key") or "")
        for name, row in futures.items()
        if row.get("instrument_key")
    }
    initial_keys = list(dict.fromkeys(
        [INDIA_VIX]
        + list(HEAVYWEIGHTS.values())
        + [key for key in futures_keys.values() if key]
    ))
    socket = SocketWatch(token, initial_keys=initial_keys)
    socket.start()
    engine = UpstoxTickEngine()
    engine.register(INDIA_VIX, "INDIA VIX", "index")
    for name, key in futures_keys.items():
        engine.register(key, f"{name} FUT", "future")
    router = OrderRouter()
    warmup = warm_engine(
        engine, token, {f"{name} FUT": key for name, key in futures_keys.items()}
    )
    token_meta = load_token_meta()
    auth_probe = verify_token(token)
    auth_verified = bool(auth_probe.get("ok"))
    auth_minted_at = str(
        token_meta.get("verified_at")
        or token_meta.get("saved_at")
        or datetime.now(tz=IST).isoformat(timespec="seconds")
    )
    print(
        f"morning auth: token_present=True verified={auth_verified} "
        f"profile_http={auth_probe.get('status')}",
        flush=True,
    )
    parity: dict[str, dict] = {}
    parity_at = 0.0
    last_vtt: dict[str, float] = {}
    close_handled = ""
    publisher = Publisher()
    story = StoryTeller(limit=20)
    hud = load_hud()
    guard = SniperRiskGuard.load(RISK_FILE, today_ist())
    hero_desk = HeroTradeDesk.load(HERO_DESK_FILE, today_ist())
    hero_last: dict[str, dict] = {}
    hero_window_entries: dict[str, int] = {}
    house_money = HouseMoneyCompounder.load(SCALPER_STATE_FILE, today_ist())
    scalp_sleeve = ScalpSleeveLedger().ensure_day(today_ist())
    capital_router = CapitalSleeveRouter()
    continuous_decision: dict[str, dict] = {}
    continuous_last: dict[str, dict] = {}
    continuous_cooldown_until: dict[str, datetime] = {}
    # Five parallel alpha engines share the same 250ms-fed quotes.  Only the
    # session owns capital; the engines publish deterministic order intents and
    # the radar snapshot, never synthetic fills.
    parallel_runtime: dict[str, dict] = {}
    parallel_option_hist: dict[str, deque] = {name: deque(maxlen=20) for name in KEYS}
    parallel_heavy_hist: dict[str, deque] = {name: deque(maxlen=20) for name in HEAVYWEIGHTS}
    parallel_fut_hist: dict[str, deque] = {name: deque(maxlen=20) for name in KEYS}
    parallel_oi_hist: dict[str, deque] = {name: deque(maxlen=20) for name in KEYS}
    parallel_straddle_low: dict[str, float] = {}
    parallel_prev_bull = 0
    parallel_prev_bear = 0
    parallel_prev_winner = ""
    scalper_last: dict[str, dict] = {}
    scalper_brain: dict[str, str] = {}
    scalper_pending: dict[str, str] = {}
    scalper_decision: dict[str, dict] = {}
    hud["scalper_trades"] = house_money.trades
    hud["scalper_vault"] = house_money.vault
    hud["house_money_pool"] = house_money.house_money_pool
    hud["scalper_next_qty"] = house_money.next_qty
    hud["scalper_status"] = house_money.status()
    hud["sniper_trades"] = guard.trades
    hud["daily_lock"] = guard.daily_lock
    hud["hero_trades"] = hero_desk.trades
    hud["hero_vault"] = hero_desk.profit_vault
    hud["profit_vault"] = round(max(float(hud.get("profit_vault") or 0.0), guard.profit_vault), 2)
    hud["tradable_capital"] = round(float(hud.get("capital") or DEFAULT_CAPITAL)
                                   - hud["profit_vault"], 2)
    hud["sniper_stats"] = guard.stats()
    ledger = PaperLedger.load()
    pending_setups: dict[str, dict] = {}
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
    sniper_pending: dict[str, str] = {}
    sniper_live: dict[str, dict] = {}
    spot_state: dict[str, dict] = {}
    oi_hist: dict[str, deque] = {name: deque(maxlen=400) for name in KEYS}
    chain_cache: dict[str, list[dict]] = {name: [] for name in KEYS}
    chain_oi_hist: dict[str, dict[str, deque]] = {name: {} for name in KEYS}
    basis_hist: dict[str, deque] = {name: deque(maxlen=400) for name in KEYS}
    vix_hist: deque = deque(maxlen=400)
    last_chain: dict[str, float] = {}
    # The chain is refetched every ~2s; without these caches the payload (and
    # the browser pane) would blink empty on every cycle in between.
    last_option: dict[str, dict] = {}
    last_trade: dict[str, dict] = {}
    last_scanner: dict[str, list] = {}
    published = 0.0
    readiness_published = 0.0
    ready_at_ist = ""
    close_request_path = REPO_ROOT / "state" / "live_close_request.json"
    close_ack_path = REPO_ROOT / "state" / "live_close_ack.json"

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

    def enrich_chain(name: str, rows: list[dict], now: float) -> list[dict]:
        """Overlay the full socket frame onto each CE/PE leg of the 11-strike window."""
        out: list[dict] = []
        histories = chain_oi_hist.setdefault(name, {})
        for raw in rows:
            row = dict(raw)
            for side_name in ("call_options", "put_options"):
                leg = dict(row.get(side_name) or {})
                key = str(leg.get("instrument_key") or "")
                tick = socket.ticks.get(key) or {}
                market = dict(leg.get("market_data") or {})
                greeks = dict(leg.get("option_greeks") or {})
                if tick:
                    market.update({
                        "ltp": tick.get("price"), "oi": tick.get("oi"),
                        "volume": tick.get("vtt"), "bid_price": tick.get("bid"),
                        "ask_price": tick.get("ask"),
                    })
                    greeks.update({
                        "delta": tick.get("delta"), "theta": tick.get("theta"),
                        "gamma": tick.get("gamma"), "vega": tick.get("vega"),
                        "rho": tick.get("rho"), "iv": tick.get("iv"),
                    })
                oi = float(market.get("oi") or 0.0)
                if key and oi > 0:
                    series = histories.setdefault(key, deque(maxlen=400))
                    series.append((now, oi))
                    reference = next((v for t, v in series if now - t <= 300), oi)
                    market["oi_change"] = round(oi - reference, 2)
                leg["market_data"] = market
                leg["option_greeks"] = greeks
                row[side_name] = leg
            out.append(row)
        return out
    started = time.time()
    deadline = started + args.minutes * 60
    telemetry = ProgrammaticTelemetry()
    brain_narrator = ProgrammaticBrainLogger()
    seen_frames = 0
    seen_bytes = 0
    latest_scan_ms: dict[str, float] = {}
    print(f"live session started ({args.minutes} min)")

    while time.time() < deadline:
        cycle = time.time()
        now_ist_dt = datetime.now(tz=IST)
        hero_mode = load_expiry_hero_mode()
        hero_window = expiry_window(now_ist_dt)
        scalper_mode = load_scalper_mode()
        cutoff = cutoff_state()
        socket_age_ms = (int(max(0.0, cycle - socket.last_frame) * 1000.0)
                         if socket.last_frame else None)
        feed_stale = socket_age_ms is None or socket_age_ms > 3000
        gap = socket.take_gap()
        if gap >= 5.0:
            backfill_missed(gap)
        payload: dict = {"updated_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
                         "updated_epoch": cycle, "prices": {}, "spot": {},
                         "option": dict(last_option), "trade": dict(last_trade),
                         "scanner": dict(last_scanner), "mtf": {}, "smart_entry": {},
                         "sniper": {}, "full_spectrum": {}, "option_chain": {},
                         "expiry_hero": {}, "scalper": {}, "kronos_inputs": {},
                         "parallel_alpha": {},
                         "parity": parity, "cutoff": cutoff, "trade_mode": router.snapshot(
                             paper_capital=ledger.capital,
                             paper_realized=float(hud.get("realized_today") or 0.0),
                         )}
        close_request: dict = {}
        try:
            close_request = json.loads(close_request_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            close_request = {}

        # Exchange ltt is the only candle clock. No local-clock tick is injected
        # while the socket is streaming, so the browser cannot drift from Upstox.
        new_frames = max(0, socket.frames - seen_frames)
        new_bytes = max(0, socket.bytes_total - seen_bytes)
        seen_frames, seen_bytes = socket.frames, socket.bytes_total
        if new_frames:
            sanitize_now = float(engine.sanitizer.snapshot().get("sanitize_latency_us") or 0.0)
            for _ in range(min(new_frames, 128)):
                telemetry.record_tick(
                    server_epoch_ms=cycle * 1000.0,
                    ltt_ms=socket.last_ltt_ms or None,
                    payload_bytes=max(1, round(new_bytes / new_frames)),
                    sanitize_us=sanitize_now,
                )

        for tick_key, tick in socket.take_ticks():
            try:
                price = float(tick.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            vtt = float(tick.get("vtt") or 0.0)
            previous = last_vtt.get(tick_key)
            last_vtt[tick_key] = vtt if vtt else previous
            delta = max(vtt - previous, 0.0) if previous is not None and vtt else 0.0
            fields = {
                key: value for key, value in tick.items()
                if key not in {"price", "ltp", "volume", "ts_ms"}
            }
            amount = delta * float(tick.get("atp") or price)
            engine.ingest_tick(
                tick_key, price, volume=delta, amount=amount,
                ts_ms=tick.get("ts_ms"), **fields,
            )

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
            socket_price = float(socket.prices.get(key) or 0.0)
            rest_price = float(row.get("last_price") or 0.0)
            price = socket_price or rest_price
            if not price:
                continue
            payload["prices"][key] = price
            # When the socket is healthy, the engine already consumed the exact
            # ltt tick above. Only a disconnected feed may use the REST quote.
            if not socket.streaming():
                engine.on_tick(key, price, ts_ms=int(cycle * 1000))
            quote = engine.instruments.get(key).quote if key in engine.instruments else None
            price = float(quote.ltp if quote and quote.ltp else price)
            cur = spot_state.setdefault(name, {})
            cur["1m"] = engine.current(key, 1) or cur.get("1m")
            cur["5m"] = engine.current(key, 5) or cur.get("5m")
            cur["15m"] = engine.current(key, 15) or cur.get("15m")
            fut_key = futures_keys.get(name, "")
            bars_1m = fused_ui_bars(engine, key, 1, fut_key)
            bars_5m = fused_ui_bars(engine, key, 5, fut_key)
            bars_15m = fused_ui_bars(engine, key, 15, fut_key)
            model_1m = build_spot_model_input(
                engine.windows(key, 1), engine.windows(fut_key, 1) if fut_key else None,
                limit=512,
            )
            model_5m = build_spot_model_input(
                engine.windows(key, 5), engine.windows(fut_key, 5) if fut_key else None,
                limit=512,
            )
            payload["kronos_inputs"][name] = {
                "features": ["open", "high", "low", "close", "volume", "amount"],
                "shape_1m": [len(model_1m), 6],
                "shape_5m": [len(model_5m), 6],
                "last_1m": tensor_rows(model_1m)[-2:],
                "last_5m": tensor_rows(model_5m)[-2:],
                "volume_source": "FUT" if fut_key else "COMPOSITE/UNAVAILABLE",
            }
            hist = [{"time": bar["t"], "open": bar["o"], "high": bar["h"],
                     "low": bar["l"], "close": bar["c"], "volume": bar["v"]}
                    for bar in bars_1m]
            atr = 0.0
            if len(hist) >= 5:
                ranges = [c["high"] - c["low"] for c in hist[-14:]]
                atr = round(sum(ranges) / len(ranges), 2)
            vwap = round(sum((c["high"] + c["low"] + c["close"]) / 3 for c in hist[-80:])
                         / max(len(hist[-80:]), 1), 2) if hist else price
            prev_close = float((row.get("ohlc") or {}).get("close") or 0.0)
            plan_now = load_plan(name)
            mtf_now = analyze_mtf(
                engine.windows(key, 1), engine.windows(key, 5), engine.windows(key, 15),
                paths_up=plan_now.get("paths_up"), samples=plan_now.get("samples"),
            )
            payload["mtf"][name] = mtf_now
            payload["spot"][name] = {
                "ltp": price,
                "change": round(price - prev_close, 2) if prev_close else 0.0,
                "pct_change": round((price / prev_close - 1) * 100, 2) if prev_close else 0.0,
                "vwap": vwap,
                "atr": atr,
                "current_candle": cur["1m"],
                "current_candle_5m": cur["5m"],
                "current_candle_15m": cur["15m"],
                "candles_1m": bars_1m[-512:],
                "candles_5m": bars_5m[-512:],
                "candles_15m": bars_15m[-512:],
                "mtf": mtf_now,
                "volume_source": "FUT" if fut_key else "COMPOSITE/UNAVAILABLE",
                "native_ohlc": (engine.instruments.get(key).market_ohlc
                                if key in engine.instruments else {}),
            }
            smc_snapshot = SmcStateMachine().update(
                candles_1m=engine.windows(key, 1),
                candles_5m=engine.windows(key, 5),
                candles_15m=engine.windows(key, 15),
                futures_1m=engine.windows(fut_key, 5) if fut_key else [],
                p10=plan_now.get("p10"), p25=plan_now.get("p25"),
                spot=price, option_atp=0.0, obi_5=0.0,
                feed_live=not feed_stale,
            )
            payload.setdefault("smc", {})[name] = smc_snapshot.as_dict()

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
            expiry_ctx = expiry_context(
                name, expiries.get(key), today=now_ist_dt.date(), mode=hero_mode
            )
            # The raw chain feed does not carry a 5m OI delta.  Use the last
            # enriched window so the chart can switch to the Hero strike before
            # this cycle's fresh overlay lands; the overlay re-scores below.
            hero_scan = rank_hero_candidates(
                chain_cache.get(name) or rows, spot, name, now_ist_dt,
                window=hero_window,
                kronos_move=max(45.0, abs(float(plan.get("p90") or spot) - spot)),
                paths_up=plan.get("paths_up"), samples=plan.get("samples"),
            )
            atm = pick_atm(rows, spot)
            if not atm:
                continue
            side = choose_side(plan.get("paths_up"), plan.get("samples"), plan.get("action"))
            # Stay on the current strike until spot walks a full step away; re-picking
            # on every 50-point wobble wiped the option's candle history.
            prev = option_state.get(name) or {}
            # An open scalp pins its strike and side; the ATM/Hero picker must
            # never wander to another book and orphan a funded position.
            pinned_trade = prev.get("trade")
            scalp_pin_open = (isinstance(pinned_trade, dict)
                              and bool(pinned_trade.get("scalp_mode"))
                              and str(pinned_trade.get("status") or "").upper() == "OPEN")
            if scalp_pin_open:
                pinned_row = next(
                    (row for row in rows
                     if float(row.get("strike_price") or 0)
                     == float(prev.get("strike") or 0)),
                    None,
                )
                if pinned_row:
                    atm = pinned_row
                    side = str(prev.get("side") or side)
            step = float(prev.get("step") or 0.0)
            if prev.get("strike") and step and abs(spot - float(prev["strike"])) <= step:
                kept = next((row for row in rows
                             if float(row.get("strike_price") or 0) == float(prev["strike"])), None)
                if kept:
                    atm = kept
                    side = prev.get("side") or side
            # A confirmed reversal arms the opposite leg for the next cycle;
            # never fight the old side while the new option book is being seeded.
            pending_side = "" if scalp_pin_open else sniper_pending.pop(name, None)
            if pending_side:
                side = pending_side
            # A ping-pong scalp on the other leg re-arms the chart for the next
            # cycle; never switch away from an option book that is still traded.
            prior_trade = (option_state.get(name) or {}).get("trade")
            prior_open = (isinstance(prior_trade, dict)
                          and str(prior_trade.get("status") or "").upper() == "OPEN")
            scalp_pending_side = "" if prior_open else scalper_pending.pop(name, None)
            if scalp_pending_side:
                side = scalp_pending_side
            # The #1 Hero candidate owns the right option chart.  The strike is
            # re-checked after every 2s chain refresh, so the pane follows the
            # gamma convexity instead of a stale ATM strike.
            hero_enabled = bool(
                expiry_ctx.get("enabled")
                and (hero_window.get("allowed") or hero_window.get("extreme_only"))
            )
            hero_pick = (hero_scan.get("hero") or {}) if hero_enabled else {}
            if hero_pick and not scalp_pin_open:
                picked_row = next(
                    (row for row in rows
                     if float(row.get("strike_price") or 0) == float(hero_pick.get("strike") or 0)),
                    None,
                )
                if picked_row:
                    atm = picked_row
                    side = str(hero_pick.get("side") or side)
            # Subscribe the entire ATM +/- 5 window (11 CE + 11 PE), not only
            # the one leg that happens to be traded.  The overlay below uses
            # the same full frames for OI, depth and Greeks.
            window = select_option_window(rows, float(atm.get("strike_price") or 0), 5)
            if window:
                window_keys = option_window_keys(window)
                if window_keys:
                    socket.subscribe(window_keys)
                enriched = enrich_chain(name, window, cycle)
                chain_cache[name] = enriched
                hero_scan = rank_hero_candidates(
                    enriched, spot, name, now_ist_dt,
                    window=hero_window,
                    kronos_move=max(45.0, abs(float(plan.get("p90") or spot) - spot)),
                    paths_up=plan.get("paths_up"), samples=plan.get("samples"),
                )
                enhanced = next((row for row in enriched
                                 if float(row.get("strike_price") or 0)
                                 == float(atm.get("strike_price") or 0)), None)
                if enhanced:
                    atm = enhanced
            else:
                chain_cache[name] = []
            leg = (atm.get("call_options") if side == "CE" else atm.get("put_options")) or {}
            market = leg.get("market_data") or {}
            greeks = leg.get("option_greeks") or {}
            ltp = float(market.get("ltp") or 0.0)
            if not ltp:
                continue

            # The lagging option's own 3-second move tells the lead-lag engine
            # whether the sister index has already repriced.  Keep this in the
            # session so it uses the exact ticks the chain just consumed.
            parallel_opt_hist = parallel_option_hist.setdefault(name, deque(maxlen=20))
            parallel_opt_hist.append((cycle, ltp))
            parallel_opt_base = next(
                (value for stamp, value in parallel_opt_hist if cycle - stamp <= 3.0),
                ltp,
            )
            parallel_option_move_3s = ltp - parallel_opt_base

            strike = float(atm.get("strike_price"))
            symbol = f"{name} {int(strike)} {side}"
            state = option_state.get(name) or {"symbol": "", "hist": deque(maxlen=120),
                                               "cur": None, "cum_value": 0.0, "cum_vol": 0.0,
                                               "trade": None}
            instrument_key = leg.get("instrument_key") or ""
            if state["symbol"] != symbol:
                # A new strike resets the book; official 1m/5m/15m bars are seeded
                # before the first tick so the option chart also has broker parity.
                option_state[name] = state = {"symbol": symbol, "hist": deque(maxlen=512),
                                              "cur": None, "cum_value": 0.0, "cum_vol": 0.0,
                                              "trade": None, "strike": strike, "side": side,
                                              "step": strike_step(rows)}
                seeded = False
                if instrument_key:
                    for minutes in (1, 5, 15):
                        try:
                            bars = fetch_official_backfill(
                                instrument_key, minutes, access_token=token, days=30,
                                timeout=15,
                            )
                            engine.seed(instrument_key, minutes, bars)
                            seeded = True
                        except Exception:  # noqa: BLE001 - chain fallback is still official
                            pass
                if instrument_key:
                    socket.subscribe([instrument_key])
                if seeded:
                    state["hist"].extend(ui_bars(engine.windows(instrument_key, 1))[-120:])
                else:
                    state["hist"].extend(fetch_candles(instrument_key, headers))
            state["instrument_key"] = instrument_key
            if instrument_key:
                socket.subscribe([instrument_key])
            volume = float(market.get("volume") or 0.0)
            state.setdefault("last_volume", volume)
            delta_vol = max(volume - state.get("last_volume", volume), 0.0)
            state["last_volume"] = volume
            state["cum_value"] += ltp * delta_vol
            state["cum_vol"] += delta_vol
            atp = round(state["cum_value"] / state["cum_vol"], 2) if state["cum_vol"] else ltp

            if instrument_key and socket.streaming():
                state["cur"] = engine.current(instrument_key, 1) or state.get("cur")
                official = ui_bars(engine.windows(instrument_key, 1))
                if official:
                    state["hist"] = deque(official[-512:], maxlen=512)
            else:
                state["cur"], closed = roll(state["cur"], ltp, cycle, 1, delta_vol)
                if closed:
                    state["hist"].append(closed)
            hist = list(state["hist"])
            legs: list[dict] = []
            ranges = [value for bar in hist[-14:] if (value := bar_range(bar)) is not None]
            ranges = ranges or [max(ltp * 0.01, 0.5)]
            atr_opt = round(sum(ranges) / len(ranges), 2)
            # The Top-5% desk keeps every initial stop inside the ₹6–₹9 envelope.
            risk = round(min(max(atr_opt * 1.5, ltp * 0.04, 6.0), 9.0), 2)
            trade = state.get("trade")
            if (isinstance(trade, dict) and str(trade.get("status") or "").upper() == "CLOSED"
                    and state.get("reentry_armed")):
                state["trade"] = None
                state["reentry_armed"] = False
                trade = None
            # The 15:18 windup closes a scalp exactly once and settles it into
            # the house-money desk, not the sniper guard.
            if (isinstance(trade, dict) and str(trade.get("status") or "").upper() == "OPEN"
                    and trade.get("scalp_mode") and cutoff["square_off_due"]):
                square_at = datetime.now(tz=IST).isoformat(timespec="seconds")
                try:
                    if router.load()["mode"] == "REAL":
                        router.close_live_at_market(trade)
                    leg = close_remaining(trade, ltp, reason=SQUARE_OFF_REASON)
                    trade["exit_at"] = square_at
                    if leg:
                        if trade.get("continuous_scalp"):
                            scalp_sleeve.sync_trade(trade, ltp=ltp)
                            scalp_sleeve.finish_trade(trade, now=now_ist_dt)
                        else:
                            add_scalp_hud_leg(hud, leg)
                            settled = settle_scalp_trade(hud, house_money, trade, now_ist_dt)
                            scalper_brain[name] = brain_banner(
                                scalper_decision.get(name) or {},
                                trade_no=house_money.trades,
                                pnl=float(settled.get("pnl") or 0.0),
                            )
                        save_hud(hud)
                    state["trade"] = trade
                    state["ledger_sig"] = None
                    ledger.upsert_trade(paper_trade_from_live(
                        trade, date=today_ist(), symbol=name, light=light,
                    ))
                    ledger.save()
                except Exception as exc:  # noqa: BLE001 - never crash the desk
                    print(f"15:18 scalp square-off failed for {name}: {exc}", flush=True)
                    trade["square_off_error"] = str(exc)
            if (isinstance(trade, dict) and str(trade.get("status") or "").upper() == "OPEN"
                    and cutoff["square_off_due"]):
                # The 15:18 windup is an exit, never a new entry.  A REAL
                # position gets its exchange SELL first; paper books locally.
                square_at = datetime.now(tz=IST).isoformat(timespec="seconds")
                try:
                    if router.load()["mode"] == "REAL":
                        router.close_live_at_market(trade)
                    leg = close_remaining(trade, ltp, reason=SQUARE_OFF_REASON)
                    trade["exit_at"] = square_at
                    if leg:
                        add_hud_leg(hud, leg, guard)
                        save_hud(hud)
                    state["trade"] = trade
                    state["ledger_sig"] = None
                    ledger.upsert_trade(paper_trade_from_live(
                        trade, date=today_ist(), symbol=name, light=light,
                    ))
                    ledger.save()
                except Exception as exc:  # noqa: BLE001 - never crash the desk
                    print(f"15:18 square-off failed for {name}: {exc}", flush=True)
                    trade["square_off_error"] = str(exc)
            option_tick = socket.ticks.get(instrument_key) or {}
            option_iv = float(option_tick.get("iv") or greeks.get("iv") or 0.0)
            option_gamma = float(option_tick.get("gamma") or greeks.get("gamma") or 0.0)
            option_delta = float(option_tick.get("delta") or greeks.get("delta") or 0.0)
            option_theta = float(option_tick.get("theta") or greeks.get("theta") or 0.0)
            option_vega = float(option_tick.get("vega") or greeks.get("vega") or 0.0)
            iv_velocity = socket.books.iv_velocity(instrument_key) if instrument_key else 0.0
            gamma_velocity = socket.books.gamma_velocity(instrument_key) if instrument_key else 0.0
            kronos_move = abs(float(plan.get("median") or spot) - spot)
            option_state_obj = engine.instruments.get(instrument_key)
            option_quote = option_state_obj.quote if option_state_obj else None
            option_depth = book_imbalance(
                option_quote.bids if option_quote else None,
                option_quote.asks if option_quote else None,
            )
            option_obi = (option_depth.get("ratio", 0.0)
                          if (option_depth.get("bid_qty", 0.0)
                              + option_depth.get("ask_qty", 0.0)) > 0 else 0.0)
            option_flow = (float(option_quote.tbq) / float(option_quote.tsq)
                           if option_quote and option_quote.tsq else 0.0)
            greek_gate = greeks_gate(
                delta=option_delta, theta=option_theta, gamma=option_gamma,
                vega=option_vega, iv=option_iv,
                iv_velocity_5m=iv_velocity, gamma_velocity_5m=gamma_velocity,
                kronos_move=kronos_move, obi_5=option_obi, tbq_tsq=option_flow,
            )
            gaps = structure_gaps(
                chain=chain_cache.get(name) or [],
                greeks={"iv": option_iv, "delta": option_delta, "theta": option_theta,
                        "gamma": option_gamma, "vega": option_vega},
                depth={"bids": option_quote.bids if option_quote else [],
                       "asks": option_quote.asks if option_quote else []},
            )
            alpha_allow = bool(greek_gate.get("allow")) and not gaps
            regime_text = str(plan.get("regime") or "").upper()
            neutral_pct = (100.0 if "NEUTRAL" in regime_text or "SIDEWAYS" in regime_text
                           else None)
            chop = zero_chop_gate(
                paths_up=plan.get("paths_up"), samples=float(plan.get("samples") or 0),
                neutral_pct=neutral_pct, obi_5=option_obi, tbq_tsq=option_flow,
            )

            # ---- five-signature pre-emptive reversal sniper ----
            fut_key_now = futures_keys.get(name, "")
            fut_state_now = engine.instruments.get(fut_key_now) if fut_key_now else None
            fut_quote_now = fut_state_now.quote if fut_state_now else None
            fut_ltp_now = float(fut_quote_now.ltp if fut_quote_now else 0.0)
            fut_bars = engine.windows(fut_key_now, 1) if fut_key_now else []
            fut_vols = [float(getattr(bar, "volume", 0.0) or 0.0) for bar in fut_bars[-21:]]
            mean_vol = (sum(fut_vols[:-1]) / len(fut_vols[:-1])
                        if len(fut_vols) > 1 else 0.0)
            vol_ratio = (fut_vols[-1] / mean_vol) if fut_vols and mean_vol > 0 else 0.0
            spot_bars = engine.windows(key, 1)[-4:]
            spot_velocity = (spot_bars[-1].close - spot_bars[-2].close
                             if len(spot_bars) >= 2 else 0.0)
            ranges_spot = [bar.high - bar.low for bar in spot_bars]
            spot_atr = sum(ranges_spot) / len(ranges_spot) if ranges_spot else 0.0
            stall_threshold = max(spot_atr * 0.15, 0.05)
            basis_now = round(fut_ltp_now - spot, 2) if fut_ltp_now else 0.0
            basis_prev = basis_hist[name][-1][1] if basis_hist[name] else None
            # A missing futures feed must never look like a basis collapse.
            top_basis_prev = basis_prev if fut_ltp_now > 0 else None
            ce_leg = atm.get("call_options") or {}
            pe_leg = atm.get("put_options") or {}
            ce_market = ce_leg.get("market_data") or {}
            pe_market = pe_leg.get("market_data") or {}
            ce_key = str(ce_leg.get("instrument_key") or "")
            pe_key = str(pe_leg.get("instrument_key") or "")
            ce_quote = (engine.instruments.get(ce_key).quote
                        if ce_key in engine.instruments else None)
            pe_quote = (engine.instruments.get(pe_key).quote
                        if pe_key in engine.instruments else None)
            ce_depth_now = book_imbalance(ce_quote.bids if ce_quote else None,
                                          ce_quote.asks if ce_quote else None)
            pe_depth_now = book_imbalance(pe_quote.bids if pe_quote else None,
                                          pe_quote.asks if pe_quote else None)

            def _obi(depth):
                return (depth.get("ratio", 0.0)
                        if (depth.get("bid_qty", 0.0) + depth.get("ask_qty", 0.0)) > 0
                        else 0.0)

            ce_obi = _obi(ce_depth_now)
            pe_obi = _obi(pe_depth_now)
            ce_ltp = float(ce_quote.ltp if ce_quote else ce_market.get("ltp") or 0.0)
            pe_ltp = float(pe_quote.ltp if pe_quote else pe_market.get("ltp") or 0.0)
            ce_bars = engine.windows(ce_key, 1) if ce_key else []
            pe_bars = engine.windows(pe_key, 1) if pe_key else []
            if not ce_bars and side == "CE":
                ce_bars = hist
            if not pe_bars and side == "PE":
                pe_bars = hist
            ce_wick_low = min((bar.low for bar in ce_bars[-3:]), default=0.0)
            pe_wick_high = max((bar.high for bar in pe_bars[-3:]), default=0.0)
            # ---- all-weather adaptive scalper: classify and price the next scalp ----
            ce_spread = max(0.0, float(ce_market.get("ask_price") or 0.0)
                            - float(ce_market.get("bid_price") or 0.0))
            pe_spread = max(0.0, float(pe_market.get("ask_price") or 0.0)
                            - float(pe_market.get("bid_price") or 0.0))
            scalp_move = abs(float(plan.get("median") or spot) - spot)
            scalp_expected = round(
                max(atr_opt * 2.0, scalp_move * abs(float(option_delta or 0.0))), 2
            )
            scalp_decision = classify_regime(
                now_ist_dt, symbol=name,
                option_atr_1m=atr_opt,
                futures_volume_ratio=vol_ratio,
                mini_velocity=((float(hist[-1].get("close") or 0.0)
                                - float(hist[-2].get("close") or 0.0))
                               if len(hist) >= 2 else 0.0),
                paths_up=plan.get("paths_up"), samples=plan.get("samples"),
                p10=plan.get("p10"), p90=plan.get("p90"), median=plan.get("median"),
                spot=spot, obi_5=ce_obi if side == "CE" else pe_obi,
                tbq=float(ce_quote.tbq if ce_quote else ce_market.get("tbq") or 0.0)
                    if side == "CE" else float(pe_quote.tbq if pe_quote else pe_market.get("tbq") or 0.0),
                tsq=float(ce_quote.tsq if ce_quote else ce_market.get("tsq") or 0.0)
                    if side == "CE" else float(pe_quote.tsq if pe_quote else pe_market.get("tsq") or 0.0),
                spread=max(ce_spread, pe_spread),
                mode=scalper_mode,
                post_trade=bool(scalper_brain.get(name)),
            )
            scalper_decision[name] = scalp_decision
            scalp_now = scalp_signal(
                scalp_decision,
                spot=spot, p10=plan.get("p10"), p90=plan.get("p90"),
                median=plan.get("median"),
                vwap=float((payload["spot"].get(name) or {}).get("vwap") or 0.0),
                ce_obi_5=ce_obi,
                ce_tbq=float(ce_quote.tbq if ce_quote else ce_market.get("tbq") or 0.0),
                ce_tsq=float(ce_quote.tsq if ce_quote else ce_market.get("tsq") or 0.0),
                pe_obi_5=pe_obi,
                pe_tbq=float(pe_quote.tbq if pe_quote else pe_market.get("tbq") or 0.0),
                pe_tsq=float(pe_quote.tsq if pe_quote else pe_market.get("tsq") or 0.0),
                ce_ltp=ce_ltp, pe_ltp=pe_ltp,
                expected_move_pts=scalp_expected,
                ignition_low=float(hist[-1].get("low") or 0.0) if hist else 0.0,
                now=now_ist_dt,
            )
            scalp_gate = house_money.can_open(
                now_ist_dt,
                sl_points=float(scalp_now.get("sl_points") or 4.0),
                obi_5=ce_obi if scalp_now.get("side") == "CE" else pe_obi,
                futures_volume_ratio=vol_ratio,
            )
            scalp_now["gate"] = scalp_gate
            scalp_now["expected_source"] = {
                "option_atr_1m": round(atr_opt, 2),
                "kronos_spot_move": round(scalp_move, 2),
                "delta": round(float(option_delta or 0.0), 3),
            }
            # Dedicated ₹35k continuous scalp policy. It is independent from the
            # legacy house-money book and only arms while the sniper is waiting.
            macro_open = bool(
                isinstance(trade, dict)
                and str(trade.get("status") or "").upper() == "OPEN"
                and not trade.get("scalp_mode")
            )
            cooldown_left = 0
            cooldown_until = continuous_cooldown_until.get(name)
            if cooldown_until:
                cooldown_left = max(0, int((cooldown_until - now_ist_dt).total_seconds()))
            continuous_now = evaluate_continuous_scalp(
                now_ist_dt,
                symbol=name,
                main_state="IN_TRADE" if macro_open else "WAITING_FOR_PRICE_ZONE",
                spot=spot,
                spot_atr_1m=spot_atr,
                spot_range_1m=spot_atr,
                option_atr_1m=atr_opt,
                spread=(ce_spread if side == "CE" else pe_spread),
                spot_vwap=float((payload["spot"].get(name) or {}).get("vwap") or 0.0),
                spot_ema5=ema_value([
                    float(bar.close) for bar in spot_bars
                ], 5),
                p10=plan.get("p10"),
                p90=plan.get("p90"),
                kronos_slope=(
                    float(hist[-1].get("close") or 0.0) - float(hist[-3].get("close") or 0.0)
                    if len(hist) >= 3 else 0.0
                ),
                side_bias="AUTO",
                ce_ltp=ce_ltp,
                ce_atp=atp if side == "CE" else 0.0,
                pe_ltp=pe_ltp,
                pe_atp=atp if side == "PE" else 0.0,
                ce_obi_5=ce_obi,
                ce_tbq=float(ce_quote.tbq if ce_quote else ce_market.get("tbq") or 0.0),
                ce_tsq=float(ce_quote.tsq if ce_quote else ce_market.get("tsq") or 0.0),
                pe_obi_5=pe_obi,
                pe_tbq=float(pe_quote.tbq if pe_quote else pe_market.get("tbq") or 0.0),
                pe_tsq=float(pe_quote.tsq if pe_quote else pe_market.get("tsq") or 0.0),
                futures_volume_ratio=vol_ratio,
                cooldown_sec=cooldown_left,
                engine_active=bool(scalp_sleeve.active),
            )
            continuous_decision[name] = continuous_now
            bottom = detect_bottom_reversal(
                spot=spot, p10=float(plan.get("p10") or 0.0),
                paths_up=plan.get("paths_up"), samples=plan.get("samples") or 0,
                futures_volume_ratio=vol_ratio, price_velocity=spot_velocity,
                velocity_stall_threshold=stall_threshold,
                obi_5=ce_obi,
                tbq=float(ce_quote.tbq if ce_quote else ce_market.get("tbq") or 0.0),
                tsq=float(ce_quote.tsq if ce_quote else ce_market.get("tsq") or 0.0),
                pe_oi_change_5m=float(pe_market.get("oi_change") or 0.0),
                ce_oi_change_5m=float(ce_market.get("oi_change") or 0.0),
                entry=ce_ltp or ltp, absorption_low=ce_wick_low,
            )
            top = detect_top_reversal(
                spot=spot, p90=float(plan.get("p90") or 0.0),
                basis=basis_now, previous_basis=top_basis_prev,
                ce_iv_change_5m=(socket.books.iv_velocity(ce_key) if ce_key else None),
                ce_tbq=float(ce_quote.tbq if ce_quote else ce_market.get("tbq") or 0.0),
                ce_tsq=float(ce_quote.tsq if ce_quote else ce_market.get("tsq") or 0.0),
                ce_obi_5=ce_obi, mtf=payload["mtf"].get(name),
                pe_obi_5=pe_obi,
                pe_tbq=float(pe_quote.tbq if pe_quote else pe_market.get("tbq") or 0.0),
                pe_tsq=float(pe_quote.tsq if pe_quote else pe_market.get("tsq") or 0.0),
                entry=pe_ltp or ltp, absorption_high=pe_wick_high,
            )
            sniper_now = sniper_state(bottom, top)
            sniper_live[name] = sniper_now
            sniper_ready = bool((bottom["trigger"] and side == "CE")
                                or (top["pe_trigger"] and side == "PE"))
            if bottom["trigger"] and side != "CE":
                sniper_pending[name] = "CE"
            if top["pe_trigger"] and side != "PE":
                sniper_pending[name] = "PE"
            pending_wait = bool(sniper_pending.get(name)) and not sniper_ready

            # ---- Expiry Hero: pre-emptive Gamma Blast trigger ----
            selected_hero = next(
                (candidate for candidate in (hero_scan.get("candidates") or [])
                 if float(candidate.get("strike") or 0) == strike
                 and str(candidate.get("side") or "").upper() == side),
                None,
            )
            hero_vols = [float(bar.get("volume") or 0.0) for bar in hist[-6:]]
            hero_avg_vol = (sum(hero_vols[:-1]) / len(hero_vols[:-1])
                            if len(hero_vols) > 1 else 0.0)
            hero_vol_ratio = (hero_vols[-1] / hero_avg_vol) if hero_avg_vol > 0 else 0.0
            hero_mini_accel = ((float(hist[-1].get("close") or 0.0)
                                - float(hist[-2].get("close") or 0.0))
                               if len(hist) >= 2 else 0.0)
            hero_rolling_high = (max(float(bar.get("high") or 0.0)
                                     for bar in hist[-16:-1])
                                 if len(hist) >= 2 else 0.0)
            hero_ignition_low = float(hist[-1].get("low") or 0.0) if hist else 0.0
            hero_signal = preemptive_trigger(
                selected_hero,
                paths_up=plan.get("paths_up"), samples=plan.get("samples"),
                mini_acceleration=hero_mini_accel,
                atp=atp, rolling_15m_high=hero_rolling_high,
                volume_ratio_1m=hero_vol_ratio,
                obi_5=option_obi,
                tbq=float(option_quote.tbq if option_quote else 0.0),
                tsq=float(option_quote.tsq if option_quote else 0.0),
                futures_volume_ratio=vol_ratio,
                basis=basis_now,
                previous_basis=(top_basis_prev if top_basis_prev is not None else basis_now),
                ignition_low=hero_ignition_low,
                window=hero_window, now=now_ist_dt, entry=ltp,
                kronos_move=max(45.0, abs(float(plan.get("p90") or spot) - spot)),
            )
            hero_can_fire = hero_desk.can_fire(now_ist_dt, allowed=True)
            hero_window_key = str(hero_window.get("window") or "")
            hero_window_cap = int(hero_window.get("max_setups") or 0)
            hero_window_used = hero_window_entries.get(hero_window_key, 0)
            hero_window_ok = hero_window_cap <= 0 or hero_window_used < hero_window_cap
            hero_ready = bool(
                hero_enabled and hero_signal.get("trigger") and hero_can_fire.get("ok")
                and hero_window_ok
            )

            traffic = str(plan.get("traffic") or "").upper()
            plan_action = str(plan.get("action") or "").upper()
            traffic_red = plan_action == "NO_TRADE" or traffic.startswith("RED")
            light = "RED" if traffic_red else ("YELLOW" if traffic.startswith("YELLOW") else "GREEN")

            # ---- p90 top wave: close the CE peak immediately, arm PE ----
            if (isinstance(trade, dict)
                    and str(trade.get("status") or "").upper() == "OPEN"
                    and str(trade.get("option_type") or side).upper() == "CE"
                    and not trade.get("hero_mode")
                    and not trade.get("continuous_scalp")
                    and top["peak_exit"]):
                peak_at = datetime.now(tz=IST).isoformat(timespec="seconds")
                try:
                    leg = close_remaining(trade, ltp, reason="P90_TOP_EXIT")
                    trade["exit_at"] = peak_at
                    state["trade"] = trade
                    state["reentry_armed"] = True
                    sniper_pending[name] = "PE"
                    if leg:
                        add_hud_leg(hud, leg, guard)
                        save_hud(hud)
                        story.top_reversal_exit(
                            price=ltp, pnl=float(leg.get("pnl") or 0.0),
                            p90=float(plan.get("p90") or 0.0),
                        )
                    state["ledger_sig"] = None
                    ledger.upsert_trade(paper_trade_from_live(
                        trade, date=today_ist(), symbol=name, light=light,
                    ))
                    ledger.save()
                except Exception as exc:  # noqa: BLE001 - never crash the desk
                    print(f"p90 top exit failed for {name}: {exc}", flush=True)

            plan_strike = str(plan.get("strike") or symbol)
            setup_id = f"{today_ist()}|{name}|{plan_strike}|{side}"
            now_iso = datetime.now(tz=IST).isoformat(timespec="seconds")
            pending = pending_setups.get(name)
            setup_dirty = False

            # Record what KRONOS identified even when the traffic light blocks
            # execution.  A watching setup is removed the moment it triggers.
            if (not trade and (plan.get("strike") or plan.get("action") or plan.get("traffic"))):
                if pending and pending.get("setup_id") != setup_id:
                    ledger.upsert_missed(MissedSetup(
                        date=today_ist(), time_identified=pending.get("time") or now_iso,
                        symbol=name, strike=pending.get("strike") or plan_strike,
                        option_type=pending.get("side") or side,
                        planned_entry=float(pending.get("planned_entry") or ltp),
                        status="Missed - Market Reversed",
                        reason="Plan or strike changed before trigger",
                        light=pending.get("light") or light,
                        setup_id=pending.get("setup_id") or "", updated_at=now_iso,
                    ))
                    setup_dirty = True
                    pending = None
                if not pending:
                    pending = {"setup_id": setup_id, "time": now_iso,
                               "strike": plan_strike, "side": side,
                               "planned_entry": round(ltp, 2), "status": "Watching",
                               "light": light}
                    pending_setups[name] = pending
                    setup_dirty = True
                status = "Cancelled by Traffic Light" if traffic_red else "Watching"
                ledger.upsert_missed(MissedSetup(
                    date=today_ist(), time_identified=pending["time"], symbol=name,
                    strike=plan_strike, option_type=side,
                    planned_entry=float(pending["planned_entry"]), status=status,
                    reason=traffic or plan_action, light=light, setup_id=setup_id,
                    updated_at=now_iso,
                ))
                if pending.get("status") != status:
                    pending["status"] = status
                    setup_dirty = True

            # Hero entries are their own risk book: the 3/day cap and 5-minute
            # Silent Watch belong to HeroTradeDesk, not the Top-5% sniper guard.
            if (not trade and hero_ready and not traffic_red
                    and cutoff["new_entries_allowed"] and not feed_stale):
                hero_sl = float(hero_signal.get("sl_points") or 0.0)
                trade = new_trade(ltp, hero_sl)
                init_partial_state(trade)
                trade["trade_id"] = uuid.uuid4().hex
                trade["entry_at"] = now_iso
                trade["mfe_price"] = round(ltp, 2)
                trade["mae_price"] = round(ltp, 2)
                trade["option_type"] = side
                trade["sl_points"] = round(hero_sl, 2)
                trade["risk_points"] = round(hero_sl, 2)
                trade["target_1"] = float(hero_signal.get("target_1") or 0.0)
                trade["target_2_p90"] = float(hero_signal.get("target_2_p90") or 0.0)
                trade["t1_label"] = "HERO_2X_LOCK"
                trade["t2_label"] = "HERO_P90_HARVEST"
                trade["hero_mode"] = True
                trade["hero_entry"] = round(ltp, 2)
                trade["hero_score"] = float((selected_hero or {}).get("score_100") or 0.0)
                trade["hero_scan"] = hero_scan_snapshot(hero_scan)
                trade["hero_signal"] = hero_signal
                trade["sniper_grade"] = "HERO"
                state["trade"] = trade
                state["risk_recorded"] = True
                state["story_trade"] = True
                state["ledger_sig"] = None
                hero_desk.register_entry()
                hero_window_entries[hero_window_key] = hero_window_used + 1
                hero_desk.save(HERO_DESK_FILE)
                hero_last[name] = trade
                hud["trades_today"] = int(hud.get("trades_today") or 0) + 1
                hud["hero_trades"] = hero_desk.trades
                hud["hero_vault"] = hero_desk.profit_vault
                save_hud(hud)
                story.hero_entry(
                    symbol=symbol, side=side, entry=ltp,
                    stop=float(trade["current_trailing_sl"]),
                    target1=float(trade["target_1"]),
                    target2=float(trade["target_2_p90"]),
                    window=str(hero_window.get("status") or ""),
                    score=float(trade["hero_score"]),
                )
                if pending:
                    ledger.remove_missed(str(pending.get("setup_id") or ""))
                    pending_setups.pop(name, None)
                    pending = None
                    setup_dirty = True

            # The dedicated continuous sleeve takes the current chain book when
            # all five signatures align. A different side is pinned for the
            # next cycle so the fill can never land on the old chart's leg.
            if (not trade and continuous_now.get("trigger")
                    and cutoff["new_entries_allowed"] and not traffic_red
                    and not feed_stale and not scalp_sleeve.active):
                if continuous_now.get("side") and continuous_now.get("side") != side:
                    scalper_pending[name] = str(continuous_now["side"])
                else:
                    expiry_day = bool(expiry_ctx.get("is_expiry_day"))
                    contract = select_scalp_contract(
                        chain_cache.get(name) or [], side,
                        capital=scalp_sleeve.allocated,
                        lot_size=(BANKNIFTY_LOT_SIZE if name == "BANKNIFTY" else NIFTY_LOT_SIZE),
                        expiry_day=expiry_day,
                    )
                    if contract:
                        contract["symbol"] = name
                    continuous_now["contract"] = dict(contract or {})
                    if contract and str(contract.get("instrument_key") or "") == str(instrument_key or ""):
                        contract["entry"] = round(ltp, 2)
                        contract["deployed"] = round(float(contract.get("qty") or 0) * ltp, 2)
                        trade = init_scalp_trade(contract, now=now_ist_dt)
                        trade["trade_id"] = uuid.uuid4().hex
                        trade["entry_at"] = now_iso
                        trade["mfe_price"] = round(ltp, 2)
                        trade["mae_price"] = round(ltp, 2)
                        trade["instrument_key"] = instrument_key
                        trade["option_symbol"] = symbol
                        trade["sniper_grade"] = "SCALP_35K"
                        trade["t1_label"] = "SCALP_S1_50PCT"
                        trade["t2_label"] = "SCALP_TRAIL_50PCT"
                        trade["scalp_signal"] = dict(continuous_now)
                        trade["scalp_regime"] = "CONTINUOUS_5OF5"
                        trade["scalp_grade"] = "A+"
                        state["trade"] = trade
                        state["risk_recorded"] = True
                        state["story_trade"] = True
                        state["ledger_sig"] = None
                        scalp_sleeve.start_trade(contract, now=now_ist_dt)
                        scalp_sleeve.sync_trade(trade, ltp=ltp)
                        continuous_last[name] = trade
                        hud["continuous_scalp_trades"] = int(hud.get("continuous_scalp_trades") or 0) + 1
                        save_hud(hud)
                        if pending:
                            ledger.remove_missed(str(pending.get("setup_id") or ""))
                            pending_setups.pop(name, None)
                            pending = None
                            setup_dirty = True
                    elif contract:
                        scalper_pending[name] = side

            # The adaptive scalper runs its own 90s / 6-scalp risk book on top
            # of the same chain: base capital first, then house money only.
            if (not trade and not continuous_now.get("trigger") and scalp_now.get("trigger")
                    and cutoff["new_entries_allowed"] and not traffic_red
                    and not feed_stale):
                if scalp_now.get("side") and scalp_now.get("side") != side:
                    # The trigger belongs to the other option leg; switch the
                    # chart next cycle so the fill lands on the right book.
                    scalper_pending[name] = str(scalp_now["side"])
                elif scalp_gate.get("ok"):
                    scalp_obi = ce_obi if side == "CE" else pe_obi
                    scalp_qty = int(scalp_gate.get("qty") or 0)
                    scalp_sl = float(scalp_now.get("sl_points") or 4.0)
                    trade = new_trade(ltp, scalp_sl, qty=scalp_qty)
                    init_partial_state(trade)
                    trade["trade_id"] = uuid.uuid4().hex
                    trade["entry_at"] = now_iso
                    trade["mfe_price"] = round(ltp, 2)
                    trade["mae_price"] = round(ltp, 2)
                    trade["option_type"] = side
                    trade["sl_points"] = round(scalp_sl, 2)
                    trade["risk_points"] = round(scalp_sl, 2)
                    trade["initial_sl"] = float(scalp_now.get("stop") or trade["initial_sl"])
                    trade["current_trailing_sl"] = float(
                        scalp_now.get("stop") or trade["current_trailing_sl"]
                    )
                    trade["target_1"] = float(scalp_now.get("target_1") or 0.0)
                    trade["target_2_p90"] = float(scalp_now.get("target_2") or 0.0)
                    trade["scalp_s1_target"] = float(scalp_now.get("target_1") or 0.0)
                    trade["scalp_s2_target"] = float(scalp_now.get("target_2") or 0.0)
                    trade["t1_label"] = "SCALP_S1_60PCT"
                    trade["t2_label"] = "SCALP_S2_40PCT"
                    trade["scalp_mode"] = True
                    trade["scalp_regime"] = str(scalp_decision.get("regime") or "")
                    trade["scalp_signal"] = dict(scalp_now)
                    trade["scalp_grade"] = (
                        "A+" if scalp_obi >= 1.75 and vol_ratio >= 2.5 else "A"
                    )
                    trade["scalp_funding"] = scalp_gate.get("funding")
                    trade["scalp_risk_rs"] = float(scalp_gate.get("risk_rs") or 0.0)
                    trade["scalp_base_capital_at_risk_rs"] = float(
                        scalp_gate.get("base_capital_at_risk_rs") or 0.0
                    )
                    trade["target_spot"] = float(scalp_now.get("target_spot") or 0.0)
                    trade["sniper_grade"] = "SCALP"
                    state["trade"] = trade
                    state["risk_recorded"] = True
                    state["story_trade"] = True
                    state["ledger_sig"] = None
                    house_money.register_entry(now_ist_dt)
                    house_money.save(SCALPER_STATE_FILE)
                    scalper_last[name] = trade
                    hud["trades_today"] = int(hud.get("trades_today") or 0) + 1
                    hud["scalper_trades"] = house_money.trades
                    hud["scalper_vault"] = house_money.vault
                    hud["house_money_pool"] = house_money.house_money_pool
                    hud["scalper_next_qty"] = house_money.next_qty
                    hud["scalper_status"] = house_money.status(now_ist_dt)
                    hud["scalper_cooldown"] = house_money.cooldown_remaining(now_ist_dt)
                    save_hud(hud)
                    if pending:
                        ledger.remove_missed(str(pending.get("setup_id") or ""))
                        pending_setups.pop(name, None)
                        pending = None
                        setup_dirty = True

            if (not trade and len(hist) >= 5 and not traffic_red and alpha_allow
                    and not chop["locked"] and not pending_wait
                    and cutoff["new_entries_allowed"] and not feed_stale):
                grade = "A+" if (sniper_ready or greek_gate.get("conviction") == "A+") else "A"
                risk_check = guard.can_open(grade)
                signal = bottom if bottom.get("trigger") else top if top.get("pe_trigger") else {}
                target_override: dict = {}
                sl_pts = float(signal.get("sl_points") or risk) if sniper_ready else risk
                if risk_check["ok"] and sniper_ready and not (6.0 <= sl_pts <= 9.0):
                    risk_check = {"ok": False, "reason": "SNIPER_SL_OUT_OF_RANGE"}
                if risk_check["ok"]:
                    target_override = map_greek_targets(
                        entry=ltp, spot=spot,
                        p65=plan.get("p65") or None, p50=plan.get("p50"),
                        p90=float(plan.get("p90") or 0.0), p10=plan.get("p10"),
                        delta=option_delta, gamma=option_gamma, theta=option_theta,
                        vega=option_vega, expected_iv_change=iv_velocity,
                        oi_unwinding_pct=max(float(pe_market.get("oi_change") or 0.0), 0.0),
                        side=side, sl_points=sl_pts, qty=300,
                    )
                    if sniper_ready and not target_override.get("ok"):
                        risk_check = {"ok": False, "reason": "GREEK_TARGETS_MISSING"}
                if risk_check["ok"]:
                    trade = new_trade(ltp, sl_pts)
                    init_partial_state(trade)
                    trade["trade_id"] = uuid.uuid4().hex
                    trade["entry_at"] = now_iso
                    trade["mfe_price"] = round(ltp, 2)
                    trade["mae_price"] = round(ltp, 2)
                    trade["option_type"] = side
                    trade["sl_points"] = round(float(sl_pts), 2)
                    trade["sniper_grade"] = grade
                    trade["sniper"] = sniper_now
                    if target_override.get("ok"):
                        trade["target_1"] = target_override["target_1"]
                        trade["target_2_p90"] = target_override["target_2_p90"]
                        trade["t1_label"] = target_override["t1_label"]
                        trade["t2_label"] = target_override["t2_label"]
                        trade["greek_targets"] = target_override
                    state["trade"] = trade
                    state["risk_recorded"] = False
                    state["story_trade"] = False
                    guard.register_entry(grade)
                    if sniper_ready:
                        story.sniper_entry(
                            symbol=symbol, side=side, entry=ltp,
                            stop=float(trade["current_trailing_sl"]),
                            target1=float(trade["target_1"]),
                            target2=float(trade["target_2_p90"]),
                            signatures=int(signal.get("signature_count") or 0),
                            reason=str(signal.get("reason") or ""),
                            p65=float(plan.get("p65") or 0.0),
                            p90=float(plan.get("p90") or 0.0),
                            formula=(target_override.get("formula") or {}),
                        )
                        state["story_trade"] = True
                        story_gear[name] = 1
                        story_stop[name] = float(trade["current_trailing_sl"])
                    if name == "NIFTY":
                        hud["trades_today"] = int(hud.get("trades_today") or 0) + 1
                        hud["sniper_trades"] = guard.trades
                        hud["daily_lock"] = guard.daily_lock
                        hud["sniper_stats"] = guard.stats()
                        save_hud(hud)
                        guard.save(RISK_FILE)
                    # A triggered setup belongs in the executed table, not here.
                    if pending:
                        ledger.remove_missed(str(pending.get("setup_id") or ""))
                        pending_setups.pop(name, None)
                        pending = None
                        setup_dirty = True
            if not trade:
                trade = {"status": "WATCHING", "entry_price": round(ltp, 2),
                         "initial_sl": round(ltp - risk, 2),
                         "current_trailing_sl": round(ltp - risk, 2), "tsl_gear": 1,
                         "target_1": round(ltp + 1.5 * risk, 2),
                         "target_2_p90": round(ltp + 3 * risk, 2), "qty": 300,
                         "risk_points": risk, "live_pnl_rs": 0.0, "live_pnl_pct": 0.0}
                init_partial_state(trade)
            else:
                hero_trade = bool(trade.get("hero_mode"))
                low_5m = min((c["low"] for c in hist[-5:]), default=None)
                low_1m = hist[-1]["low"] if hist else None
                scalp_trade = bool(trade.get("scalp_mode"))
                if scalp_trade and trade.get("continuous_scalp"):
                    scalp_update = manage_continuous_scalp(
                        trade, ltp,
                        obi_5=(ce_obi if str(trade.get("option_type") or side) == "CE"
                               else pe_obi),
                        now=now_ist_dt,
                    )
                    legs = list(scalp_update.get("legs") or [])
                    scalp_sleeve.sync_trade(trade, ltp=ltp)
                    continuous_last[name] = trade
                    trade["mfe_price"] = max(float(trade.get("mfe_price") or ltp), ltp)
                    trade["mae_price"] = min(float(trade.get("mae_price") or ltp), ltp)
                    if scalp_update.get("exit"):
                        scalp_sleeve.finish_trade(trade, now=now_ist_dt)
                        continuous_cooldown_until[name] = now_ist_dt + timedelta(seconds=15)
                        flip = detect_scalp_flip(
                            str(trade.get("option_type") or side),
                            ce_obi_5=ce_obi, pe_obi_5=pe_obi,
                            ce_tbq=float(ce_quote.tbq if ce_quote else 0.0),
                            ce_tsq=float(ce_quote.tsq if ce_quote else 0.0),
                            pe_tbq=float(pe_quote.tbq if pe_quote else 0.0),
                            pe_tsq=float(pe_quote.tsq if pe_quote else 0.0),
                            profit_booked=bool(trade.get("scalp_s1_done")),
                        )
                        if flip.get("flip"):
                            scalper_pending[name] = str(flip.get("side") or "")
                        trade["exit_at"] = now_iso
                        state["reentry_armed"] = True
                        save_hud(hud)
                elif scalp_trade:
                    scalp_update = manage_scalp_trade(
                        trade, ltp,
                        obi_5=(ce_obi if str(trade.get("option_type") or side) == "CE"
                               else pe_obi),
                    )
                    legs = list(scalp_update.get("legs") or [])
                    for leg in legs:
                        add_scalp_hud_leg(hud, leg)
                    scalper_last[name] = trade
                    trade["mfe_price"] = max(float(trade.get("mfe_price") or ltp), ltp)
                    trade["mae_price"] = min(float(trade.get("mae_price") or ltp), ltp)
                    if not scalp_update.get("exit"):
                        hold_max = int(
                            (trade.get("scalp_signal") or {}).get("hold_max_seconds") or 0
                        )
                        try:
                            held = (now_ist_dt - datetime.fromisoformat(
                                str(trade.get("entry_at") or "")
                            )).total_seconds()
                        except (TypeError, ValueError):
                            held = 0.0
                        if (hold_max and held >= hold_max
                                and str(trade.get("status") or "").upper() == "OPEN"):
                            leg = close_remaining(trade, ltp, reason="SCALP_TIME_STOP")
                            if leg:
                                legs.append(leg)
                                add_scalp_hud_leg(hud, leg)
                                scalp_update["exit"] = True
                                scalp_update["reason"] = "SCALP_TIME_STOP"
                    if scalp_update.get("exit"):
                        settled = settle_scalp_trade(hud, house_money, trade, now_ist_dt)
                        scalper_brain[name] = brain_banner(
                            scalp_decision,
                            trade_no=house_money.trades,
                            pnl=float(settled.get("pnl") or 0.0),
                            previous_regime=str(trade.get("scalp_regime") or ""),
                        )
                        trade["exit_at"] = now_iso
                        state["reentry_armed"] = True
                        save_hud(hud)
                elif hero_trade:
                    hero_update = manage_hero_trade(
                        trade, ltp,
                        obi_5=option_obi,
                        tbq=float(option_quote.tbq if option_quote else 0.0),
                        tsq=float(option_quote.tsq if option_quote else 0.0),
                        iv_velocity_1m=iv_velocity,
                    )
                    legs = list(hero_update.get("legs") or [])
                    for leg in legs:
                        add_hero_hud_leg(hud, leg)
                    hero_last[name] = trade
                    if hero_update.get("peak_exit") or hero_update.get("stop_exit"):
                        total_pnl = float(hero_update.get("total_pnl") or 0.0)
                        hero_exit = hero_desk.register_exit(total_pnl, now_ist_dt)
                        swept = float(hero_exit.get("swept") or 0.0)
                        hud["profit_vault"] = round(
                            float(hud.get("profit_vault") or 0.0) + swept, 2
                        )
                        hud["hero_vault"] = float(hero_desk.profit_vault)
                        hud["vault_swept_today"] = round(
                            float(hud.get("vault_swept_today") or 0.0) + swept, 2
                        )
                        hud["hero_trades"] = hero_desk.trades
                        hud["hero_status"] = hero_desk.status
                        hud["tradable_capital"] = round(
                            float(hud.get("capital") or DEFAULT_CAPITAL)
                            - float(hud.get("profit_vault") or 0.0), 2
                        )
                        trade["hero_total_pnl"] = round(total_pnl, 2)
                        trade["hero_vault_swept"] = swept
                        state["reentry_armed"] = True
                        state["risk_recorded"] = True
                        trade["exit_at"] = now_iso
                        hero_desk.save(HERO_DESK_FILE)
                        save_hud(hud)
                        if hero_update.get("peak_exit"):
                            story.hero_peak_exit(
                                entry=float(trade.get("entry_price") or 0.0),
                                exit_price=ltp,
                                pnl=total_pnl,
                                roi_pct=((ltp / float(trade.get("entry_price") or ltp) - 1.0) * 100.0),
                                swept=swept,
                                vault=float(hud.get("profit_vault") or 0.0),
                                trade_no=hero_desk.trades,
                                max_trades=hero_desk.max_trades,
                            )
                        else:
                            story.stop_hit(price=ltp, pnl=total_pnl)
                        if hero_desk.status == "SILENT_WATCH_FOR_NEXT_WAVE":
                            story.hero_silent_watch(
                                cooldown_min=hero_desk.cooldown_remaining(now_ist_dt),
                                next_side=("PE" if side == "CE" else "CE"),
                            )
                    elif hero_update.get("stage1") or hero_update.get("stage2"):
                        save_hud(hud)
                else:
                    smc_payload = payload.get("smc", {}).get(name) or {}
                    structures = smc_payload.get("structures") or {}
                    primary = structures.get("5m") or smc_payload.get("structure") or {}
                    macro = structures.get("15m") or {}
                    micro = structures.get("1m") or {}
                    macro_bear = bool(
                        str(macro.get("active_choch") or "").startswith("BEARISH")
                        or macro.get("active_bos") == "BEARISH_BOS"
                    )
                    smc_bull_bos = bool(
                        primary.get("active_bos") == "BULLISH_BOS" and not macro_bear
                    )
                    smc_bear_choch = bool(
                        str(primary.get("active_choch") or "").startswith("BEARISH")
                        or str(micro.get("active_choch") or "").startswith("BEARISH")
                        or macro_bear
                    )
                    option_ema9 = ema_value([
                        float(bar.get("close") or 0.0) for bar in hist[-30:]
                    ])
                    super_update = manage_post_t2_super_trend(
                        trade, ltp,
                        smc_bull_bos=smc_bull_bos,
                        smc_bear_choch=smc_bear_choch,
                        obi_5=option_obi,
                        tbq=float(option_quote.tbq if option_quote else 0.0),
                        tsq=float(option_quote.tsq if option_quote else 0.0),
                        futures_volume_ratio=vol_ratio,
                        ema9=option_ema9,
                        atp=atp,
                        oi_change_5m_pct=float(
                            (pe_market if side == "CE" else ce_market).get("oi_change") or 0.0
                        ),
                        atr_1m=atr_opt,
                        latest_1m_low=float(hist[-1].get("low") or 0.0) if hist else 0.0,
                    )
                    legs = list(super_update.get("legs") or [])
                    if str(trade.get("status") or "").upper() == "OPEN":
                        gear_update(trade, ltp, low_5m, low_1m)
                        trade["mfe_price"] = max(float(trade.get("mfe_price") or ltp), ltp)
                        trade["mae_price"] = min(float(trade.get("mae_price") or ltp), ltp)
                        if (str(trade.get("status") or "").upper() == "CLOSED"
                                and int(trade.get("qty_open") or 0) > 0):
                            stop_leg = close_remaining(
                                trade,
                                float(trade.get("current_trailing_sl") or ltp),
                                reason="SL",
                            )
                            if stop_leg:
                                legs.append(stop_leg)
                for leg in legs:
                    leg.setdefault("at", now_iso)
                if str(trade.get("status") or "").upper() == "CLOSED" and not trade.get("exit_at"):
                    trade["exit_at"] = now_iso
                if name == "NIFTY" and legs and not hero_trade and not scalp_trade:
                    for leg in legs:
                        add_hud_leg(hud, leg, guard)
                        if float(leg.get("pnl") or 0.0) > 0:
                            story.vault_locked(
                                profit=float(leg.get("pnl") or 0.0),
                                swept=round(float(leg.get("pnl") or 0.0) * 0.50, 2),
                                vault=float(hud.get("profit_vault") or 0.0),
                                tradable=float(hud.get("tradable_capital") or 0.0),
                            )
                    save_hud(hud)
                if (str(trade.get("status") or "").upper() == "CLOSED"
                        and not state.get("risk_recorded") and not hero_trade
                        and not scalp_trade):
                    guard.register_trade(
                        pnl=float(trade.get("realized_pnl_rs") or 0.0),
                        outcome=str(trade.get("exit_reason") or "CLOSED"),
                        sl_points=float(trade.get("sl_points") or trade.get("risk_points") or 0.0),
                    )
                    state["risk_recorded"] = True
                    hud["daily_lock"] = guard.daily_lock
                    hud["sniper_trades"] = guard.trades
                    hud["sniper_stats"] = guard.stats()
                    hud["expectancy"] = expectancy_stats(
                        ledger.trades, month=today_ist()[:7],
                        starting_capital=float(hud.get("starting_capital") or DEFAULT_CAPITAL),
                        profit_vault=float(hud.get("profit_vault") or 0.0),
                    )
                    save_hud(hud)
                    guard.save(RISK_FILE)
                    if guard.daily_lock:
                        story.daily_lock(losses=guard.consecutive_sl,
                                         realized=float(hud.get("realized_today") or 0.0))
                # Persist on open, every T1/T2/SL sub-booking and closure.  The
                # live mark itself keeps updating through SSE; the file keeps
                # the banked, auditable numbers.
                sig = (str(trade.get("status") or ""), len(trade.get("booked_legs") or []),
                       round(float(trade.get("realized_pnl_rs") or 0.0), 2))
                if state.get("ledger_sig") != sig:
                    ledger.upsert_trade(paper_trade_from_live(
                        trade, date=today_ist(), symbol=name, light=light,
                    ))
                    ledger.save()
                    state["ledger_sig"] = sig
                    if not args.no_firebase:
                        publisher.put("/kronos_state/trade_ledger.json", ledger.snapshot())
            if setup_dirty:
                ledger.save()
                if not args.no_firebase:
                    publisher.put("/kronos_state/trade_ledger.json", ledger.snapshot())
            trade["option_symbol"] = symbol

            oi = float(market.get("oi") or 0.0)
            hist_oi = oi_hist[name]
            hist_oi.append((cycle, oi))
            five_ago = next((v for t, v in hist_oi if cycle - t <= 300), oi)
            oi_change = round((oi / five_ago - 1) * 100, 2) if five_ago else 0.0

            # ---- 100% full-spectrum snapshot (futures flow, 5-level depth,
            # 11-strike PCR, Greeks/IV velocity, VIX and heavyweight breadth) ----
            fut_key = futures_keys.get(name, "")
            fut_state = engine.instruments.get(fut_key) if fut_key else None
            fut_quote = fut_state.quote if fut_state else None
            fut_depth = book_imbalance(
                fut_quote.bids if fut_quote else None,
                fut_quote.asks if fut_quote else None,
            )
            fut_ltp = float(fut_quote.ltp if fut_quote else 0.0)
            vix_state = engine.instruments.get(INDIA_VIX)
            vix_quote = vix_state.quote if vix_state else None
            vix_ltp = float(vix_quote.ltp if vix_quote else 0.0)
            vix_prev = vix_hist[-1][1] if vix_hist else None
            vix_change_pct = ((vix_ltp / vix_prev - 1.0) * 100.0) if vix_prev else 0.0
            basis_now = round(fut_ltp - spot, 2) if fut_ltp else 0.0
            basis_prev = basis_hist[name][-1][1] if basis_hist[name] else None
            breadth_rows: list[dict] = []
            for hname, hkey in HEAVYWEIGHTS.items():
                hstate = engine.instruments.get(hkey)
                hq = hstate.quote if hstate else None
                h_ltp = float(hq.ltp if hq else 0.0)
                h_atp = float(hq.atp if hq else 0.0)
                h_tbq = float(hq.tbq if hq else 0.0)
                h_tsq = float(hq.tsq if hq else 0.0)
                h_bars = engine.windows(hkey, 1)
                h_bar = h_bars[-1] if h_bars else None
                h_prev_bar = h_bars[-2] if len(h_bars) >= 2 else h_bar
                h_open = float(getattr(h_bar, "open", 0.0) or 0.0)
                h_close = float(getattr(h_bar, "close", h_ltp) or h_ltp)
                h_high = float(getattr(h_prev_bar, "high", h_ltp) or h_ltp)
                h_low = float(getattr(h_prev_bar, "low", h_ltp) or h_ltp)
                h_vtt = float(getattr(hq, "vtt", 0.0) or 0.0)
                h_hist = parallel_heavy_hist.setdefault(hname, deque(maxlen=20))
                h_hist.append((cycle, h_ltp, h_vtt))
                h_base = next((row for row in h_hist if cycle - row[0] <= 3.0),
                              h_hist[0])
                h_velocity = h_ltp - h_base[1]
                h_vtt_ratio = 0.0
                if len(h_hist) >= 3:
                    h_deltas = [max(h_hist[idx][2] - h_hist[idx - 1][2], 0.0)
                                for idx in range(1, len(h_hist))]
                    h_floor = (sum(h_deltas[:-1]) / len(h_deltas[:-1])
                               if len(h_deltas) > 1 else 0.0)
                    h_vtt_ratio = (h_deltas[-1] / h_floor) if h_floor > 0 else 0.0
                if h_vtt_ratio <= 0 and h_tsq > 0:
                    h_vtt_ratio = h_tbq / h_tsq
                h_bull = bool(h_ltp > 0 and h_atp > 0 and h_ltp > h_atp
                              and h_close > h_open and h_tbq >= 1.25 * h_tsq)
                h_bear = bool(h_ltp > 0 and h_atp > 0 and h_ltp < h_atp
                              and h_close < h_open and h_tsq >= 1.25 * h_tbq)
                breadth_rows.append({
                    "name": hname, "ltp": round(h_ltp, 2),
                    "atp": round(h_atp, 2), "bullish": h_bull, "bearish": h_bear,
                    "open_1m": round(h_open, 2), "close_1m": round(h_close, 2),
                    "one_min_high": round(h_high, 2), "one_min_low": round(h_low, 2),
                    "velocity_3s": round(h_velocity, 2),
                    "vtt_ratio": round(h_vtt_ratio, 2),
                    "high_break": bool(h_ltp > h_high and h_velocity > 0),
                    "low_break": bool(h_ltp < h_low and h_velocity < 0),
                    "tbq": round(h_tbq, 2), "tsq": round(h_tsq, 2),
                })
            aligned_heavy = sum(1 for row in breadth_rows if row["bullish"])
            # BankNifty futures are the third leg of the 3-second lead-lag basket.
            bank_lead_key = futures_keys.get("BANKNIFTY", "")
            bank_lead_state = (engine.instruments.get(bank_lead_key)
                               if bank_lead_key else None)
            bank_lead_quote = bank_lead_state.quote if bank_lead_state else None
            bank_lead_ltp = float(bank_lead_quote.ltp if bank_lead_quote else 0.0)
            bank_lead_tbq = float(getattr(bank_lead_quote, "tbq", 0.0) or 0.0)
            bank_lead_tsq = float(getattr(bank_lead_quote, "tsq", 0.0) or 0.0)
            bank_lead_vtt = float(getattr(bank_lead_quote, "vtt", 0.0) or 0.0)
            bank_lead_bars = engine.windows(bank_lead_key, 1) if bank_lead_key else []
            bank_lead_bar = bank_lead_bars[-1] if bank_lead_bars else None
            bank_lead_prev = (bank_lead_bars[-2]
                              if len(bank_lead_bars) >= 2 else bank_lead_bar)
            bank_lead_high = float(getattr(bank_lead_prev, "high", bank_lead_ltp)
                                   or bank_lead_ltp)
            bank_lead_low = float(getattr(bank_lead_prev, "low", bank_lead_ltp)
                                  or bank_lead_ltp)
            b_hist = parallel_fut_hist.setdefault("BANKNIFTY", deque(maxlen=20))
            b_hist.append((cycle, bank_lead_ltp, bank_lead_vtt))
            b_base = next((row for row in b_hist if cycle - row[0] <= 3.0), b_hist[0])
            b_velocity = bank_lead_ltp - b_base[1]
            b_vtt_ratio = 0.0
            if len(b_hist) >= 3:
                b_deltas = [max(b_hist[idx][2] - b_hist[idx - 1][2], 0.0)
                            for idx in range(1, len(b_hist))]
                b_floor = (sum(b_deltas[:-1]) / len(b_deltas[:-1])
                           if len(b_deltas) > 1 else 0.0)
                b_vtt_ratio = (b_deltas[-1] / b_floor) if b_floor > 0 else 0.0
            if b_vtt_ratio <= 0 and bank_lead_tsq > 0:
                b_vtt_ratio = bank_lead_tbq / bank_lead_tsq
            bank_lead_row = {
                "name": "BANKNIFTY", "ltp": round(bank_lead_ltp, 2),
                "one_min_high": round(bank_lead_high, 2),
                "one_min_low": round(bank_lead_low, 2),
                "velocity_3s": round(b_velocity, 2),
                "vtt_ratio": round(b_vtt_ratio, 2),
                "high_break": bool(bank_lead_ltp > bank_lead_high and b_velocity > 0),
                "low_break": bool(bank_lead_ltp < bank_lead_low and b_velocity < 0),
                "tbq": round(bank_lead_tbq, 2), "tsq": round(bank_lead_tsq, 2),
            }
            option_tick = socket.ticks.get(instrument_key) or {}
            greeks_now = {
                "delta": option_tick.get("delta", greeks.get("delta")),
                "gamma": option_tick.get("gamma", greeks.get("gamma")),
                "theta": option_tick.get("theta", greeks.get("theta")),
                "vega": option_tick.get("vega", greeks.get("vega")),
                "rho": option_tick.get("rho", greeks.get("rho")),
                "iv": option_tick.get("iv", greeks.get("iv")),
            }
            native_ohlc = (engine.instruments.get(key).market_ohlc
                           if key in engine.instruments else {})
            if not native_ohlc and fut_key in engine.instruments:
                native_ohlc = engine.instruments[fut_key].market_ohlc
            fut_obi = (fut_depth.get("ratio", 0.0)
                       if (fut_depth.get("bid_qty", 0.0)
                           + fut_depth.get("ask_qty", 0.0)) > 0 else 0.0)
            scan_started_ns = time.perf_counter_ns()
            snapshot = build_full_spectrum(
                symbol=name, spot=spot,
                futures={"ltp": fut_ltp, "atp": getattr(fut_quote, "atp", 0.0),
                          "volume": getattr(fut_quote, "vtt", 0.0),
                          "amount": (float(getattr(fut_quote, "vtt", 0.0))
                                     * float(getattr(fut_quote, "atp", 0.0))),
                          "oi": getattr(fut_quote, "oi", 0.0)},
                depth={"bids": list(getattr(fut_quote, "bids", []) or []),
                       "asks": list(getattr(fut_quote, "asks", []) or []),
                       "bid_qty_top": getattr(fut_quote, "bid_qty_top", 0.0),
                       "ask_qty_top": getattr(fut_quote, "ask_qty_top", 0.0),
                       "obi_5": fut_obi,
                       "bid_qty_5": fut_depth.get("bid_qty", 0.0),
                       "ask_qty_5": fut_depth.get("ask_qty", 0.0),
                       "tbq": getattr(fut_quote, "tbq", 0.0),
                       "tsq": getattr(fut_quote, "tsq", 0.0),
                       "tbq_tsq": (float(getattr(fut_quote, "tbq", 0.0))
                                   / float(getattr(fut_quote, "tsq", 0.0))
                                   if getattr(fut_quote, "tsq", 0.0) else 0.0)},
                chain=chain_cache.get(name) or [], greeks=greeks_now,
                vix={"ltp": vix_ltp, "change_pct": vix_change_pct,
                     "direction": "RISING" if vix_change_pct > 0 else "FALLING" if vix_change_pct < 0 else "FLAT",
                     "previous": vix_prev},
                breadth={"aligned": aligned_heavy, "total": len(breadth_rows),
                         "rows": breadth_rows},
                market_ohlc=native_ohlc, previous_basis=basis_prev,
                iv_velocity_5m=iv_velocity, gamma_velocity_5m=gamma_velocity,
                kronos_move=kronos_move, side=side,
                option_ltp=(ce_ltp if str(side).upper() == "CE" else pe_ltp),
                source="upstox-v3", fixture=False,
                as_of=payload["updated_at"], generated_epoch=cycle,
                paths_up=plan.get("paths_up"), samples=float(plan.get("samples") or 0),
                neutral_pct=neutral_pct,
                order_flow_divergence=bool(sniper_now.get("trigger")),
                stale=feed_stale,
            )
            snapshot["sniper"] = sniper_now
            latest_scan_ms[name] = round((time.perf_counter_ns() - scan_started_ns) / 1_000_000.0, 3)
            telemetry.record_scan(scan_ms=latest_scan_ms[name])
            payload["full_spectrum"][name] = snapshot
            payload["sniper"][name] = sniper_now

            # ---- parallel alpha engines: same ticks, five independent lenses ----
            plan_samples = float(plan.get("samples") or 0.0)
            paths_up = float(plan.get("paths_up") or 0.0)
            kronos_bullish = bool(plan_samples > 0 and paths_up / plan_samples > 0.5)
            kronos_bearish = bool(plan_samples > 0 and paths_up / plan_samples < 0.5)
            parity_now = parity_dislocation_snipe(
                fut_ltp, strike, ce_ltp, pe_ltp,
                symbol=name, ce_obi_5=ce_obi, pe_obi_5=pe_obi,
                kronos_bullish=kronos_bullish, kronos_bearish=kronos_bearish,
            )
            leader_rows = {row["name"]: row for row in breadth_rows}
            leader_rows["BANKNIFTY"] = bank_lead_row
            lead_lag_now = lead_lag_snipe(
                leader_rows,
                lagging_option_move_3s=parallel_option_move_3s,
                lagging_obi_5=max(ce_obi, pe_obi),
                symbol=name,
            )
            tsunami_now = heavyweight_tsunami(
                {row["name"]: row for row in breadth_rows},
                previous_bull_count=parallel_prev_bull,
                previous_bear_count=parallel_prev_bear,
                kronos_bullish=kronos_bullish,
                kronos_bearish=kronos_bearish,
            )
            box_rows = hist[-30:]
            box_high = max((row["high"] for row in box_rows), default=0.0)
            box_low = min((row["low"] for row in box_rows), default=0.0)
            straddle_now = ce_ltp + pe_ltp
            straddle_low = min(parallel_straddle_low.get(name, straddle_now),
                               straddle_now)
            parallel_straddle_low[name] = straddle_low
            fut_oi_now = float(getattr(fut_quote, "oi", 0.0) or 0.0)
            fut_oi_hist = parallel_oi_hist.setdefault(name, deque(maxlen=20))
            fut_oi_hist.append((cycle, fut_oi_now))
            fut_oi_base = next((value for stamp, value in fut_oi_hist
                                if cycle - stamp <= 3.0), fut_oi_now)
            coiled_now = coiled_spring_trap(
                spot, box_high=box_high, box_low=box_low,
                compression_minutes=float(len(box_rows)),
                straddle_ltp=straddle_now,
                straddle_intraday_low=straddle_low,
                futures_oi_rising=bool(fut_oi_now > fut_oi_base),
                futures_vtt_ratio=vol_ratio,
            )
            parallel_runtime[name] = {
                "parity": parity_now,
                "lead_lag": lead_lag_now,
                "tsunami": tsunami_now,
                "coiled_spring": coiled_now,
                "metrics": {
                    "atr_velocity": round(max(abs(spot_velocity), spot_atr * 0.10), 2),
                    "obi_strength": round(max(ce_obi, pe_obi), 2),
                    "parity_edge": round(max(float(parity_now.get("ce_edge") or 0.0),
                                              float(parity_now.get("pe_edge") or 0.0)), 2),
                    "kronos_confidence": round(
                        (paths_up / plan_samples * 100.0) if plan_samples else 0.0, 2
                    ),
                },
            }
            payload["option_chain"][name] = chain_cache.get(name) or []
            option_model = build_option_model_input(
                engine.windows(instrument_key, 1) if instrument_key else [], limit=512
            )
            payload["kronos_inputs"].setdefault(name, {})["option_1m"] = {
                "shape": [len(option_model), 6],
                "last": tensor_rows(option_model)[-2:],
            }
            if name == "NIFTY":
                if vix_ltp:
                    vix_hist.append((cycle, vix_ltp))
            if fut_ltp:
                basis_hist[name].append((cycle, basis_now))

            mtf_now = payload["mtf"].get(name) or analyze_mtf(
                engine.windows(key, 1), engine.windows(key, 5), engine.windows(key, 15),
                paths_up=plan.get("paths_up"), samples=plan.get("samples"),
            )
            option_quote = (engine.instruments.get(instrument_key).quote
                            if instrument_key in engine.instruments else None)
            # Use the real five-level book; never synthesize a one-level book
            # from aggregate quantities.
            depth_bids = list(option_quote.bids) if option_quote else []
            depth_asks = list(option_quote.asks) if option_quote else []
            smart = smart_limit_price(
                bid=float((option_quote.bid if option_quote else market.get("bid_price")) or 0.0),
                ask=float((option_quote.ask if option_quote else market.get("ask_price")) or 0.0),
                bids=depth_bids, asks=depth_asks, atp_vwap=atp,
                p25_dip=p25(engine.windows(instrument_key, 1)),
                mtf=mtf_now, side=side, oi_change_5m_pct=oi_change,
                high_velocity=abs(oi_change) >= 1.5 and oi_change < 0,
            )
            payload["smart_entry"][name] = smart
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
                "current_candle_5m": engine.current(instrument_key, 5) if instrument_key else None,
                "current_candle_15m": engine.current(instrument_key, 15) if instrument_key else None,
                "candles_1m": hist[-512:],
                "candles_5m": ui_bars(engine.windows(instrument_key, 5))[-512:] if instrument_key else [],
                "candles_15m": ui_bars(engine.windows(instrument_key, 15))[-512:] if instrument_key else [],
                "volume": volume,
                "mtf": mtf_now,
                "smart_entry": smart,
                "expiry": expiries.get(key),
            }
            # ---- institutional journal snapshot: entry, every T1/T2 leg and the
            # final exit.  One write per lifecycle change, using the exact same
            # candle windows the desk is seeing right now. ----
            if (isinstance(trade, dict)
                    and str(trade.get("status") or "").upper() in {"OPEN", "CLOSED"}):
                journal_sig = (
                    str(trade.get("status") or ""),
                    len(trade.get("booked_legs") or []),
                    round(float(trade.get("realized_pnl_rs") or 0.0), 2),
                    int(trade.get("qty_open") or 0),
                    round(float(trade.get("current_trailing_sl") or 0.0), 2),
                    int(trade.get("tsl_gear") or 0),
                )
                if state.get("journal_sig") != journal_sig:
                    try:
                        from state.trade_journal_store import TradeJournalStore

                        try:
                            mode_now = str((router.load() or {}).get("mode") or "PAPER")
                        except Exception:  # noqa: BLE001 - journal stays PAPER-safe
                            mode_now = "PAPER"
                        journal_context = {
                            "symbol": name,
                            "spot": spot,
                            "spot_exit": spot,
                            "basis": basis_now,
                            "vix": vix_ltp,
                            "pcr": plan.get("pcr") or 0.0,
                            "obi_5": option_obi,
                            "tbq_tsq": option_flow,
                            "doi_5m_pct": oi_change,
                            "atp": atp,
                            "paths_up": plan.get("paths_up"),
                            "samples": plan.get("samples"),
                            "p10": plan.get("p10"),
                            "median": plan.get("median"),
                            "p90": plan.get("p90"),
                            "mtf": mtf_now,
                            "futures_volume_ratio": vol_ratio,
                            "expiry": expiries.get(key),
                            "mode": mode_now,
                            "greeks": greeks_now,
                            "vault_locked_inr": hud.get("profit_vault") or 0.0,
                        }
                        TradeJournalStore().record_live_trade(
                            trade,
                            context=journal_context,
                            option_candles=hist[-45:],
                            spot_candles=ui_bars(engine.windows(key, 5))[-30:],
                        )
                        state["journal_sig"] = journal_sig
                    except Exception as exc:  # noqa: BLE001 - never crash the desk
                        print(f"journal snapshot failed for {name}: {exc}", flush=True)

            payload["option"][name] = blob
            payload["trade"][name] = trade
            payload["scalper"][name] = {
                "mode": scalper_mode,
                "regime": scalp_decision,
                "signal": scalp_now,
                "gate": scalp_gate,
                "house_money": house_money.snapshot(now_ist_dt),
                "brain": (scalper_brain.get(name)
                          or brain_banner(scalp_decision)),
                "trade": (trade if isinstance(trade, dict) and trade.get("scalp_mode")
                          else scalper_last.get(name) or {}),
            }
            payload.setdefault("continuous_scalp", {})[name] = continuous_now
            hero_selected = selected_hero or hero_scan.get("hero") or hero_scan.get("watch") or {}
            hero_payload_trade = (trade if isinstance(trade, dict) and trade.get("hero_mode")
                                  else hero_last.get(name) or {})
            payload["expiry_hero"][name] = {
                "enabled": bool(expiry_ctx.get("enabled")),
                "mode": hero_mode,
                "is_expiry_day": bool(expiry_ctx.get("is_expiry_day")),
                "source": expiry_ctx.get("source"),
                "expiry": expiries.get(key),
                "window": hero_window,
                "scan": hero_scan_snapshot(hero_scan),
                "selected": hero_selected,
                "fuel": hero_selected.get("fuel") or {},
                "desk": hero_desk.snapshot(now_ist_dt),
                "trade": hero_payload_trade,
                "signal": hero_signal,
            }

            # A scalp close request settles into the house-money desk before
            # the generic one-click close path can see it.
            request_id = str(close_request.get("request_id") or "")
            if (request_id and request_id != close_handled
                    and str(close_request.get("symbol") or name).upper() == name
                    and isinstance(trade, dict) and trade.get("scalp_mode")
                    and str(trade.get("status") or "").upper() == "OPEN"):
                close_handled = request_id
                ack: dict = {"request_id": request_id, "symbol": name, "at": now_iso}
                try:
                    if router.load()["mode"] == "REAL":
                        live_exit = router.close_live_at_market(trade)
                        ack["order_id"] = live_exit.get("order_id")
                        ack["mode"] = "REAL"
                    else:
                        ack["mode"] = "PAPER"
                    leg = close_remaining(trade, ltp, reason=MANUAL_CLOSE_REASON)
                    trade["exit_at"] = now_iso
                    trade["broker_orders"] = []
                    if leg:
                        if trade.get("continuous_scalp"):
                            scalp_sleeve.sync_trade(trade, ltp=ltp)
                            scalp_sleeve.finish_trade(trade, now=now_ist_dt)
                        else:
                            add_scalp_hud_leg(hud, leg)
                            settled = settle_scalp_trade(hud, house_money, trade, now_ist_dt)
                            scalper_brain[name] = brain_banner(
                                scalp_decision, trade_no=house_money.trades,
                                pnl=float(settled.get("pnl") or 0.0),
                            )
                        save_hud(hud)
                    ack.update({"ok": True, "fill_price": ltp,
                                "pnl": float(leg.get("pnl") or 0.0) if leg else 0.0})
                    ledger.upsert_trade(paper_trade_from_live(
                        trade, date=today_ist(), symbol=name, light=light,
                    ))
                    ledger.save()
                    if not args.no_firebase:
                        publisher.put("/kronos_state/trade_ledger.json", ledger.snapshot())
                except (ModeRejected, Exception) as exc:  # noqa: BLE001
                    ack.update({"ok": False, "reason": str(exc)})
                try:
                    close_ack_path.write_text(json.dumps(ack, indent=1), encoding="utf-8")
                except OSError:
                    pass

            # A one-click close request is consumed exactly once. Paper mode
            # books the live premium into the ledger/HUD; REAL mode has already
            # been through the Submit Mode guard and sends the exchange SELL.
            request_id = str(close_request.get("request_id") or "")
            if (request_id and request_id != close_handled
                    and str(close_request.get("symbol") or name).upper() == name
                    and str(trade.get("status") or "").upper() == "OPEN"):
                close_handled = request_id
                mode_now = router.load()["mode"]
                ack: dict = {"request_id": request_id, "symbol": name, "at": now_iso}
                try:
                    if mode_now == "REAL":
                        live_exit = router.close_live_at_market(trade)
                        ack["order_id"] = live_exit.get("order_id")
                        ack["mode"] = "REAL"
                    else:
                        ack["mode"] = "PAPER"
                    leg = close_remaining(trade, ltp, reason=MANUAL_CLOSE_REASON)
                    trade["exit_at"] = now_iso
                    trade["broker_orders"] = []
                    if leg:
                        add_hud_leg(hud, leg, guard)
                        save_hud(hud)
                        story.manual_smart_exit(price=ltp, pnl=float(leg.get("pnl") or 0.0),
                                                qty=int(leg.get("qty") or 0))
                    ack.update({"ok": True, "fill_price": ltp,
                                "pnl": float(leg.get("pnl") or 0.0) if leg else 0.0})
                    ledger.upsert_trade(paper_trade_from_live(
                        trade, date=today_ist(), symbol=name, light=light,
                    ))
                    ledger.save()
                    if not args.no_firebase:
                        publisher.put("/kronos_state/trade_ledger.json", ledger.snapshot())
                except (ModeRejected, Exception) as exc:  # noqa: BLE001
                    ack.update({"ok": False, "reason": str(exc)})
                try:
                    close_ack_path.write_text(json.dumps(ack, indent=1), encoding="utf-8")
                except OSError:
                    pass

            # REAL entries are only possible when Submit Mode persisted a confirmed
            # mode with a valid margin read. The protective SL is placed with it.
            if (router.load()["mode"] == "REAL" and not trade.get("broker_entry_order_id")
                    and trade.get("status") == "OPEN" and instrument_key
                    and cutoff["new_entries_allowed"] and not feed_stale):
                try:
                    trade["instrument_key"] = instrument_key
                    entry = router.place_entry(trade=trade, price=ltp)
                    trade["broker_entry_order_id"] = entry.get("order_id") or ""
                    if entry.get("order_id"):
                        stop = router.broker.place_order(
                            instrument_key=instrument_key,
                            quantity=int(trade.get("qty_open") or trade.get("qty") or 0),
                            transaction_type="SELL", order_type="SL-M",
                            trigger_price=float(trade.get("current_trailing_sl") or 0.0),
                            tag="KRONOS-SL",
                        )
                        trade["broker_orders"] = ["SL-" + str(stop.get("order_id"))]
                except Exception as exc:  # noqa: BLE001 - never crash the desk
                    print(f"REAL entry rejected: {exc}", flush=True)
                    trade["broker_error"] = str(exc)

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
                 "pass": risk <= 9 and (spread <= 0.5 or spread == 0),
                 "value": f"SL = ₹{risk:.2f} pts (<= ₹9) · Spread ₹{spread:.2f}"},
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
                    leg_name = str(leg.get("leg") or "")
                    if leg_name in ("T1", "T2") or leg_name.startswith(("SUPER_T1", "SUPER_T2")):
                        story.partial_booked(
                            name=leg_name,
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
                    # A T2 or Hero peak winner must not be narrated as a stop-out.
                    if ("sl" not in hits and trade.get("exit_reason") != "T2"
                            and not trade.get("hero_mode")
                            and not trade.get("scalp_mode")):
                        story.stop_hit(price=float(trade.get("exit_price") or ltp),
                                       pnl=float(trade.get("realized_pnl_rs") or 0.0))
                        hits.add("sl")

        # Publish one parallel-alpha snapshot after both index books have been
        # refreshed.  The engines are advisory order intents; the existing
        # ₹35k/₹65k sleeves remain the only capital owners.
        if parallel_runtime:
            primary = parallel_runtime.get("NIFTY") or next(iter(parallel_runtime.values()))
            sister = parallel_runtime.get("BANKNIFTY") or {}
            rotator_now = dual_index_rotator(
                primary.get("metrics") or {},
                sister.get("metrics") or {},
                previous_winner=parallel_prev_winner,
            )
            parallel_prev_winner = str(rotator_now.get("winner") or "")
            tsunami_primary = primary.get("tsunami") or {}
            parallel_snapshot = build_parallel_alpha(
                parity=primary.get("parity"),
                lead_lag=primary.get("lead_lag"),
                tsunami=tsunami_primary,
                coiled_spring=primary.get("coiled_spring"),
                rotator=rotator_now,
                stale=feed_stale,
                now=now_ist_dt,
            )
            parallel_prev_bull = int(tsunami_primary.get("bull_count") or 0)
            parallel_prev_bear = int(tsunami_primary.get("bear_count") or 0)
            macro_live = any(
                isinstance(row, dict)
                and str(row.get("status") or "").upper() == "OPEN"
                and not row.get("scalp_mode")
                for row in (payload.get("trade") or {}).values()
            )
            parallel_snapshot["execution_capacity"] = {
                "scalp_available": round(max(0.0, SCALP_SLEEVE - scalp_sleeve.deployed), 2),
                "sniper_available": 0.0 if macro_live else SNIPER_SLEEVE,
                "scalp_limit": SCALP_SLEEVE,
                "sniper_limit": SNIPER_SLEEVE,
            }
            # Publish the hard ₹35k/₹65k split with the current live exposure.
            capacity = capital_router.snapshot()
            capacity["live_exposure"] = {
                "sleeve_a": scalp_sleeve.deployed,
                "sleeve_b": SNIPER_SLEEVE if macro_live else 0.0,
            }
            parallel_snapshot["capital_router"] = capacity
            payload["parallel_alpha"] = parallel_snapshot

        if cycle - parity_at >= 60:
            parity_at = cycle
            for name, key in KEYS.items():
                parity[name] = verify_parity(engine, key, token)

        payload.update({
            "socket_connected": socket.streaming(),
            "socket_frames": socket.frames,
            "socket_last_frame_age": round(time.time() - socket.last_frame, 1) if socket.last_frame else None,
            "socket_error": socket.last_error,
            "tick_rate": round(socket.frames / max(cycle - started, 1), 2),
            "buffer_1m": {n: len(engine.windows(k, 1)) for n, k in KEYS.items()},
            "buffer_5m": {n: len(engine.windows(k, 5)) for n, k in KEYS.items()},
            "buffer_15m": {n: len(engine.windows(k, 15)) for n, k in KEYS.items()},
            "parity": parity,
            "market_open": is_market_open(now_ist_dt),
            "fixture": False,
            "sanitizer": engine.sanitizer.snapshot(),
            "feed_status": {},
            "entries_suspended": bool(feed_stale),
            "story": story.snapshot(),
            "story_at": round(time.time(), 3),
            "hud": hud,
        })
        if feed_stale:
            payload["feed_status"] = {
                "status": "FEED_OFFLINE_OR_STALE",
                "age_ms": socket_age_ms,
                "market_open": payload["market_open"],
                "allow_order_generation": False,
                "reason": ("NO_V3_FRAME" if socket_age_ms is None
                            else f"SOCKET_AGE_{socket_age_ms}MS_GT_3000MS"),
                "as_of": payload.get("updated_at"),
                "ok": False,
            }
        else:
            payload["feed_status"] = feed_state(
                payload, now=cycle, market_open=payload["market_open"]
            ).as_dict()
        payload["entries_suspended"] = bool(feed_stale) or not payload["feed_status"].get(
            "allow_order_generation", False
        )
        narration_plan = load_plan("NIFTY")
        oscillator_now = build_oscillator_from_feed(
            payload, "NIFTY", plan=narration_plan,
        )
        narration_now = brain_narrator.record_cycle(
            payload, "NIFTY", plan=narration_plan, oscillator=oscillator_now,
            scan_ms=latest_scan_ms.get("NIFTY"),
            sanitize_ms=float(engine.sanitizer.snapshot().get("sanitize_latency_us") or 0.0) / 1000.0,
            latency_ms=max(0.0, cycle * 1000.0 - (socket.last_ltt_ms or 0.0)),
        )
        payload["model_oscillator"] = oscillator_now
        payload["model_thoughts_te"] = narration_now["thoughts"]
        payload["brain_log_te"] = narration_now["log"]
        payload["programmatic"] = telemetry.snapshot(
            payload, now=cycle, scan_latency_ms=latest_scan_ms.get("NIFTY")
        )
        # Six-check autopilot heartbeat: local for the laptop, Firebase for the
        # static phone page.  A check is GREEN only when its real source is hot.
        chain_counts = [len(chain_cache.get(name) or []) for name in KEYS]
        min_chain = min(chain_counts) if chain_counts else 0
        buffer_values = [
            int((payload.get(key) or {}).get(name) or 0)
            for key in ("buffer_1m", "buffer_5m", "buffer_15m")
            for name in KEYS
        ]
        min_buffer = min(buffer_values) if buffer_values else 0
        model_bars = min(
            int((payload.get("buffer_1m") or {}).get(name) or 0)
            for name in KEYS
        ) if payload.get("buffer_1m") else 0
        model_ready = min_buffer >= 512 and model_bars >= 512
        model_feeds = {
            name: {"ready": model_ready, "bars": model_bars, "features": 6,
                   "source": "shared-ohlcva"}
            for name in ("base", "small", "mini")
        }
        edge_ready = bool(edge and edge.url)
        checks = build_checks(
            auth_ok=auth_verified,
            runner_ok=True,
            edge_ok=edge_ready,
            socket_ok=socket.streaming(),
            chain_ok=min_chain >= 11,
            buffer_ok=min_buffer >= 512,
            models_ok=model_ready,
            details={
                "auth": f"VERIFIED · {auth_minted_at}",
                "runner": "ONLINE (PC-Free) · GitHub Actions",
                "edge": (f"CONNECTED · {edge.url}" if edge_ready else "TUNNEL NOT PUBLISHED"),
                "socket": f"{payload.get('tick_rate', 0)} ticks/s · chain {min_chain}/11",
                "bars": f"min {min_buffer}/512 · futures volume fused",
                "models": f"102M/25M/4M · {model_bars}×6 feeds",
            },
        )
        if checks["all_green"] and not ready_at_ist:
            ready_at_ist = datetime.now(tz=IST).isoformat(timespec="seconds")
        readiness = {
            **checks,
            "runner": {"online": True, "heartbeat_epoch": cycle,
                       "host": "github-actions", "run_id": os.environ.get("GITHUB_RUN_ID", "")},
            "auth": {"minted": auth_verified, "verified": auth_verified,
                     "minted_at_ist": auth_minted_at},
            "edge": {"active": edge_ready, "url": (edge.url if edge else ""),
                     "updated_epoch": cycle},
            "buffers": {"1m": payload.get("buffer_1m") or {},
                        "5m": payload.get("buffer_5m") or {},
                        "15m": payload.get("buffer_15m") or {}},
            "models": model_feeds,
            "warmup": warmup,
            "cutoff": cutoff,
            "ready_at_ist": ready_at_ist,
            "updated_epoch": cycle,
        }
        write_readiness(readiness)
        if not args.no_firebase and cycle - readiness_published >= 15:
            readiness_published = cycle
            publish_readiness(readiness, firebase=True)
        payload["autopilot_status"] = readiness["status"]
        payload["autopilot_green"] = readiness["green"]
        # The same six checks travel through the SSE payload, not only Firebase.
        payload["autopilot"] = readiness
        hud["sniper_trades"] = guard.trades
        hud["daily_lock"] = guard.daily_lock
        hud["sniper_stats"] = guard.stats()
        hud["expectancy"] = expectancy_stats(
            ledger.trades, month=today_ist()[:7],
            starting_capital=float(hud.get("starting_capital") or DEFAULT_CAPITAL),
            profit_vault=float(hud.get("profit_vault") or 0.0),
        )
        hud["tradable_capital"] = round(
            float(hud.get("capital") or DEFAULT_CAPITAL)
            - float(hud.get("profit_vault") or 0.0), 2,
        )
        guard.save(RISK_FILE)
        hud["scalper_trades"] = house_money.trades
        hud["scalper_vault"] = house_money.vault
        hud["house_money_pool"] = house_money.house_money_pool
        hud["scalper_realized"] = house_money.realized
        hud["scalper_next_qty"] = house_money.next_qty
        hud["scalper_status"] = house_money.status(now_ist_dt)
        hud["scalper_cooldown"] = house_money.cooldown_remaining(now_ist_dt)
        payload["scalp_sleeve"] = scalp_sleeve.snapshot(
            main_sniper_total_pnl_today=float(
                (hud.get("sniper_stats") or {}).get("realized") or 0.0
            ),
            engine_state=scalp_sleeve.engine_state,
        )
        last_option.update(payload["option"])
        last_trade.update(payload["trade"])
        last_scanner.update(payload["scanner"])
        HEARTBEAT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        if edge:
            edge.notify_tick()
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
                "expiry_hero": payload["expiry_hero"],
                "scalper": payload["scalper"],
                "scalp_sleeve": payload["scalp_sleeve"],
                "parallel_alpha": payload.get("parallel_alpha") or {},
                "hud": hud,
                "socket": {"rate": payload["tick_rate"], "connected": socket.streaming()},
                "cutoff": cutoff,
                "autopilot_status": readiness["status"],
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
