from __future__ import annotations

import math

import pytest

import market.live_ticks as live_ticks
from strategy.parallel_alpha_engines import (
    ParallelAlphaEngine,
    build_parallel_alpha,
    coiled_spring_trap,
    dual_index_rotator,
    heavyweight_tsunami,
    lead_lag_snipe,
    opportunity_score,
    parity_dislocation_snipe,
    put_call_parity,
)


# ---------------------------------------------------------------------------
# Module 1: put-call parity
# ---------------------------------------------------------------------------


def test_put_call_parity_computes_fair_values_and_discounts():
    values = put_call_parity(
        futures_ltp=25_000.0,
        strike=24_950.0,
        ce_ltp=100.0,
        pe_ltp=55.0,
    )

    # Fair CE = 55 + (25,000 - 24,950) = 105; fair PE = 100 - 50 = 50.
    assert values["fair_ce"] == pytest.approx(105.0)
    assert values["fair_pe"] == pytest.approx(50.0)
    assert values["ce_discount"] == pytest.approx(5.0)
    assert values["pe_discount"] == pytest.approx(-5.0)


def test_parity_sniper_triggers_on_four_rupee_underpriced_ce():
    result = parity_dislocation_snipe(
        futures_ltp=25_000.0,
        strike=24_950.0,
        ce_ltp=100.0,
        pe_ltp=56.0,  # fair CE = 106 => CE is ₹6 cheap
        ce_obi_5=1.62,
        kronos_bullish=True,
        symbol="NIFTY",
    )

    assert result["trigger"] is True
    assert result["side"] == "CE"
    assert result["ce_edge"] >= 3.50
    assert result["reason"] == "PARITY_DISLOCATION_SNIPE"
    assert result["entry"] == pytest.approx(100.0)
    assert result["target_1"] == pytest.approx(105.50)
    assert result["target_2"] == pytest.approx(109.00)


def test_parity_sniper_mirrors_for_underpriced_pe():
    result = parity_dislocation_snipe(
        futures_ltp=24_900.0,
        strike=24_950.0,
        ce_ltp=55.0,
        pe_ltp=100.0,  # basis -50 => fair PE = 105
        pe_obi_5=1.55,
        kronos_bearish=True,
        symbol="NIFTY",
    )

    assert result["trigger"] is True
    assert result["side"] == "PE"
    assert result["fair"]["pe_discount"] == pytest.approx(5.0)
    assert result["pe_obi_5"] == pytest.approx(1.55)


def test_parity_sniper_respects_banknifty_eight_rupee_threshold():
    common = dict(
        futures_ltp=25_000.0,
        strike=24_950.0,
        ce_ltp=100.0,
        pe_ltp=54.0,  # ₹4 edge
        ce_obi_5=1.80,
        kronos_bullish=True,
    )

    nifty = parity_dislocation_snipe(**common, symbol="NIFTY")
    bank = parity_dislocation_snipe(**common, symbol="BANKNIFTY")

    assert nifty["trigger"] is True
    assert bank["trigger"] is False
    assert bank["threshold"] == pytest.approx(8.00)


def test_parity_sniper_requires_order_flow_confirmation():
    result = parity_dislocation_snipe(
        futures_ltp=25_000.0,
        strike=24_950.0,
        ce_ltp=100.0,
        pe_ltp=60.0,
        ce_obi_5=1.39,
        kronos_bullish=True,
    )

    assert result["trigger"] is False
    assert result["side"] == ""
    assert result["reason"] == "NO_PARITY_DISLOCATION"


# ---------------------------------------------------------------------------
# Module 2: 3-second lead-lag
# ---------------------------------------------------------------------------


def _leader(name: str, *, ltp: float, velocity: float, high_break: bool,
            low_break: bool = False, vtt: float = 2.6) -> dict:
    return {
        "name": name,
        "ltp": ltp,
        "one_min_high": ltp - 1 if high_break else ltp + 20,
        "one_min_low": ltp + 1 if low_break else ltp - 20,
        "velocity_3s": velocity,
        "vtt_ratio": vtt,
        "high_break": high_break,
        "low_break": low_break,
    }


def test_lead_lag_fires_when_heavyweights_and_banknifty_surge_before_nifty():
    leaders = {
        "HDFCBANK": _leader("HDFCBANK", ltp=1_720.0, velocity=8.0, high_break=True),
        "ICICIBANK": _leader("ICICIBANK", ltp=1_180.0, velocity=6.0, high_break=True),
        "BANKNIFTY": _leader("BANKNIFTY", ltp=52_100.0, velocity=90.0, high_break=True),
    }

    result = lead_lag_snipe(
        leaders,
        lagging_option_move_3s=0.82,
        lagging_obi_5=1.48,
        symbol="NIFTY",
    )

    assert result["trigger"] is True
    assert result["side"] == "CE"
    assert result["leader_side"] == "BULL"
    assert result["bull_leaders"] == 3
    assert result["lagging_option_move_3s"] == pytest.approx(0.82)
    assert result["reason"] == "LEAD_LAG_SNIPE"


def test_lead_lag_mirrors_for_bearish_lead_and_requires_volume_surge():
    leaders = {
        "HDFCBANK": _leader("HDFCBANK", ltp=1_690.0, velocity=-8.0, high_break=False,
                            low_break=True, vtt=2.5),
        "ICICIBANK": _leader("ICICIBANK", ltp=1_160.0, velocity=-5.0, high_break=False,
                             low_break=True, vtt=2.4),
        "BANKNIFTY": _leader("BANKNIFTY", ltp=51_700.0, velocity=-70.0, high_break=False,
                             low_break=True, vtt=2.3),
    }

    result = lead_lag_snipe(leaders, lagging_option_move_3s=-0.40)
    weak = lead_lag_snipe(
        {name: {**row, "vtt_ratio": 2.19} for name, row in leaders.items()},
        lagging_option_move_3s=-0.40,
    )

    assert result["trigger"] is True
    assert result["side"] == "PE"
    assert weak["trigger"] is False


def test_lead_lag_waits_when_lagging_option_already_moved():
    leaders = {
        "HDFCBANK": _leader("HDFCBANK", ltp=1_720.0, velocity=8.0, high_break=True),
        "ICICIBANK": _leader("ICICIBANK", ltp=1_180.0, velocity=6.0, high_break=True),
        "BANKNIFTY": _leader("BANKNIFTY", ltp=52_100.0, velocity=90.0, high_break=True),
    }

    result = lead_lag_snipe(leaders, lagging_option_move_3s=1.50)

    assert result["trigger"] is False
    assert result["side"] == ""


# ---------------------------------------------------------------------------
# Module 3: 5/5 heavyweight tsunami
# ---------------------------------------------------------------------------


def _heavy(name: str, *, bullish: bool = True) -> dict:
    if bullish:
        return {
            "name": name,
            "ltp": 110.0,
            "atp": 100.0,
            "open_1m": 100.0,
            "close_1m": 110.0,
            "tbq": 200.0,
            "tsq": 100.0,
        }
    return {
        "name": name,
        "ltp": 90.0,
        "atp": 100.0,
        "open_1m": 100.0,
        "close_1m": 90.0,
        "tbq": 100.0,
        "tsq": 200.0,
    }


def test_heavyweight_tsunami_fires_on_fresh_unanimous_5_of_5_bull():
    stocks = {
        name: _heavy(name)
        for name in ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS")
    }

    result = heavyweight_tsunami(
        stocks,
        previous_bull_count=3,
        kronos_bullish=True,
    )

    assert result["bull_count"] == 5
    assert result["all_bull"] is True
    assert result["fresh_transition"] is True
    assert result["trigger"] is True
    assert result["side"] == "CE"
    assert result["target_1"] == pytest.approx(15.0)
    assert result["target_2"] == pytest.approx(28.0)
    assert result["gear"] == 3
    assert "5/5 BULL TSUNAMI" in result["status"]


def test_heavyweight_tsunami_needs_alignment_and_a_fresh_transition():
    stocks = {
        name: _heavy(name)
        for name in ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS")
    }
    not_aligned = heavyweight_tsunami(stocks, previous_bull_count=3)
    already_five = heavyweight_tsunami(
        stocks,
        previous_bull_count=5,
        kronos_bullish=True,
    )

    assert not_aligned["trigger"] is False
    assert already_five["trigger"] is False
    assert already_five["all_bull"] is True


def test_heavyweight_tsunami_mirrors_bearish_and_rejects_mixed_flow():
    bear = {
        name: _heavy(name, bullish=False)
        for name in ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS")
    }
    mixed = dict(bear)
    mixed["TCS"] = _heavy("TCS", bullish=True)

    result = heavyweight_tsunami(
        bear,
        previous_bear_count=2,
        kronos_bearish=True,
    )
    mixed_result = heavyweight_tsunami(
        mixed,
        previous_bear_count=2,
        kronos_bearish=True,
    )

    assert result["trigger"] is True
    assert result["side"] == "PE"
    assert result["bear_count"] == 5
    assert mixed_result["trigger"] is False


# ---------------------------------------------------------------------------
# Module 4: coiled spring / dual-index rotator
# ---------------------------------------------------------------------------


def test_coiled_spring_arms_bracket_and_fires_upper_breakout():
    result = coiled_spring_trap(
        24_303.0,
        box_high=24_300.0,
        box_low=24_278.0,
        compression_minutes=31,
        straddle_ltp=210.0,
        straddle_intraday_low=210.0,
        futures_oi_rising=True,
        futures_vtt_ratio=2.1,
    )

    assert result["armed"] is True
    assert result["compressed"] is True
    assert result["upper_trigger"] == pytest.approx(24_303.0)
    assert result["lower_trigger"] == pytest.approx(24_275.0)
    assert result["trigger"] is True
    assert result["side"] == "CE"
    assert result["cancel_opposite"] is True


def test_coiled_spring_fires_lower_breakout_and_rejects_wide_box():
    result = coiled_spring_trap(
        24_275.0,
        box_high=24_300.0,
        box_low=24_278.0,
        compression_minutes=30,
        straddle_ltp=210.0,
        straddle_intraday_low=210.0,
        futures_oi_rising=True,
        futures_vtt_ratio=2.0,
    )
    wide = coiled_spring_trap(
        24_300.0,
        box_high=24_320.0,
        box_low=24_290.0,
        compression_minutes=45,
        straddle_ltp=220.0,
        straddle_intraday_low=200.0,
        futures_oi_rising=True,
        futures_vtt_ratio=4.0,
    )

    assert result["trigger"] is True
    assert result["side"] == "PE"
    assert wide["armed"] is False
    assert wide["trigger"] is False


def test_opportunity_score_orders_nifty_above_slow_banknifty():
    nifty = {
        "atr_velocity": 16.0,
        "obi_strength": 2.4,
        "parity_edge": 8.0,
        "kronos_confidence": 90.0,
    }
    bank = {
        "atr_velocity": 8.0,
        "obi_strength": 1.2,
        "parity_edge": 2.0,
        "kronos_confidence": 60.0,
    }

    assert opportunity_score(nifty) > opportunity_score(bank)
    assert math.isclose(opportunity_score(nifty), 82.5, rel_tol=0, abs_tol=0.01)


def test_dual_index_rotator_switches_and_caps_both_sleeves():
    result = dual_index_rotator(
        {"atr_velocity": 16.0, "obi_strength": 2.4, "parity_edge": 8.0,
         "kronos_confidence": 90.0},
        {"atr_velocity": 8.0, "obi_strength": 1.2, "parity_edge": 2.0,
         "kronos_confidence": 60.0},
        previous_winner="BANKNIFTY",
        scalp_capital=50_000.0,
        sniper_capital=90_000.0,
    )

    assert result["winner"] == "NIFTY"
    assert result["rotated"] is True
    assert result["trigger"] is True
    assert result["scalp_allocated"] == pytest.approx(35_000.0)
    assert result["sniper_allocated"] == pytest.approx(65_000.0)
    assert result["total_allocated"] == pytest.approx(100_000.0)
    assert result["sleeve_limits"] == {"scalp": 35_000.0, "sniper": 65_000.0}


def test_dual_index_rotator_is_stable_on_a_tie():
    metrics = {"atr_velocity": 10.0, "obi_strength": 2.0, "parity_edge": 5.0,
               "kronos_confidence": 80.0}
    result = dual_index_rotator(metrics, metrics, previous_winner="BANKNIFTY")

    assert result["winner"] == "BANKNIFTY"
    assert result["rotated"] is False


# ---------------------------------------------------------------------------
# Radar snapshot and live bridge
# ---------------------------------------------------------------------------


def test_build_parallel_alpha_exposes_five_radar_rows_and_pnl():
    snapshot = build_parallel_alpha(
        parity={"trigger": True, "side": "CE", "status": "CE cheap"},
        lead_lag={"trigger": False, "status": "watching"},
        tsunami={"trigger": True, "side": "CE", "status": "5/5 BULL"},
        coiled_spring={"trigger": False, "status": "armed"},
        rotator={"trigger": False, "status": "NIFTY"},
        pnl={"parity": 6.5, "tsunami": 12.0, "lead_lag": 0.0,
             "coiled_spring": 0.0, "rotator": 0.0},
    )

    assert snapshot["active_count"] == 2
    assert snapshot["total_pnl_contribution"] == pytest.approx(18.5)
    assert [row["key"] for row in snapshot["radar"]] == [
        "parity", "lead_lag", "tsunami", "coiled_spring", "rotator"
    ]
    assert snapshot["parallel_ready"] is True
    assert snapshot["stale"] is False


def test_build_parallel_alpha_fails_closed_when_stale():
    snapshot = build_parallel_alpha(stale=True)

    assert snapshot["ok"] is False
    assert snapshot["stale"] is True
    assert snapshot["parallel_ready"] is False
    assert all(row["state"] == "WAITING" for row in snapshot["radar"])


def test_stateful_engine_preserves_tsunami_transition_and_rotation():
    engine = ParallelAlphaEngine(previous_bull_count=3)
    stocks = {
        name: _heavy(name)
        for name in ("HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS")
    }
    first = engine.evaluate(tsunami_stocks=stocks, tsunami_direction="BULL")
    second = engine.evaluate(tsunami_stocks=stocks, tsunami_direction="BULL")

    assert first["engines"]["tsunami"]["trigger"] is True
    assert second["engines"]["tsunami"]["trigger"] is False
    assert engine.previous_bull_count == 5


def test_live_tick_snapshot_carries_parallel_alpha(monkeypatch):
    now = 1_700_000_000.0
    payload = build_parallel_alpha(now=now)
    feed = {
        "updated_epoch": now,
        "updated_at": "2023-11-14T22:13:20+00:00",
        "market_open": True,
        "socket_connected": True,
        "prices": {"NSE_INDEX|Nifty 50": 24_300.0},
        "parallel_alpha": payload,
        "scalp_sleeve": {"scalp_capital_allocated": 35_000.0},
    }

    class Gate:
        status = "FEED_LIVE"
        allow_order_generation = True

        def as_dict(self):
            return {"status": self.status}

    def fake_load(path):
        return feed if path == live_ticks.FEED_FILE else {}

    monkeypatch.setattr(live_ticks, "_load", fake_load)
    monkeypatch.setattr(live_ticks, "feed_state", lambda *args, **kwargs: Gate())

    snap = live_ticks.snapshot("NIFTY", now=now)

    assert snap["live"] is True
    assert snap["parallel_alpha"]["engine_count"] == 5
    assert len(snap["parallel_alpha"]["radar"]) == 5
    assert snap["parallel_alpha_pnl"] == {
        "parity": 0.0, "lead_lag": 0.0, "tsunami": 0.0,
        "coiled_spring": 0.0, "rotator": 0.0,
    }
