"""Top-5% institutional sniper strategy tests.

Covers the fail-closed full-spectrum gate, the five-signature bottom/top
reversal detectors, the Greek Taylor targets, the 2-SL circuit breaker and the
50% profit-vault sweep.
"""

from __future__ import annotations

import time

import pytest

from execution.pro_money_manager import (
    SniperRiskGuard,
    book_partials,
    expectancy_stats,
)
from market.option_picker import new_trade
from strategy.full_spectrum_alpha import (
    MISSING_OR_STALE_REASON,
    build_snapshot,
    evaluate,
    field_coverage,
    zero_chop_gate,
)
from strategy.greek_target_mapper import interpolate_p65, map_greek_targets
from strategy.reversal_sniper import (
    checklist_label,
    detect_bottom_reversal,
    detect_top_reversal,
)


def _chain(count: int = 11, *, ce_oi: float = 1_200, pe_oi: float = 1_500) -> list[dict]:
    return [
        {
            "strike_price": 24_950 + index * 10,
            "call_options": {"market_data": {"oi": ce_oi, "oi_change": -30, "ltp": 120.0}},
            "put_options": {"market_data": {"oi": pe_oi, "oi_change": 40, "ltp": 110.0}},
        }
        for index in range(count)
    ]


def _levels(count: int = 5) -> tuple[list[dict], list[dict]]:
    bids = [{"price": 24_999.5 - index * 0.5, "qty": 100 + index * 10} for index in range(count)]
    asks = [{"price": 25_000.5 + index * 0.5, "qty": 90 + index * 10} for index in range(count)]
    return bids, asks


def _snapshot(**overrides):
    bids, asks = _levels(5)
    base = dict(
        symbol="NIFTY",
        spot=25_000,
        futures={"ltp": 25_020, "atp": 25_010, "volume": 120_000,
                 "amount": 3_000_000_000, "oi": 2_000_000},
        depth={"bids": bids, "asks": asks, "obi_5": 1.4,
               "bid_qty_5": 600, "ask_qty_5": 500,
               "bid_qty_top": 100, "ask_qty_top": 90,
               "tbq": 1_800, "tsq": 1_300, "tbq_tsq": 1.38},
        chain=_chain(),
        greeks={"delta": 0.52, "gamma": 0.002, "theta": -4.2,
                "vega": 8.1, "rho": 0.03, "iv": 14.2},
        vix={"ltp": 13.4, "change_pct": 1.2, "direction": "RISING"},
        breadth={"aligned": 4, "total": 5, "rows": []},
        market_ohlc={
            "1m": {"open": 24_990, "high": 25_010, "low": 24_980, "close": 25_000},
            "30m": {"open": 24_950, "high": 25_020, "low": 24_940, "close": 25_000},
            "1d": {"open": 24_800, "high": 25_100, "low": 24_750, "close": 25_000},
        },
        previous_basis=18.0,
        iv_velocity_5m=0.4,
        gamma_velocity_5m=0.1,
        kronos_move=15.0,
        side="CE",
        as_of="2026-11-10T10:00:00+05:30",
        generated_epoch=time.time(),
        paths_up=4,
        samples=6,
        order_flow_divergence=True,
    )
    base.update(overrides)
    return build_snapshot(**base)


# -- fail-closed full-spectrum gate ---------------------------------------- #
def test_missing_or_stale_data_blocks_with_exact_reason() -> None:
    decision = evaluate(
        chain=[], spot=0, futures_ltp=0, futures_volume=0, depth={}, vix=0,
        stale=True, age_ms=7_000,
    )
    assert decision["allow_entry"] is False
    assert decision["conviction"] == "BLOCKED"
    assert decision["reason"] == MISSING_OR_STALE_REASON == "STALE_FEED"
    assert decision["block_reason"] == MISSING_OR_STALE_REASON
    assert "stale" in decision["missing_fields"]
    assert decision["structure_ok"] is False

    snap = _snapshot(chain=[], spot=0, futures={"ltp": 0, "volume": 0},
                     greeks={}, vix={"ltp": 0}, stale=True, age_ms=8_000)
    assert snap["alpha"]["allow_entry"] is False
    assert snap["alpha"]["reason"] == MISSING_OR_STALE_REASON
    assert snap["stale"] is True
    assert snap["coverage"]["pct"] < 100.0


def test_four_depth_levels_and_ten_strikes_are_not_full_coverage() -> None:
    bids, asks = _levels(4)
    snap = _snapshot(depth={"bids": bids, "asks": asks, "obi_5": 1.4,
                            "bid_qty_5": 500, "ask_qty_5": 400,
                            "bid_qty_top": 100, "ask_qty_top": 90,
                            "tbq": 1_800, "tsq": 1_300, "tbq_tsq": 1.38},
                     chain=_chain(10))
    assert snap["coverage"]["pct"] < 100.0
    assert "depth.bids" in snap["coverage"]["missing"]
    assert "pcr.valid_strikes" in snap["coverage"]["missing"]
    assert snap["alpha"]["allow_entry"] is False
    assert snap["alpha"]["reason"] == "MISSING_CHAIN"
    assert "depth" in snap["alpha"]["missing_structures"]
    assert "option_chain" in snap["alpha"]["missing_structures"]


def test_zero_chop_gate_locks_sideways_and_flow_divergence_unlocks() -> None:
    locked = zero_chop_gate(paths_up=50, samples=100, obi_5=1.0, tbq_tsq=1.0)
    assert locked["locked"] is True
    assert locked["status"] == "NO_TRADE_CAPITAL_PROTECTED"

    unlocked = zero_chop_gate(paths_up=50, samples=100, obi_5=1.7, tbq_tsq=1.4)
    assert unlocked["locked"] is False
    assert unlocked["status"] == "TRADE_ALLOWED"

    directional = zero_chop_gate(paths_up=88, samples=100)
    assert directional["locked"] is False


# -- bottom / top reversal sniper ----------------------------------------- #
def test_bottom_reversal_ce_triggers_with_6_50_sl_when_all_five_align() -> None:
    signal = detect_bottom_reversal(
        spot=24_948, p10=24_950, paths_up=88, samples=100,
        futures_volume_ratio=3.0, price_velocity=-0.2,
        velocity_stall_threshold=0.5,
        obi_5=1.62, tbq=145, tsq=100,
        pe_oi_change_5m=1_250, ce_oi_change_5m=-400,
        entry=100.0, absorption_low=93.5,
    )
    assert signal["trigger"] is True
    assert signal["side"] == "CE"
    assert signal["sl_points"] == pytest.approx(6.50)
    assert signal["stop"] == pytest.approx(93.50)
    assert signal["signature_count"] == 5
    assert "5/5" in checklist_label(signal["checklist"])
    assert all(item["pass"] for item in signal["checklist"])
    assert signal["reason"] == "BOTTOM_REVERSAL_CE_CONFIRMED"


def test_bottom_reversal_skips_when_structural_sl_exceeds_nine() -> None:
    signal = detect_bottom_reversal(
        spot=24_948, p10=24_950, paths_up=88, samples=100,
        futures_volume_ratio=3.0, price_velocity=-0.2,
        velocity_stall_threshold=0.5, obi_5=1.62, tbq=145, tsq=100,
        pe_oi_change_5m=1_250, entry=100.0, absorption_low=90.0,
    )
    assert signal["trigger"] is False
    assert signal["sl_points"] == 9.0
    assert signal["structural_sl_points"] == 10.0


def test_top_reversal_exits_ce_and_triggers_pe_at_p90() -> None:
    signal = detect_top_reversal(
        spot=25_120, p90=25_100, basis=18.0, previous_basis=24.0,
        ce_iv_change_5m=-2.4, ce_tbq=100, ce_tsq=200, ce_obi_5=0.9,
        mtf={"alignment": "BEAR"},
        pe_obi_5=1.62, pe_tbq=140, pe_tsq=100,
        entry=80.0, absorption_high=87.0,
    )
    assert signal["peak_exit"] is True
    assert signal["close_ce"] is True
    assert signal["pe_trigger"] is True
    assert signal["action"] == "CLOSE_CE_AND_BUY_PE"
    assert signal["sl_points"] == pytest.approx(7.0)
    assert signal["stop"] == pytest.approx(87.0)


# -- Greek Taylor target mapper ------------------------------------------- #
def test_greek_t1_p65_and_t2_p90_taylor_targets() -> None:
    targets = map_greek_targets(
        entry=100.0, spot=25_000, p65=25_060, p90=25_120,
        delta=0.55, gamma=0.002, theta=-4.2, vega=8.1,
        expected_iv_change=0.5, dt_minutes=60, oi_unwinding_pct=9.0,
        sl_points=7.5, qty=300,
    )
    assert targets["ok"] is True
    assert targets["target_1"] >= 100.0 + 1.8 * 7.5
    assert targets["target_2_p90"] > targets["target_1"]
    assert targets["breakeven"] == pytest.approx(101.50)
    assert targets["t1_book_qty"] == 150 and targets["t2_book_qty"] == 150
    assert targets["t1_label"].startswith("T1 \u20b9")
    assert "p65" in targets["t1_label"] and "50% Book" in targets["t1_label"]
    assert "p90 Wave" in targets["t2_label"]
    # Taylor components are exposed for the narration and the audit trail.
    t1 = targets["formula"]["t1"]
    assert t1["delta_term"] == pytest.approx(0.55 * 60, abs=0.01)
    assert t1["gamma_term"] == pytest.approx(0.5 * 0.002 * 60 * 60, abs=0.01)
    assert targets["formula"]["t2"]["boost"] > 0


def test_greek_mapper_fails_closed_without_full_greeks() -> None:
    targets = map_greek_targets(
        entry=100.0, spot=25_000, p65=25_060, p90=25_120,
        delta=0.55, gamma=0.0, theta=-4.2, vega=8.1,
    )
    assert targets["ok"] is False
    assert targets["reason"] == "MISSING_GREEKS_OR_TARGET_BAND"
    assert "gamma" in targets["missing"]


def test_p65_interpolates_between_p50_and_p90() -> None:
    value = interpolate_p65(24_900, 25_000, 25_100)
    assert 25_000 < value < 25_100


# -- risk guard, breaker and vault ---------------------------------------- #
def test_two_consecutive_sl_locks_the_day_and_caps_a_plus_trades() -> None:
    guard = SniperRiskGuard(date="2026-11-10", starting_capital=100_000)
    assert guard.register_entry("A+")["ok"] is True
    guard.register_trade(pnl=-2_250, outcome="SL", sl_points=7.5)
    assert guard.daily_lock is False
    assert guard.register_entry("A+")["ok"] is True
    guard.register_trade(pnl=-2_250, outcome="SL", sl_points=7.5)
    assert guard.daily_lock is True
    assert guard.can_open("A+") == {"ok": False, "reason": "DAILY_LOCK_2_SL"}
    assert guard.stats()["consecutive_sl"] == 2

    capped = SniperRiskGuard(date="2026-11-10", starting_capital=100_000)
    for _ in range(3):
        assert capped.register_entry("A+")["ok"] is True
    assert capped.can_open("A+")["reason"] == "MAX_3_A_PLUS_TRADES"
    assert SniperRiskGuard(date="2026-11-10").can_open("A")["reason"] == "LOW_GRADE_SETUP"


def test_t2_is_skipped_when_obi_above_175_and_spot_clears_p90() -> None:
    trade = new_trade(100.0, 7.5, qty=300)
    book_partials(trade, 115.0)
    assert trade["qty_open"] == 150
    assert book_partials(trade, 200.0, ride_wave=True) == []
    assert trade["qty_open"] == 150 and trade["status"] == "OPEN"
    assert trade["current_trailing_sl"] == pytest.approx(101.50)


def test_half_of_every_positive_leg_is_swept_into_the_profit_vault() -> None:
    guard = SniperRiskGuard(date="2026-11-10", starting_capital=100_000)
    first = guard.register_leg({"leg": "T1", "pnl": 1_000.0})
    second = guard.register_leg({"leg": "T2", "pnl": 2_000.0})
    third = guard.register_leg({"leg": "SL", "pnl": -500.0})
    assert first["swept"] == pytest.approx(500.0)
    assert second["swept"] == pytest.approx(1_000.0)
    assert third["swept"] == 0.0
    assert guard.profit_vault == pytest.approx(1_500.0)
    assert guard.realized == pytest.approx(2_500.0)
    assert guard.t1_hits == 1 and guard.t2_full_runs == 1


def test_monthly_expectancy_tracker_reports_all_six_hud_metrics() -> None:
    rows = [
        {"date": "2026-11-03", "net": 4_500, "outcome": "WIN", "entry": 150,
         "sl_price": 142.5, "t1_qty": 150, "t2_qty": 150},
        {"date": "2026-11-04", "net": -2_250, "outcome": "SL", "entry": 150,
         "sl_price": 142.5, "t1_qty": 0, "t2_qty": 0},
        {"date": "2026-10-31", "net": 9_000, "outcome": "WIN", "entry": 150,
         "sl_price": 142.5, "t1_qty": 150, "t2_qty": 0},
    ]
    stats = expectancy_stats(rows, month="2026-11", starting_capital=100_000,
                             profit_vault=1_500)
    assert stats["trades"] == 2
    assert stats["win_rate_pct"] == 50.0
    assert stats["avg_sl_pts"] == pytest.approx(7.5)
    assert stats["t1_hit_rate_pct"] == 50.0
    assert stats["t2_full_run_rate_pct"] == 50.0
    assert stats["profit_vault"] == 1_500.0
    assert stats["net_roi_pct"] == pytest.approx(2.25)
