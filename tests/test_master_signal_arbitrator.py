"""Focused contracts for the visual candle engine and the master arbitrator."""

from __future__ import annotations

import time

from strategy.candle_visual_classifier import classify_candle_visuals
from strategy.master_signal_arbitrator import (
    SCALP_SLEEVE_RS,
    SNIPER_SLEEVE_RS,
    build_master_arbitrator,
    edge_score,
)


def _bar(t: int, o: float, h: float, l: float, c: float) -> dict:
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 100}


def test_four_base_styles_and_hollow_continuation() -> None:
    bars = [
        _bar(1, 95, 101, 94, 100),
        _bar(2, 100, 103, 99, 102),   # higher close, bullish -> hollow emerald
        _bar(3, 100.5, 103, 100, 101.5),  # bullish but weaker close -> filled emerald
        _bar(4, 101.5, 102, 97, 98),      # lower close, bearish -> hollow coral
        _bar(5, 99, 100, 96, 98.5),       # bearish close, stronger than prev -> filled coral
    ]
    out = classify_candle_visuals(bars, p10=0, p90=0, feed_live=True)
    assert [row["style"] for row in out["candles"][-4:]] == [
        "HOLLOW_EMERALD", "FILLED_EMERALD", "HOLLOW_CORAL", "FILLED_CORAL",
    ]
    assert out["reason"] == "CANDLE_VISUALS_READY"


def test_bull_reversal_is_conjunctive_and_live_only() -> None:
    bars = [
        _bar(1, 100, 101, 99, 100),
        _bar(2, 95, 99, 89, 98),
    ]
    live = classify_candle_visuals(
        bars, p10=90, p90=120, obi_5=1.25, tbq_tsq=1.4, smc={}, feed_live=True,
    )
    last = live["candles"][-1]
    assert last["style"] == "BULL_REVERSAL"
    assert last["marker"] == "▲ EARLY BULL REVERSAL (BUY CE)"
    assert live["bull_reversal_armed"] is True
    assert live["action_lines"][-1]["side"] == "CE"

    stale = classify_candle_visuals(
        bars, p10=90, p90=120, obi_5=1.25, tbq_tsq=1.4, smc={}, feed_live=False,
    )
    assert stale["bull_reversal_armed"] is False
    assert stale["candles"][-1]["is_reversal"] is False
    assert stale["reason"] == "CANDLE_VISUALS_STALE"


def test_edge_score_is_multiplicative_and_clamped() -> None:
    perfect = edge_score(2.5, 100, 1.0)
    assert perfect["edge_score"] == 100.0
    assert perfect["roi_factor"] == 1.0
    assert perfect["confidence_factor"] == 1.0
    assert perfect["flow_factor"] == 1.0
    assert edge_score(1.25, 80, 0.5)["edge_score"] == 20.0


def test_arbitrator_blocks_fixture_and_zero_feed() -> None:
    now = time.time()
    fixture = {
        "fixture": True,
        "updated_epoch": now,
        "spot": {"NIFTY": {"ltp": 25000}},
    }
    out = build_master_arbitrator(fixture, now=now, feed_live=True)
    assert out["ok"] is False
    assert out["data_gate"]["reason"] == "FIXTURE_BLOCKED"
    assert out["sleeve_b"]["reason"] == "BLOCKED_BY_DATA_GATE"
    assert out["sleeves"]["B"]["capital_rs"] == SNIPER_SLEEVE_RS
    assert out["sleeves"]["A"]["capital_rs"] == SCALP_SLEEVE_RS

    empty = build_master_arbitrator({}, now=now, feed_live=True)
    assert empty["data_gate"]["reason"] == "ZERO_OR_MISSING_MARKET_DATA"


def test_arbitrator_reports_fresh_live_gate() -> None:
    now = time.time()
    feed = {
        "updated_epoch": now,
        "spot": {"NIFTY": {"ltp": 25000}},
        "full_spectrum": {"NIFTY": {"spot": 25000, "fixture": False}},
    }
    out = build_master_arbitrator(feed, now=now, feed_live=True)
    assert out["data_gate"]["allowed"] is True
    assert out["data_gate"]["reason"] == "SANITIZED_LIVE_FEED"
    assert out["reason"] == "ARBITRATION_ACTIVE"
    assert len(out["sleeve_b"]["candidates"]) == 3
    assert len(out["sleeve_a"]["candidates"]) == 3
