"""Proof tests for the 2-of-3 quorum, intra-5m entry and auto regime switch.

The runtime replay uses the exact 14:56:03 saved feed.  CI/public checkouts use
the versioned byte-for-byte fixture.  The completed human-close proof uses an
earlier 5m candle from that same recorded feed, so both prices are observed
market data rather than a fabricated example.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from execution.autonomous_trade_manager import (
    _book_t2,
    create_autonomous_trade,
    manage_autonomous_trade,
)
from model.tri_model_orchestrator import build_model_quorum
from strategy.intra_candle_sniper import IntraCandleEarlyDetector
from strategy.regime_auto_switcher import evaluate_market_regime

REPO = Path(__file__).resolve().parents[1]
FEED = REPO / "state" / "live_feed.json"
FIXTURE = REPO / "tests" / "fixtures" / "live_feed_1456.json"


@pytest.fixture(scope="module")
def replay_feed() -> dict:
    source = FEED if FEED.exists() else FIXTURE
    return json.loads(source.read_text(encoding="utf-8"))


def _one_minute(feed: dict, stamp: float) -> dict:
    for row in feed["option"]["NIFTY"]["candles_1m"]:
        if int(row["time"]) == int(stamp):
            return row
    raise AssertionError(f"missing actual option 1m bar at {stamp}")


def _spot_one_minute(feed: dict, stamp: float) -> dict:
    for row in feed["spot"]["NIFTY"]["candles_1m"]:
        if int(row["t"]) == int(stamp):
            return row
    raise AssertionError(f"missing actual spot 1m bar at {stamp}")


def test_1456_quorum_confirms_two_of_three_without_futures_veto(replay_feed: dict) -> None:
    quorum = build_model_quorum(replay_feed, symbol="NIFTY")

    assert quorum["approved"] is True
    assert quorum["side"] == "PE"
    assert quorum["quorum_count"] == "3/3"
    assert quorum["confidence"] >= 60.0
    assert quorum["routing_gate"]["third_model_neutral_blocks"] is False
    assert quorum["routing_gate"]["futures_volume_1_15x_blocks"] is False
    assert quorum["entry"]["contract"] == "NIFTY 25200 PE"
    assert quorum["entry"]["entry_price"] == 223.45
    assert all(vote["confidence"] >= 62.0 for vote in quorum["votes"])
    assert {vote["role"] for vote in quorum["votes"]} == {"base", "small", "mini"}


def test_intra_5m_entry_is_recorded_inside_forming_candle(replay_feed: dict) -> None:
    quorum = build_model_quorum(replay_feed, symbol="NIFTY")
    detector = IntraCandleEarlyDetector()
    snapshot = detector.update(replay_feed, symbol="NIFTY", quorum=quorum)

    assert snapshot["in_window"] is True
    assert 5.0 <= snapshot["elapsed_sec"] <= 290.0
    assert snapshot["seconds_to_close"] > 200.0
    assert snapshot["ready"] is True
    assert snapshot["entry_price"] == 223.45
    assert snapshot["proof"]["contract"] == "NIFTY 25200 PE"
    assert snapshot["proof"]["kronos_entry_price"] == 223.45
    assert snapshot["proof"]["human_5m_close_price"] is None
    assert snapshot["proof"]["status"] == "WAITING_HUMAN_5M_CLOSE"
    assert len(detector.snapshot()) == 1


def test_human_close_proof_records_seconds_and_option_edge(replay_feed: dict) -> None:
    """Use an actual recorded up-candle: 14:11 IST entry, 14:15 IST human close."""
    candle_time = 1_794_385_500
    entry_stamp = candle_time + 60
    close_stamp = candle_time + 240
    option_entry = _one_minute(replay_feed, entry_stamp)["close"]
    option_close = _one_minute(replay_feed, close_stamp)["close"]
    spot_entry = _spot_one_minute(replay_feed, entry_stamp)["c"]
    five = next(row for row in replay_feed["spot"]["NIFTY"]["candles_5m"] if row["t"] == candle_time)

    detector = IntraCandleEarlyDetector()
    row = detector.record_entry(
        symbol="NIFTY",
        side="CE",
        contract="NIFTY 25200 CE",
        strike=25200,
        entry_price=option_entry,
        spot_entry=spot_entry,
        t_kronos=entry_stamp,
        entry_candle_time=candle_time,
        quorum_count="2/3",
        quorum_confidence=67.5,
        models=["base", "mini"],
        level=25166.05,
        elapsed_sec=60,
    )
    proof = detector.finalize_proof(
        row["proof_id"],
        human_5m_close_price=option_close,
        human_5m_close_spot=five["c"],
        human_t=candle_time + 300,
        source="actual_recorded_5m_close",
    )

    assert proof["seconds_ahead_of_human"] == 240.0
    assert proof["option_price_edge_inr"] == pytest.approx(round(option_close - option_entry, 2))
    assert proof["spot_price_edge_pts"] == pytest.approx(round(five["c"] - spot_entry, 2))
    assert proof["human_5m_close_price"] == option_close
    assert proof["status"] == "HUMAN_5M_CLOSE_RECORDED"
    assert proof["human_price_source"] == "actual_recorded_5m_close"


def test_regime_switcher_produces_both_execution_plans(replay_feed: dict) -> None:
    quorum = build_model_quorum(replay_feed, symbol="NIFTY")
    trendy = evaluate_market_regime(replay_feed, symbol="NIFTY", quorum=quorum)
    assert trendy["mode"] == "TRENDY_DIRECTIONAL_HOLD"
    assert trendy["score"] >= 65.0
    assert trendy["book_plan"]["t1_book_fraction"] == 0.40
    assert trendy["book_plan"]["t2_book_fraction"] == 0.30
    assert trendy["book_plan"]["runner_fraction"] == 0.30
    assert "ENTRY_PLUS_1_50" in trendy["book_plan"]["stop_to"]

    scalp_feed = copy.deepcopy(replay_feed)
    scalp_feed["tick_rate"] = 0.4
    scalp_feed["socket_frames"] = 0
    scalp_feed["model_oscillator"]["bullish_upside_prob_pct"] = 50.0
    scalp_feed["model_oscillator"]["bearish_downside_prob_pct"] = 50.0
    scalp_feed["full_spectrum"]["NIFTY"]["futures"]["ltp"] = 25_227.6
    scalp_feed["full_spectrum"]["NIFTY"]["futures"]["atp"] = 25_227.6
    flat = {"time": 1_794_389_100, "open": 25_174.0, "high": 25_174.1,
            "low": 25_173.9, "close": 25_174.0, "volume": 0.0}
    scalp_feed["spot"]["NIFTY"]["current_candle_5m"] = flat
    for row in scalp_feed["spot"]["NIFTY"]["candles_5m"][-12:]:
        row.update({"o": 25_174.0, "h": 25_174.1, "l": 25_173.9, "c": 25_174.0})
    scalp = evaluate_market_regime(scalp_feed, symbol="NIFTY", quorum=quorum)
    assert scalp["mode"] == "HIGH_ACCURACY_SCALPING"
    assert scalp["score"] < 65.0
    assert scalp["book_plan"]["t1_book_fraction"] == 0.70
    assert scalp["book_plan"]["stop_to"] == "COST"
    assert scalp["book_plan"]["runner_fraction"] == 0.0


def _quorum_trade(book_plan: dict) -> dict:
    playbook = {
        "auto_execute": True,
        "side": "CE",
        "winner_id": "QUORUM_2_OF_3",
        "winner": {"score": 68.0, "short": "2-of-3 Model Quorum", "index": 5},
        "entry": {
            "entry_price": 100.0,
            "risk_points": 5.0,
            "target_1": 108.0,
            "target_2": 116.0,
            "stop_loss": 95.0,
            "strike": 25200,
            "option_type": "CE",
            "contract": "NIFTY 25200 CE",
            "book_plan": book_plan,
        },
    }
    trade = create_autonomous_trade(playbook, qty=300)
    trade["book_plan"] = dict(book_plan)
    return trade


def test_trend_regime_books_40_30_and_holds_30_runner() -> None:
    plan = {
        "t1_book_fraction": 0.40, "t2_book_fraction": 0.30,
        "runner_fraction": 0.30, "stop_to": "ENTRY_PLUS_1_50",
        "runner_hold_obi": 0.80,
    }
    trade = _quorum_trade(plan)
    first = manage_autonomous_trade(trade, 108.0, obi_5=1.4, ema5=100.0, prior_1m_low=96.0)
    assert trade["t1_booked"] is True
    assert trade["qty_open"] == 180
    assert trade["current_trailing_sl"] >= 101.50
    assert first["events"][-1].startswith("T1_40PCT_BOOKED")

    second = manage_autonomous_trade(trade, 116.0, obi_5=1.2, ema5=105.0, prior_1m_low=100.0)
    assert trade["t2_booked"] is True
    assert trade["runner_active"] is True
    assert trade["qty_open"] == 90
    assert trade["realized_pnl_rs"] == pytest.approx(960.0 + 16.0 * 90)
    assert any("T2_RUNNER" in event for event in second["events"])


def test_scalp_regime_books_70_then_closes_30_at_t2() -> None:
    plan = {
        "t1_book_fraction": 0.70, "t2_book_fraction": 0.30,
        "runner_fraction": 0.0, "stop_to": "COST",
        "runner_hold_obi": 0.70,
    }
    trade = _quorum_trade(plan)
    first = manage_autonomous_trade(trade, 108.0, obi_5=1.4, ema5=100.0, prior_1m_low=96.0)
    assert trade["t1_booked"] is True
    assert trade["qty_open"] == 90
    assert trade["current_trailing_sl"] >= 100.0
    assert first["events"][-1].startswith("T1_70PCT_BOOKED")

    second = manage_autonomous_trade(trade, 116.0, obi_5=1.2, ema5=105.0, prior_1m_low=100.0)
    assert trade["t2_booked"] is True
    assert trade["qty_open"] == 0
    assert trade["status"] == "CLOSED"
    assert trade["runner_active"] is False
    assert trade["realized_pnl_rs"] == pytest.approx(1680.0 + 16.0 * 90)
    assert any("T2_SCALP" in event for event in second["events"])
