"""Proofs for the adaptive scalper and house-money compounder."""

from __future__ import annotations

from datetime import datetime, timedelta

from execution.house_money_compounder import HouseMoneyCompounder
from strategy.adaptive_scalper import (
    MODE_PING_PONG,
    REGIME_DEAD_COMPRESSION,
    REGIME_OPENING_VELOCITY,
    REGIME_SIDEWAYS_PING_PONG,
    REGIME_TREND_PULLBACK_SCALP,
    brain_banner,
    classify_regime,
    manage_scalp_trade,
    scalp_signal,
)


def _sideways_decision() -> dict:
    return classify_regime(
        datetime(2026, 11, 10, 11, 0),
        symbol="NIFTY", option_atr_1m=2.5, futures_volume_ratio=1.1,
        mini_velocity=0.05, paths_up=50, samples=100,
        p10=24930, median=24960, p90=24990, spot=24930,
        spread=0.20, mode="AUTO",
    )


def test_regime_classifier_transitions_between_all_four_states() -> None:
    opening = classify_regime(
        datetime(2026, 11, 10, 9, 30),
        symbol="NIFTY", option_atr_1m=3.6, futures_volume_ratio=2.8,
        mini_velocity=0.65, paths_up=68, samples=100,
        p10=24920, median=24980, p90=25020, spot=24980,
        spread=0.20,
    )
    assert opening["regime"] == REGIME_OPENING_VELOCITY
    assert opening["tradeable"] is True
    assert opening["params"]["sl_points"] == 4.0

    sideways = _sideways_decision()
    assert sideways["regime"] == REGIME_SIDEWAYS_PING_PONG
    assert sideways["tradeable"] is True
    assert sideways["direction"] == "BOTH"

    trend = classify_regime(
        datetime(2026, 11, 10, 11, 0),
        option_atr_1m=2.6, futures_volume_ratio=1.4, mini_velocity=0.3,
        paths_up=82, samples=100, p10=24920, median=24980, p90=25020,
        spot=24995, spread=0.20,
    )
    assert trend["regime"] == REGIME_TREND_PULLBACK_SCALP
    assert trend["direction"] == "CE"

    dead_atr = classify_regime(
        datetime(2026, 11, 10, 11, 0),
        option_atr_1m=1.4, futures_volume_ratio=1.0, mini_velocity=0.0,
        paths_up=50, samples=100, p10=24950, p90=25010, spot=24980,
        spread=0.20,
    )
    assert dead_atr["regime"] == REGIME_DEAD_COMPRESSION
    assert dead_atr["tradeable"] is False
    assert "ATR" in dead_atr["reason"]

    dead_spread = classify_regime(
        datetime(2026, 11, 10, 11, 0),
        option_atr_1m=2.5, futures_volume_ratio=1.0,
        paths_up=50, samples=100, p10=24930, p90=24990, spot=24960,
        spread=0.85,
    )
    assert dead_spread["regime"] == REGIME_DEAD_COMPRESSION
    assert "SPREAD" in dead_spread["reason"]

    mode_wait = classify_regime(
        datetime(2026, 11, 10, 11, 0),
        option_atr_1m=2.5, paths_up=82, samples=100,
        p10=24920, p90=25020, spread=0.20, mode=MODE_PING_PONG,
    )
    assert mode_wait["regime"] == REGIME_TREND_PULLBACK_SCALP
    assert mode_wait["tradeable"] is False
    assert "PING-PONG" in mode_wait["mode_blocked"]


def test_ping_pong_ce_at_p10_targets_med_and_pe_at_p90_targets_med() -> None:
    decision = _sideways_decision()
    ce = scalp_signal(
        decision, spot=24930, p10=24930, median=24960, p90=24990,
        ce_obi_5=1.55, ce_tbq=150_000, ce_tsq=100_000, ce_ltp=13.0,
        expected_move_pts=8.0,
    )
    assert ce["trigger"] is True
    assert ce["order"] == "SCALP_BUY_CE"
    assert ce["target_spot"] == 24960
    assert ce["target_1"] == 19.5
    assert ce["target_2"] == 23.0
    assert ce["sl_points"] == 3.5

    pe = scalp_signal(
        decision, spot=24990, p10=24930, median=24960, p90=24990,
        pe_obi_5=1.60, pe_tbq=140_000, pe_tsq=100_000, pe_ltp=14.0,
        expected_move_pts=8.2,
    )
    assert pe["trigger"] is True
    assert pe["order"] == "SCALP_BUY_PE"
    assert pe["target_spot"] == 24960

    middle = scalp_signal(
        decision, spot=24960, p10=24930, median=24960, p90=24990,
        ce_obi_5=1.60, ce_tbq=140_000, ce_tsq=100_000, ce_ltp=13.0,
        expected_move_pts=8.0,
    )
    assert middle["trigger"] is False
    assert middle["reason"] == "WAITING_FOR_P10_P90_OR_ATP_RECLAIM"


def test_minimum_net_edge_blocks_low_range_brokerage_trap() -> None:
    decision = _sideways_decision()
    low_edge = scalp_signal(
        decision, spot=24930, p10=24930, median=24960, p90=24990,
        ce_obi_5=1.8, ce_tbq=150_000, ce_tsq=100_000, ce_ltp=13.0,
        expected_move_pts=5.0,
    )
    assert low_edge["trigger"] is False
    assert low_edge["reason"] == "LOW_NET_EDGE_BROKERAGE_TRAP"
    assert low_edge["net_edge_pts"] == 4.0

    bad_rr = scalp_signal(
        decision, spot=24930, p10=24930, median=24960, p90=24990,
        ce_obi_5=1.8, ce_tbq=150_000, ce_tsq=100_000, ce_ltp=13.0,
        expected_move_pts=6.5,
        min_rr=2.0,
    )
    assert bad_rr["trigger"] is False
    assert bad_rr["reason"] == "REWARD_RISK_BELOW_1_7"


def test_two_stage_scalp_harvest_and_obi_flip() -> None:
    trade = {
        "status": "OPEN", "entry_price": 10.0, "initial_qty": 300,
        "qty_open": 300, "qty": 300, "current_trailing_sl": 6.0,
        "target_1": 16.5, "scalp_s1_target": 16.5, "scalp_s2_target": 20.0,
        "realized_pnl_rs": 0.0, "booked_legs": [],
    }
    first = manage_scalp_trade(trade, 16.5, obi_5=1.6)
    assert first["stage1"] is True
    assert first["legs"][0]["qty"] == 180
    assert trade["current_trailing_sl"] == 11.5
    assert trade["qty_open"] == 120
    assert first["exit"] is False

    flipped = manage_scalp_trade(trade, 18.0, obi_5=0.60)
    assert flipped["stage2"] is True
    assert flipped["reason"] == "SCALP_OBI_FLIP"
    assert trade["status"] == "CLOSED"
    assert flipped["total_pnl"] > 2_000

    target_trade = {
        "status": "OPEN", "entry_price": 10.0, "initial_qty": 300,
        "qty_open": 300, "qty": 300, "current_trailing_sl": 6.5,
        "target_1": 16.5, "scalp_s1_target": 16.5, "scalp_s2_target": 20.0,
        "realized_pnl_rs": 0.0, "booked_legs": [],
    }
    manage_scalp_trade(target_trade, 16.5, obi_5=1.8)
    s2 = manage_scalp_trade(target_trade, 20.0, obi_5=1.2)
    assert s2["reason"] == "SCALP_S2"
    assert s2["total_pnl"] == 2_370.0


def test_post_s1_stop_locks_entry_plus_one_fifty() -> None:
    trade = {
        "status": "OPEN", "entry_price": 10.0, "initial_qty": 300,
        "qty_open": 300, "qty": 300, "current_trailing_sl": 6.5,
        "target_1": 16.5, "scalp_s1_target": 16.5, "scalp_s2_target": 20.0,
        "realized_pnl_rs": 0.0, "booked_legs": [],
    }
    first = manage_scalp_trade(trade, 16.5, obi_5=1.6)
    assert first["stage1"] is True
    assert trade["current_trailing_sl"] == 11.5

    stopped = manage_scalp_trade(trade, 11.40, obi_5=1.2)
    assert stopped["exit"] is True
    assert stopped["reason"] == "SCALP_BREAKEVEN_STOP"
    assert trade["status"] == "CLOSED"
    assert trade["qty_open"] == 0
    assert stopped["total_pnl"] > 0



def test_house_money_locks_half_then_scales_only_from_profit() -> None:
    now = datetime(2026, 11, 10, 9, 30)
    desk = HouseMoneyCompounder(starting_capital=100_000)
    start = desk.next_quantity(sl_points=4.0)
    assert start["qty"] == 300
    assert start["risk_rs"] == 1_200
    assert start["funding"] == "BASE_CAPITAL"
    assert desk.can_open(now, sl_points=4.0)["ok"] is True

    desk.register_entry(now)
    settled = desk.register_trade(3_000, now)
    assert settled["vault"] == 1_500
    assert settled["house_money_pool"] == 1_500
    assert settled["house_money_mode"] is True
    assert desk.status(now) == "DIAGNOSTIC_COOLDOWN"
    # The fast sleeve re-arms after 15s, not 90s: a 90s lock skipped the whole
    # ping-pong re-entry leg on a 5m candle.
    assert desk.can_open(now + timedelta(seconds=10), sl_points=4.0)["ok"] is False

    after_cooldown = desk.can_open(now + timedelta(seconds=91), sl_points=4.0)
    assert after_cooldown["ok"] is True
    assert after_cooldown["funding"] == "HOUSE_MONEY"
    assert after_cooldown["base_capital_at_risk_rs"] == 0.0
    assert after_cooldown["qty"] == 300
    assert after_cooldown["risk_rs"] <= after_cooldown["pool"]

    desk.register_entry(now + timedelta(seconds=92))
    desk.register_trade(2_500, now + timedelta(seconds=120))
    assert desk.realized == 5_500
    assert desk.win_streak == 2
    assert desk.accelerator_ready is True
    accelerated = desk.next_quantity(sl_points=4.0, obi_5=1.82, futures_volume_ratio=2.8)
    assert accelerated["qty"] in (375, 450)
    assert accelerated["funding"] == "HOUSE_MONEY"
    assert accelerated["base_capital_at_risk_rs"] == 0.0
    assert accelerated["risk_rs"] <= desk.house_money_pool


def test_two_consecutive_sl_locks_the_scalper_and_banner_reports_regime() -> None:
    now = datetime(2026, 11, 10, 10, 0)
    desk = HouseMoneyCompounder(starting_capital=100_000)
    desk.register_entry(now)
    desk.register_trade(-500, now)
    assert desk.locked is False
    assert desk.consecutive_sl == 1
    desk.register_entry(now + timedelta(seconds=91))
    desk.register_trade(-500, now + timedelta(seconds=120))
    assert desk.locked is True
    assert desk.lock_reason == "2_CONSECUTIVE_SL"
    assert desk.can_open(now + timedelta(seconds=300))["ok"] is False

    banner = brain_banner(_sideways_decision(), trade_no=1, pnl=2850)
    assert "POST-TRADE BRAIN" in banner
    assert "PING-PONG" in banner
    assert "p10" in banner and "p90" in banner
