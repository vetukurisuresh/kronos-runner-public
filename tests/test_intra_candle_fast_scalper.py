"""Intra-candle scalper: entry inside the forming candle, exit on the peak."""

from __future__ import annotations

from strategy.intra_candle_scalper import (
    PeakTickRatchet,
    arm_strike_cache,
    detect_mode,
    tick_velocity,
)


def _option(**over) -> dict:
    base = {
        "ltp": 210.0,
        "atp_vwap": 200.0,
        "obi_5": 1.6,
        "tbq_tsq": 1.6,
        "bid": 209.8,
        "ask": 210.2,
    }
    base.update(over)
    return base


def test_shockwave_fires_in_the_first_four_points_without_a_candle_close() -> None:
    ticks = [(0.0, 25000.0), (0.4, 25002.0), (0.8, 25004.0)]
    assert tick_velocity(ticks) == 5.0
    mode = detect_mode(
        spot_ticks=ticks,
        option=_option(),
        bars_5m=[{"o": 25000.0, "c": 25004.0}],
    )
    assert mode["mode"] == "SHOCKWAVE_VELOCITY_SCALP"
    assert mode["side"] == "CE"
    # Only 4 of the eventual 25 points have printed: no candle close needed.
    assert mode["velocity"] >= 5.0


def test_peak_tick_ratchet_books_before_the_wick_retraces() -> None:
    ratchet = PeakTickRatchet(entry=200.0, qty=75)
    assert ratchet.on_tick(205.0)["action"] == "HOLD"
    assert ratchet.risk_free is True
    assert ratchet.sl == 201.2

    t1 = ratchet.on_tick(208.0)
    assert t1["action"] == "T1_BOOK_PEAK_RATCHET"
    assert t1["qty"] == 45
    assert ratchet.on_tick(212.0)["action"] == "HOLD"

    exit_tick = ratchet.on_tick(209.8)
    assert exit_tick["action"] == "EXIT_PEAK_EXHAUSTION"
    assert exit_tick["qty"] == 30
    assert exit_tick["realized"] > 0
    # The +₹12 spike was protected; it never ran back to the entry.
    assert exit_tick["realized"] >= 600


def test_sideways_ping_pong_enters_at_the_box_floor() -> None:
    bars = [{"o": 100 + i, "c": float(100 + i)} for i in range(10)]
    mode = detect_mode(
        spot_ticks=[(0.0, 102.0), (1.0, 101.8)],
        option=_option(obi_5=1.5),
        bars_5m=bars,
    )
    assert mode["mode"] == "SIDEWAYS_PING_PONG_ABSORPTION"
    assert mode["side"] == "CE"
    assert mode["box_floor"] == 101.0


def test_wick_exhaustion_snaps_back_on_the_opposite_leg() -> None:
    mode = detect_mode(
        spot_ticks=[(0.0, 25000.0), (0.5, 25022.0)],
        option=_option(obi_5=0.40),
        bars_5m=[{"o": 25000.0, "c": 25022.0}],
        call_wall=25025.0,
        opposite_obi=1.62,
    )
    assert mode["mode"] == "WICK_EXHAUSTION_SNAPBACK"
    assert mode["side"] == "PE"
    assert mode["stretch_pts"] >= 18.0


def test_small_sl_and_stagnation_scratch() -> None:
    stopped = PeakTickRatchet(entry=200.0, qty=75)
    assert stopped.on_tick(188.0)["action"] == "EXIT_SL"

    slow = PeakTickRatchet(entry=200.0, qty=75, opened_at=0.0)
    assert slow.on_tick(200.5, now=21.0)["action"] == "SCRATCH_STAGNATION"


def test_cache_pre_arms_itm_contracts_and_oi_walls() -> None:
    rows = [
        {
            "strike_price": 25050,
            "call_options": {"market_data": {"oi": 180000, "delta": 0.60, "ltp": 210.0,
                                             "bid": 209.8, "ask": 210.2, "obi_5": 1.6}},
            "put_options": {"market_data": {"oi": 90000, "delta": 0.34, "ltp": 80.0}},
        },
        {
            "strike_price": 25100,
            "call_options": {"market_data": {"oi": 120000, "delta": 0.41, "ltp": 90.0}},
            "put_options": {"market_data": {"oi": 240000, "delta": 0.61, "ltp": 190.0,
                                            "bid": 189.8, "ask": 190.2, "obi_5": 1.4}},
        },
    ]
    cache = arm_strike_cache(
        {"option_chain": {"NIFTY": rows}, "spot": {"NIFTY": {"ltp": 25080.0}}},
        "NIFTY",
    )
    assert cache["hot_ce_contract"]["strike"] == 25050
    assert cache["hot_ce_contract"]["side"] == "CE"
    assert cache["hot_pe_contract"]["strike"] == 25100
    assert cache["hot_pe_contract"]["side"] == "PE"
    assert cache["call_wall_spot"] == 25050
    assert cache["put_wall_spot"] == 25100
    assert cache["armed"] is True


def test_tick_window_measures_live_velocity() -> None:
    from strategy.intra_candle_scalper import TickWindow

    w = TickWindow()
    w.add(0.0, 25000.0, 100.0, 1.2)
    w.add(0.5, 25003.0, 101.0, 1.4)
    w.add(1.0, 25006.0, 103.0, 1.5)
    assert w.spot_velocity() == 6.0
    assert w.option_velocity() == 3.0
    assert len(w.spot_series()) == 3


def test_arm_falls_back_to_the_live_gate_delta_without_inventing_a_strike() -> None:
    cache = arm_strike_cache({"spot": {"NIFTY": {"ltp": 25224.5}}}, "NIFTY", 25224.5,
                             gate={"delta": 0.5976, "max_pain": 25200.0})
    assert cache["armed"] is True
    assert cache["armed_source"] == "gate_25m"
    assert cache["strike_pending"] is True
    assert cache["hot_ce_contract"]["strike"] is None      # never fabricated
    assert cache["call_wall_spot"] == 25200.0


def test_order_plan_refuses_a_strike_less_contract() -> None:
    from strategy.intra_candle_scalper import plan_scalp_order

    mode = {"mode": "SHOCKWAVE_VELOCITY_SCALP", "side": "CE"}
    assert plan_scalp_order(mode, {"strike": None}, 210.0, 75) is None
    plan = plan_scalp_order(mode, {"strike": 25300, "symbol": "NIFTY"}, 210.0, 75)
    assert plan["sl"] == 198.45 and plan["t1"] == 217.5
    assert plan["sl_pct"] == -5.5 and plan["executor"] == "paper"
