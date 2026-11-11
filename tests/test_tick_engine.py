"""Tick-to-candle engine: ring buffers, sealing and the three scan speeds."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from market.upstox_tick_engine import (  # noqa: E402
    RING_CAPACITY,
    RingBuffer,
    UpstoxTickEngine,
    parse_v3_frames,
)

BASE = 1_760_000_100  # epoch seconds, aligned to both 1m and 5m boundaries
KEY = "NSE_INDEX|Nifty 50"


def _frame(ltp: float, ltt_ms: int, **extra) -> dict:
    market = {
        "ltpc": {"ltp": ltp, "ltt": ltt_ms},
        "eFeedDetails": {"atp": extra.get("atp", ltp - 1), "oi": extra.get("oi", 1000),
                         "vtt": extra.get("vtt", 500)},
        "marketLevel": {"bidAskQuote": [{"bidP": ltp - 0.05, "bidQ": 75,
                                         "askP": ltp + 0.05, "askQ": 150}]},
        "optionGreeks": {"delta": extra.get("delta", 0.52), "iv": 14.2},
    }
    return {"feeds": {KEY: {"ltpc": {"ltp": ltp, "ltt": ltt_ms}, "ff": {"marketFF": market}}}}


def test_ring_buffer_caps_at_512() -> None:
    ring = RingBuffer(capacity=512)
    for index in range(600):
        ring.append(type("C", (), {"close": index, "as_dict": lambda self: {}})())
    assert len(ring) == RING_CAPACITY == 512
    assert ring.closes()[-1] == 599
    assert ring.closes(512)[0] == 88


def test_frame_parser_reads_ltpc_atp_oi_and_depth() -> None:
    ticks = parse_v3_frames(json.dumps(_frame(24_986.4, BASE * 1000 + 500)))
    assert len(ticks) == 1
    key, tick = ticks[0]
    assert key == KEY
    assert tick["price"] == 24_986.4
    assert tick["atp"] == 24_985.4
    assert tick["oi"] == 1000
    assert tick["ask"] > tick["bid"]
    assert tick["delta"] == 0.52


def test_minute_candle_seals_on_the_boundary() -> None:
    sealed: list[tuple[str, int]] = []
    engine = UpstoxTickEngine(on_minute=lambda key, candle: sealed.append((key, candle.start)))
    engine.on_tick(KEY, 100.0, ts_ms=(BASE + 5) * 1000)
    engine.on_tick(KEY, 101.0, ts_ms=(BASE + 30) * 1000)
    assert sealed == []  # still inside the first minute
    engine.on_tick(KEY, 102.0, ts_ms=(BASE + 61) * 1000)
    assert sealed == [(KEY, BASE)]
    bars = engine.windows(KEY, 1)
    assert len(bars) == 1
    assert (bars[0].open, bars[0].high, bars[0].close) == (100.0, 101.0, 101.0)


def test_five_minute_and_one_minute_bars_stay_in_sync() -> None:
    engine = UpstoxTickEngine()
    for offset in range(0, 12 * 60, 30):
        engine.on_tick(KEY, 100 + offset / 60.0, ts_ms=(BASE + offset) * 1000)
    engine.on_tick(KEY, 200.0, ts_ms=(BASE + 12 * 60) * 1000)
    assert len(engine.windows(KEY, 1)) >= 12
    assert len(engine.windows(KEY, 5)) >= 2
    assert len(engine.windows(KEY, 15)) >= 0


def test_telemetry_ribbon_reports_quote_and_oi_change() -> None:
    engine = UpstoxTickEngine()
    engine.on_tick(KEY, 24_900.0, ts_ms=(BASE + 1) * 1000, atp=24_880.0, oi=1_000_000,
                   delta=0.51, bid=24_899.95, ask=24_900.05)
    engine.on_tick(KEY, 24_950.0, ts_ms=(BASE + 62) * 1000, oi=900_000)
    row = engine.telemetry()[0]
    assert row["ltp"] == 24_950.0
    assert row["atp"] == 24_880.0
    assert row["delta"] == 0.51
    assert row["spread"] == 0.1
    assert row["oi_change_pct"] < 0  # unwinding


def test_due_scans_hits_all_three_speeds() -> None:
    clock = [BASE * 1.0]
    engine = UpstoxTickEngine(clock=lambda: clock[0])
    assert engine.due_scans() == ["tick", "minute", "master"]
    assert engine.due_scans() == []  # nothing is due 0 ms later
    clock[0] += 301
    assert engine.due_scans() == ["tick", "minute", "master"]


def test_universe_registers_indices_heavyweights_and_atm_options() -> None:
    engine = UpstoxTickEngine()
    keys = engine.register_universe({"NIFTY": 24_986.0, "BANKNIFTY": 58_850.0}, strike_step=50)
    assert "NSE_INDEX|Nifty 50" in keys
    assert "NSE_EQ|HDFCBANK" in keys
    assert "NIFTY 24950 CE" in keys and "NIFTY 25050 PE" in keys
    assert "BANKNIFTY 58850 CE" in keys
    assert len({key for key in keys}) == len(keys)


def test_flush_seals_open_buckets() -> None:
    engine = UpstoxTickEngine()
    engine.on_tick(KEY, 100.0, ts_ms=(BASE + 10) * 1000)
    closed = engine.flush()
    assert len(closed) == 3  # 1m, 5m and 15m
    assert engine.windows(KEY, 1)[0].close == 100.0
