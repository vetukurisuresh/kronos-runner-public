from __future__ import annotations

import json
import time

import pytest

from execution.pro_money_manager import book_partials, close_remaining, init_partial_state
from market import live_ticks
from market.option_picker import choose_watch_side, contract_label, new_trade
from model.live_predictor import quantile_probability


def test_bearish_flow_selects_pe_and_bullish_selects_ce() -> None:
    assert choose_watch_side(downside=72, upside=28, pcr=0.65,
                             spot=25_180, vwap=25_220, obi_5=0.70) == "PE"
    assert choose_watch_side(upside=74, downside=26, pcr=1.18,
                             spot=25_240, vwap=25_200, obi_5=1.30) == "CE"
    assert choose_watch_side(manual="PE", upside=80, downside=20) == "PE"
    assert choose_watch_side(active_trade={"status": "OPEN", "option_type": "CE"},
                             manual="PE") == "CE"
    assert contract_label("NIFTY", 0, "PE") == "NIFTY ATM PE"
    assert contract_label("NIFTY", 25_200, "CE") == "NIFTY 25200 CE"


def test_quantile_probability_is_continuous_not_binary() -> None:
    bear = quantile_probability(25_100, 25_000, 25_050, 25_100)
    mid = quantile_probability(25_050, 25_000, 25_050, 25_100)
    bull = quantile_probability(25_050, 25_000, 25_100, 25_200)
    assert bear["upside_pct"] == 2.0
    assert mid["upside_pct"] == 50.0
    assert bull["upside_pct"] == 74.0
    assert bear["source"] == "QUANTILE_CDF"
    # A 2-path 0/2 vote must not collapse to 0% when spot is inside the cone.
    assert quantile_probability(25_050, 25_000, 25_050, 25_100,
                                paths_up=0, samples=2)["upside_pct"] == 50.0


def test_idle_levels_do_not_move_and_trade_levels_are_frozen(monkeypatch, tmp_path) -> None:
    now = time.time()
    bars = [{"t": 1_760_000_000 + i * 300, "o": 25_100, "h": 25_120,
             "l": 25_090, "c": 25_110, "v": 100} for i in range(6)]
    feed = {
        "updated_epoch": now, "updated_at": "2026-11-11T10:30:00+05:30",
        "market_open": True, "socket_connected": True,
        "prices": {"NSE_INDEX|Nifty 50": 25_115},
        "spot": {"NIFTY": {"ltp": 25_115, "vwap": 25_100, "atr": 20,
                            "candles_5m": bars, "candles_1m": bars,
                            "candles_15m": bars}},
        "option": {"NIFTY": {"strike_symbol": "NIFTY 25100 CE", "ltp": 120,
                              "atp_vwap": 118}},
        "full_spectrum": {"NIFTY": {"generated_epoch": now, "depth": {"obi_5": 0.8}}},
    }
    feed_file = tmp_path / "live_feed.json"
    feed_file.write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FEED_FILE", feed_file)
    monkeypatch.setattr(live_ticks, "FAST_FILE", tmp_path / "live_fast.json")
    monkeypatch.setattr(live_ticks, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(live_ticks, "OPTION_PIN_FILE", tmp_path / "pin.json")
    first = live_ticks.snapshot("NIFTY", now=now)
    assert first["live"] is True
    assert first["chart_levels"]["levels_state"] == "IDLE_STATIC"
    for key in ("entry", "sl", "t1", "t2"):
        assert key not in first["chart_levels"]
    anchor = first["chart_levels"]["trigger_zone"]

    feed["spot"]["NIFTY"]["ltp"] = 25_130
    feed["prices"]["NSE_INDEX|Nifty 50"] = 25_130
    feed_file.write_text(json.dumps(feed), encoding="utf-8")
    second = live_ticks.snapshot("NIFTY", now=now + 0.2)
    assert second["chart_levels"]["trigger_zone"] == anchor
    assert "entry" not in second["chart_levels"]

    open_trade = {"status": "OPEN", "entry_price": 120, "current_trailing_sl": 112,
                  "target_1": 132, "target_2_p90": 145}
    trade = new_trade(120, 5)
    trade["current_trailing_sl"] = 112
    trade["target_1"] = 132
    trade["target_2_p90"] = 145
    assert open_trade["entry_price"] == trade["entry_price"]


def test_t1_then_breakeven_then_t2_and_manual_close() -> None:
    trade = new_trade(100, 5, qty=300)
    init_partial_state(trade)
    legs = book_partials(trade, 107.5)
    assert legs[0]["leg"] == "T1"
    assert legs[0]["qty"] == 150
    assert trade["current_trailing_sl"] == 101.5
    assert trade["qty_open"] == 150 and trade["status"] == "OPEN"
    legs = book_partials(trade, 116.0)
    assert legs[0]["leg"] == "T2"
    assert trade["status"] == "CLOSED"
    assert trade["realized_pnl_rs"] == pytest.approx(3_375.0)  # T2 is 3R = 115, not 116

    manual = new_trade(100, 5, qty=300)
    init_partial_state(manual)
    leg = close_remaining(manual, 103.0, reason="MANUAL_CLOSE")
    assert leg["leg"] == "MANUAL_CLOSE"
    assert manual["status"] == "CLOSED" and manual["qty_open"] == 0
def test_lkg_fast_overlay_keeps_gate_green_without_faking_chain_age(monkeypatch, tmp_path) -> None:
    now = time.time()
    dynamic = tmp_path / "live_fast.json"
    dynamic.write_text(json.dumps({
        "epoch": now, "socket_connected": True, "socket_frames": 10,
        "tick_rate": 6.5, "tick_seq": 10,
        "spot": {"NIFTY": {"ltp": 25_200}},
        "option": {"NIFTY": {"strike_symbol": "NIFTY 25200 PE", "ltp": 140}},
        "depth": {"NIFTY": {"obi_5": 0.82}},
    }), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FAST_FILE", dynamic)
    payload = {
        "ts": now - 9, "age_ms": 9_000, "live": False,
        "data_gate": {"live": False, "allowed": False, "status": "BLOCKED"},
        "entries_suspended": True,
        "spot": {"NIFTY": {"ltp": 25_190, "display": 25_190}},
        "option": {}, "indices": {}, "master_arbitrator": {},
    }
    patched = live_ticks.fast_overlay(payload, now=now)
    assert patched["data_gate"]["live"] is True
    assert patched["entries_suspended"] is False
    assert patched["spot"]["NIFTY"]["ltp"] == 25_200
    assert patched["age_ms"] <= 10