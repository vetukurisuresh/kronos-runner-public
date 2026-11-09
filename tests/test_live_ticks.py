from __future__ import annotations

import json
import time

from market import live_ticks
from market.option_picker import choose_side, gear_update, new_trade, pick_atm


def test_pick_atm_and_side() -> None:
    rows = [{"strike_price": 24950}, {"strike_price": 25000}, {"strike_price": 25050}]
    assert pick_atm(rows, 25012)["strike_price"] == 25000
    assert pick_atm(rows, 25040)["strike_price"] == 25050
    assert choose_side(4, 6) == "CE"
    assert choose_side(1, 6) == "PE"
    assert choose_side(4, 6, "BUY_PE") == "PE"


def test_gears_never_move_stop_down() -> None:
    trade = new_trade(100.0, 5.0)
    assert trade["initial_sl"] == 95.0
    gear_update(trade, 106.0)          # +1.2R -> breakeven
    assert trade["tsl_gear"] == 2 and trade["current_trailing_sl"] == 100.0
    gear_update(trade, 111.0, low_5m=108.0)   # +2.2R -> candle-low trail
    assert trade["tsl_gear"] == 3 and trade["current_trailing_sl"] == 108.0
    gear_update(trade, 104.0, low_5m=101.0)   # pullback must not lower it
    assert trade["current_trailing_sl"] == 108.0
    gear_update(trade, 116.0)          # +3.2R -> 1R trail
    assert trade["tsl_gear"] == 4 and trade["current_trailing_sl"] == 111.0
    assert trade["live_pnl_rs"] == 4800


def test_overlay_coordinates_inside_plot() -> None:
    trade = {"entry_price": 120.0, "current_trailing_sl": 108.0,
             "target_1": 140.0, "target_2_p90": 160.0, "qty": 300, "tsl_gear": 2}
    lines = live_ticks.overlay_lines(trade, height=400, top=40, bottom=360, lo=100.0, hi=170.0)
    assert [row["key"] for row in lines] == ["entry", "sl", "target1", "target2"]
    assert all(40 <= row["y"] <= 360 for row in lines)
    # Higher price = smaller y (canvas coordinates).
    assert lines[0]["y"] > lines[2]["y"]


def test_snapshot_reads_live_feed(tmp_path, monkeypatch) -> None:
    feed = {
        "updated_epoch": time.time(),
        "socket_connected": True,
        "tick_rate": 6.5,
        "socket_frames": 100,
        "socket_last_frame_age": 0.1,
        "prices": {"NSE_INDEX|Nifty 50": 25005.5},
        "spot": {"NIFTY": {"ltp": 25005.5, "change": 12.0, "pct_change": 0.05,
                           "vwap": 24990.0, "atr": 18.0,
                           "current_candle": {"time": 1, "open": 25000, "high": 25010,
                                              "low": 24995, "close": 25005.5, "volume": 10},
                           "current_candle_5m": {"time": 1794214200, "open": 24998,
                                                 "high": 25004, "low": 24998,
                                                 "close": 25001, "volume": 42},
                           "candles_1m": []}},
        "option": {"NIFTY": {"strike_symbol": "NIFTY 25000 CE", "ltp": 116.0, "atp_vwap": 110.0}},
        "trade": {"NIFTY": {"entry_price": 116.0, "current_trailing_sl": 108.0, "tsl_gear": 1}},
        "scanner": {"NIFTY": [{"key": "tick_speed", "pass": True, "value": "6.5 t/s"}]},
        "hud": {"starting_capital": 100000.0, "realized_today": 8500.0,
                "capital": 108500.0, "trades_today": 3},
    }
    path = tmp_path / "live_feed.json"
    path.write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FEED_FILE", path)

    snap = live_ticks.snapshot("NIFTY")
    assert snap["live"] is True
    assert snap["spot"]["ltp"] == 25005.5
    # The forming 5m bucket is passed through with the live tick folded into H/L.
    five = snap["spot"]["current_candle_5m"]
    assert five["time"] == 1794214200
    assert five["high"] == 25005.5 and five["low"] == 24998
    assert five["close"] == 25005.5
    assert snap["option"]["strike_symbol"] == "NIFTY 25000 CE"
    assert snap["option"]["zone"] == "ABOVE"
    assert snap["active_trade"]["entry_price"] == 116.0
    assert snap["hud"]["capital"] == 108500.0
    assert snap["hud"]["trades_today"] == 3
    assert snap["scanner_xray"][0]["key"] == "tick_speed"
    assert 0 < snap["spot"]["countdown_sec"]["bar_5m"] <= 300


def test_countdown_bounds() -> None:
    values = live_ticks.countdown(now=1_700_000_123)
    assert 1 <= values["bar_5m"] <= 300
    assert 1 <= values["bar_1m"] <= 60
