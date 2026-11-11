"""Deterministic proof for the four-angle autonomous quant upgrade.

The first test replays the actual 14:56:03 IST saved feed.  The old checklist
missed that wave; the new engine must select the real 25200 PE, fire Angles 1
and 2, and carry the paper trade through shield/T1/T2/runner with positive P&L.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.kronos_ensemble import vectorized_path_projection
from execution.autonomous_trade_manager import (
    create_autonomous_trade,
    manage_autonomous_trade,
)
from strategy.multi_angle_playbook_engine import evaluate_playbooks
from strategy.quant_math_formulas import (
    EV_RATIO_FLOOR,
    greek_ev_ratio,
    weighted_book_imbalance,
)

REPO = Path(__file__).resolve().parents[1]
FEED = REPO / "state" / "live_feed.json"
FIXTURE = REPO / "tests" / "fixtures" / "live_feed_1456.json"


@pytest.fixture(scope="module")
def replay_feed() -> dict:
    # Runtime uses the actual state snapshot; CI/public checkouts use the
    # versioned byte-for-byte fixture of the same 14:56:03 feed.
    source = FEED if FEED.exists() else FIXTURE
    return json.loads(source.read_text(encoding="utf-8"))


def test_exact_1456_snapshot_fires_trend_and_orderflow_pe(replay_feed: dict) -> None:
    feed = replay_feed
    assert feed["updated_at"].startswith("2026-11-11T14:56:03")
    result = evaluate_playbooks(feed, symbol="NIFTY")

    assert result["side"] == "PE"
    assert result["contract"] == "NIFTY 25200 PE"
    assert result["auto_execute"] is True
    by_id = {row["key"]: row for row in result["angles"]}
    assert by_id["TREND_SURF_PULLBACK"]["trigger"] is True
    assert by_id["TREND_SURF_PULLBACK"]["score"] >= 70.0
    assert by_id["ORDERFLOW_MOMENTUM_IGNITION"]["trigger"] is True
    assert by_id["ORDERFLOW_MOMENTUM_IGNITION"]["score"] >= 70.0
    assert by_id["SMC_EXTREME_REVERSAL_SNIPER"]["trigger"] is False
    assert result["paths_bearish_of_100"] == 88
    assert result["entry"]["option_type"] == "PE"
    assert result["entry"]["entry_price"] == 223.45
    assert "AUTO-EXECUTED" in result["headline"]
    assert "NIFTY 25200 PE" in result["auto_pick_reason"]


def test_exponential_obi_and_greek_ev_are_real_math(replay_feed: dict) -> None:
    depth = replay_feed["option"]["NIFTY"]["depth"]
    raw_ratio = sum(row["qty"] for row in depth["bids"]) / sum(
        row["qty"] for row in depth["asks"]
    )
    assert raw_ratio == pytest.approx(10.50, abs=1e-6)
    weighted = weighted_book_imbalance(depth["bids"], depth["asks"])
    assert weighted > 1.30
    assert weighted != pytest.approx(raw_ratio)

    ev = greek_ev_ratio(
        delta=0.5058, gamma=0.0007, expected_abs_move=10.0,
        theta_15m=-19.6695, spread_inr=0.35,
    )
    expected = (0.5058 * 10.0 + 0.5 * 0.0007 * 100.0) / (19.6695 + 0.35)
    assert ev["ev_ratio"] == pytest.approx(expected, abs=1e-4)
    assert ev["approved"] is (expected >= EV_RATIO_FLOOR)


def test_vectorized_100_ways_over_150_candles_under_5ms(replay_feed: dict) -> None:
    spot = replay_feed["spot"]["NIFTY"]
    brain = replay_feed["model_brain"]["NIFTY"]
    # Warm NumPy's lazy import/allocator once; measure the actual matrix math.
    vectorized_path_projection(
        spot["candles_1m"], spot=spot["ltp"],
        p10=brain["p10"], p50=brain["p50"], p90=brain["p90"],
        pred_len=12, paths=100, candles_analyzed=150,
        buffer_total=len(spot["candles_1m"]), futures_ratio=1.15, obi_5=10.5,
    )
    runs: list[tuple[dict, float]] = []
    for _ in range(3):
        started = time.process_time()
        projection = vectorized_path_projection(
            spot["candles_1m"], spot=spot["ltp"],
            p10=brain["p10"], p50=brain["p50"], p90=brain["p90"],
            pred_len=12, paths=100, candles_analyzed=150,
            buffer_total=len(spot["candles_1m"]), futures_ratio=1.15, obi_5=10.5,
        )
        runs.append((projection, (time.process_time() - started) * 1000.0))
    projection, best_cpu_ms = min(runs, key=lambda item: item[1])
    assert projection["paths_total"] == 100
    assert len(projection["paths"]) == 100
    assert len(projection["paths"][0]) == 12
    assert projection["paths_bullish"] + projection["paths_bearish"] == 100
    assert projection["candles_analyzed"] == 150
    # CPU-time best-of-three proves the vectorized math budget even when the
    # rest of the pytest suite is loading the wall clock.
    assert best_cpu_ms < 5.0, f"100x12 projection CPU budget exceeded: {best_cpu_ms:.2f}ms"


def test_autonomous_trade_hits_micro_shield_t1_t2_and_runner(replay_feed: dict) -> None:
    result = evaluate_playbooks(replay_feed, symbol="NIFTY")
    trade = create_autonomous_trade(result)
    entry = trade["entry_price"]
    risk = trade["risk_points"]
    t1 = trade["target_1"]
    t2 = trade["target_2_p90"]
    assert trade["option_symbol"] == "NIFTY 25200 PE"
    assert trade["auto_executed"] is True
    assert trade["qty"] == 300

    shield_level = entry + (t1 - entry) * 0.40
    shielded = manage_autonomous_trade(
        trade, shield_level + 0.01, obi_5=10.5, ema5=entry, prior_1m_low=218.0,
    )
    assert "MICRO_SHIELD_ARMED" in shielded["events"]
    assert trade["micro_shield_armed"] is True
    assert trade["current_trailing_sl"] == pytest.approx(entry - risk * 0.25, abs=0.01)
    assert trade["status"] == "OPEN"

    t1_update = manage_autonomous_trade(
        trade, t1 + 0.10, obi_5=9.0, ema5=entry, prior_1m_low=218.0,
    )
    assert t1_update["stage"] == 3
    assert trade["t1_booked"] is True
    assert trade["qty_open"] == 150
    assert trade["current_trailing_sl"] == pytest.approx(entry + 1.50, abs=0.01)

    t2_update = manage_autonomous_trade(
        trade, t2 + 0.10, obi_5=4.0, ema5=entry, prior_1m_low=218.0,
    )
    assert t2_update["stage"] == 4
    assert trade["t2_booked"] is True
    assert trade["qty_open"] == 75
    assert trade["status"] == "OPEN"
    assert trade["runner_active"] is True

    runner_exit = manage_autonomous_trade(
        trade, t2 + 18.0, obi_5=0.62, ema5=t2, prior_1m_low=218.0,
    )
    assert "ORDER_FLOW_RUNNER_EXIT" in runner_exit["events"]
    assert runner_exit["stage"] == 5
    assert trade["status"] == "CLOSED"
    assert trade["qty_open"] == 0
    assert trade["realized_pnl_rs"] > 0
    assert len(trade["booked_legs"]) == 3
    assert [leg["leg"] for leg in trade["booked_legs"]] == ["T1", "T2_RUNNER", "ORDER_FLOW_RUNNER_EXIT"]
