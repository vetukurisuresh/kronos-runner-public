"""Proofs for the expiry-day Zero-to-Hero / Gamma Blast desk."""

from __future__ import annotations

from datetime import datetime, timedelta

from execution.pro_money_manager import (
    HERO_STAGE1_MULTIPLE,
    HeroTradeDesk,
    manage_hero_trade,
)
from strategy.expiry_zero_to_hero import (
    WINDOW_2,
    WINDOW_3,
    detect_peak_exhaustion,
    expiry_day_status,
    expiry_window,
    preemptive_trigger,
    rank_hero_candidates,
    strike_distance_ok,
)


def _chain(spot: float = 25000.0) -> list[dict]:
    def row(strike: float, ce: dict, pe: dict) -> dict:
        return {
            "strike_price": strike,
            "call_options": {
                "instrument_key": f"NSE_FO|{int(strike)}CE",
                "market_data": ce,
                "option_greeks": {"gamma": ce.pop("gamma"), "delta": ce.pop("delta")},
            },
            "put_options": {
                "instrument_key": f"NSE_FO|{int(strike)}PE",
                "market_data": pe,
                "option_greeks": {"gamma": pe.pop("gamma"), "delta": pe.pop("delta")},
            },
        }

    return [
        row(
            25000,
            {"ltp": 13.40, "oi": 120000, "oi_change_pct": -9.4, "obi_5": 1.82,
             "tbq": 180000, "tsq": 99000, "gamma": 0.0024, "delta": 0.24},
            {"ltp": 18.00, "oi": 100000, "oi_change_pct": 7.2, "obi_5": 1.1,
             "tbq": 80000, "tsq": 90000, "gamma": 0.0018, "delta": -0.22},
        ),
        row(
            25050,
            {"ltp": 9.20, "oi": 90000, "oi_change_pct": -6.3, "obi_5": 1.55,
             "tbq": 90000, "tsq": 70000, "gamma": 0.0012, "delta": 0.16},
            {"ltp": 21.00, "oi": 80000, "oi_change_pct": 1.0, "obi_5": 0.8,
             "tbq": 50000, "tsq": 60000, "gamma": 0.0010, "delta": -0.18},
        ),
        row(
            25100,
            {"ltp": 1.50, "oi": 10000, "oi_change_pct": 0.0, "obi_5": 0.7,
             "tbq": 1000, "tsq": 4000, "gamma": 0.0002, "delta": 0.05},
            {"ltp": 1.20, "oi": 9000, "oi_change_pct": 0.0, "obi_5": 0.7,
             "tbq": 1000, "tsq": 3000, "gamma": 0.0001, "delta": -0.04},
        ),
        row(
            24950,
            {"ltp": 45.00, "oi": 70000, "oi_change_pct": 1.0, "obi_5": 0.9,
             "tbq": 50000, "tsq": 50000, "gamma": 0.0010, "delta": 0.44},
            {"ltp": 8.50, "oi": 110000, "oi_change_pct": -7.0, "obi_5": 1.6,
             "tbq": 100000, "tsq": 70000, "gamma": 0.0014, "delta": -0.15},
        ),
    ]


def test_heroscore_picks_cheap_atm_gamma_and_rejects_far_otm_junk() -> None:
    now = datetime(2026, 11, 10, 13, 40)
    scan = rank_hero_candidates(
        _chain(), 25000.0, "NIFTY", now,
        window=expiry_window(now), kronos_move=45.0, paths_up=82, samples=100,
    )
    assert scan["window"]["window"] == WINDOW_3
    hero = scan["hero"]
    assert hero is not None
    assert hero["side"] == "CE"
    assert hero["strike"] == 25000
    assert 8.0 <= hero["ltp"] <= 32.0
    assert hero["score_100"] >= 80.0
    assert hero["fuel"]["ok"] is True
    assert "Panic Covering" in hero["fuel"]["reason"]
    assert scan["rejected"]["far_otm"] >= 1
    # The Rs 1.50 far-OTM lottery ticket is never eligible.
    junk = next(c for c in scan["candidates"] if c["strike"] == 25100 and c["side"] == "CE")
    assert junk["eligible"] is False
    assert "FAR_OTM_JUNK" in junk["reason"]


def test_strike_distance_is_atm_to_one_strike_otm_only() -> None:
    ok, distance, _ = strike_distance_ok("NIFTY", "CE", 25050, 25000)
    assert ok and distance == 50
    ok, _, _ = strike_distance_ok("NIFTY", "CE", 25100, 25000)
    assert not ok
    ok, distance, _ = strike_distance_ok("BANKNIFTY", "PE", 57000, 57100, step=100)
    assert ok and distance == 100
    ok, _, _ = strike_distance_ok("BANKNIFTY", "PE", 56800, 57100, step=100)
    assert not ok


def test_expiry_auto_detection_and_manual_override() -> None:
    auto = expiry_day_status("2026-11-10", today="2026-11-10", mode="AUTO")
    assert auto["is_expiry_day"] is True and auto["enabled"] is True
    not_today = expiry_day_status("2026-11-12", today="2026-11-10", mode="AUTO")
    assert not_today["is_expiry_day"] is False and not_today["enabled"] is False
    forced = expiry_day_status("2026-11-12", today="2026-11-10", mode="FORCE_ON")
    assert forced["is_expiry_day"] is False and forced["enabled"] is True
    off = expiry_day_status("2026-11-10", today="2026-11-10", mode="OFF")
    assert off["is_expiry_day"] is True and off["enabled"] is False


def test_midday_theta_blackout_blocks_until_extreme_blast() -> None:
    now = datetime(2026, 11, 10, 11, 0)
    window = expiry_window(now)
    assert window["window"] == WINDOW_2
    assert window["allowed"] is False
    assert "THETA SHIELD ACTIVE" in window["status"]

    candidate = {
        "symbol": "NIFTY", "strike": 25000, "side": "CE", "ltp": 13.40,
        "delta": 0.24, "gamma": 0.0024, "eligible": True, "reason": "ELIGIBLE",
    }
    common = dict(
        paths_up=82, samples=100, mini_acceleration=0.8, atp=12.10,
        rolling_15m_high=12.90, volume_ratio_1m=2.6, obi_5=1.82,
        tbq=180000, tsq=99000, ignition_low=9.40, window=window,
        entry=13.40, kronos_move=45.0,
    )
    blocked = preemptive_trigger(
        candidate, futures_volume_ratio=2.6, basis=35.0, previous_basis=22.0, **common
    )
    assert blocked["trigger"] is False
    assert blocked["reason"] == "THETA_SHIELD_ACTIVE"
    assert blocked["theta_shield"] is True

    extreme = preemptive_trigger(
        candidate, futures_volume_ratio=3.2, basis=35.0, previous_basis=22.0,
        **{**common, "paths_up": 92},
    )
    assert extreme["extreme_breakout"] is True
    assert extreme["trigger"] is True
    assert extreme["signal"] == "ZERO_TO_HERO_BUY"
    assert 3.0 <= extreme["sl_points"] <= 6.0
    assert abs(extreme["stop"] - 9.40) < 0.01


def test_preemptive_trigger_enforces_tight_3_to_6_rupee_stop() -> None:
    now = datetime(2026, 11, 10, 13, 40)
    candidate = {
        "symbol": "NIFTY", "strike": 25000, "side": "CE", "ltp": 13.40,
        "delta": 0.24, "gamma": 0.0024, "eligible": True, "reason": "ELIGIBLE",
    }
    base = dict(
        candidate=candidate, paths_up=82, samples=100, mini_acceleration=0.8,
        atp=12.10, rolling_15m_high=12.90, volume_ratio_1m=2.6,
        obi_5=1.82, tbq=180000, tsq=99000, futures_volume_ratio=2.6,
        basis=35.0, previous_basis=22.0, window=expiry_window(now),
        entry=13.40, kronos_move=45.0,
    )
    armed = preemptive_trigger(**base, ignition_low=9.40)
    assert armed["trigger"] is True
    assert armed["sl_points"] == 4.0
    assert armed["stop"] == 9.40
    assert armed["target_1"] == 26.80
    assert armed["signature_count"] == 5

    too_wide = preemptive_trigger(**base, ignition_low=6.40)
    assert too_wide["sl_points"] == 0.0
    assert too_wide["reason"] == "HERO_SL_OUT_OF_RANGE"


def test_peak_exhaustion_and_silent_watch_loop() -> None:
    now = datetime(2026, 11, 10, 14, 25)
    assert expiry_window(now)["window"] == "W4"

    trade = {
        "status": "OPEN", "entry_price": 10.00, "initial_qty": 300,
        "qty_open": 300, "qty": 300, "current_trailing_sl": 9.00,
        "target_1": 20.00, "target_2_p90": 36.00, "realized_pnl_rs": 0.0,
        "booked_legs": [],
    }
    assert HERO_STAGE1_MULTIPLE == 2.0

    stage1 = manage_hero_trade(trade, 20.00)
    assert stage1["stage1"] is True
    assert trade["qty_open"] == 195
    assert trade["current_trailing_sl"] == 13.50
    assert stage1["legs"][0]["qty"] == 105

    stage2 = manage_hero_trade(trade, 36.50)
    assert stage2["stage2"] is True
    assert trade["qty_open"] == 90
    assert stage2["legs"][0]["leg"] == "HERO_P90"

    peak = manage_hero_trade(trade, 52.00, obi_5=0.55, tbq=50000, tsq=120000)
    assert peak["peak_exit"] is True
    assert "OBI_FLIP" in peak["reason"]
    assert peak["remaining"] == 0
    assert trade["status"] == "CLOSED"
    assert trade["exit_reason"] == "HERO_PEAK_EXIT"
    assert peak["total_pnl"] > 7_000

    desk = HeroTradeDesk(date="2026-11-10")
    desk.register_entry()
    exit_result = desk.register_exit(peak["total_pnl"], now)
    assert exit_result["swept"] == 4567.5  # 60% of the 7,612.50 total
    assert desk.status == "SILENT_WATCH_FOR_NEXT_WAVE"
    assert desk.can_fire(now + timedelta(minutes=1), allowed=True)["ok"] is False
    assert desk.can_fire(now + timedelta(minutes=6), allowed=True)["ok"] is True

    # Two more Hero trades complete the three-trade expiry-day cap.
    for minute in (7, 14):
        desk.register_entry()
        desk.register_exit(1000.0, now + timedelta(minutes=minute))
    assert desk.trades == 3
    assert desk.status == "DONE"
    assert desk.can_fire(now + timedelta(minutes=30), allowed=True)["reason"] == "MAX_3_HERO_TRADES"


def test_stage1_moves_stop_to_135x_and_books_the_last_30pct() -> None:
    trade = {
        "status": "OPEN", "entry_price": 10.00, "initial_qty": 300,
        "qty_open": 300, "qty": 300, "current_trailing_sl": 8.00,
        "target_1": 20.00, "target_2_p90": 40.00, "realized_pnl_rs": 0.0,
        "booked_legs": [],
    }
    first = manage_hero_trade(trade, 20.00)
    assert first["stage1"] is True
    assert trade["current_trailing_sl"] == 13.50
    stopped = manage_hero_trade(trade, 13.40)
    assert stopped["stop_exit"] is True
    assert stopped["remaining"] == 0
    assert trade["exit_reason"] == "HERO_TRAIL_SL"
    assert stopped["total_pnl"] > 0  # 2x leg profit survived the trail stop


def test_peak_detector_requires_profit_zone_and_12pct_drop_after_p90() -> None:
    detection = detect_peak_exhaustion(
        10.0, 16.0, 20.0, p90_target=36.0, obi_5=1.4, tbq=100, tsq=100,
    )
    assert detection["peak_exit"] is False  # below the 1.8x profit zone
    detection = detect_peak_exhaustion(
        10.0, 37.0, 37.0, p90_target=36.0, obi_5=1.4, tbq=100, tsq=100,
    )
    assert detection["peak_exit"] is False
    detection = detect_peak_exhaustion(
        10.0, 32.0, 37.0, p90_target=36.0, obi_5=1.4, tbq=100, tsq=100,
    )
    assert detection["peak_exit"] is True
    assert detection["reason"] == "PEAK_DRAWDOWN_12PCT"
