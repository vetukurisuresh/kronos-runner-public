"""Dual-sleeve capital router, Gear-5 runner and zero-zoom HUD contract tests."""

from __future__ import annotations

import time

import pytest

from execution.capital_sleeve_router import (
    CapitalSleeveRouter,
    SCALP_SLEEVE_CAPITAL,
    SNIPER_SLEEVE_CAPITAL,
    calculate_scalp_qty,
    clamp_scalp_stop,
    continuous_scalp_setup,
    gear5_runner_decision,
    house_money_entry,
    house_money_exit,
    route_dual_index,
    scalp_partial_plan,
    select_scalp_engine,
)
from execution.scalp_sleeve_ledger import ScalpSleeveLedger
from strategy.full_spectrum_alpha import build_snapshot, chain_option_ltp, evaluate


def _levels(count: int = 5):
    bids = [{"price": 24_999.5 - i * 0.5, "qty": 100 + i * 10} for i in range(count)]
    asks = [{"price": 25_000.5 + i * 0.5, "qty": 90 + i * 10} for i in range(count)]
    return bids, asks


def _chain(count: int = 11, *, ltp: float = 120.0):
    return [
        {
            "strike_price": 25_000 + (i - 5) * 50,
            "call_options": {"market_data": {"oi": 1_200, "oi_change": 10, "ltp": ltp}},
            "put_options": {"market_data": {"oi": 1_500, "oi_change": -10, "ltp": ltp - 5}},
        }
        for i in range(count)
    ]


def _snapshot(**overrides):
    bids, asks = _levels()
    kwargs = dict(
        symbol="NIFTY", spot=25_000,
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
        market_ohlc={k: {"open": 24_990, "high": 25_010, "low": 24_980, "close": 25_000}
                     for k in ("1m", "30m", "1d")},
        previous_basis=18.0, iv_velocity_5m=0.4, gamma_velocity_5m=0.1,
        kronos_move=15.0, side="CE", as_of="2026-11-10T10:00:00+05:30",
        generated_epoch=time.time(), stale=False, paths_up=70, samples=100,
        order_flow_divergence=True,
    )
    kwargs.update(overrides)
    return build_snapshot(**kwargs)


# -- P0 fail-closed reasons ------------------------------------------------- #
def test_p0_zero_quote_blocks_with_exact_reason_and_degraded_coverage() -> None:
    snap = _snapshot(spot=0, futures={"ltp": 0, "volume": 0}, greeks={}, chain=[])
    assert snap["alpha"]["allow_entry"] is False
    assert snap["alpha"]["conviction"] == "BLOCKED"
    assert snap["alpha"]["reason"] == "STALE_FEED"
    assert snap["coverage"]["pct"] < 100.0
    assert snap["coverage"]["status"] != "OK"


def test_p0_missing_each_structure_has_its_own_reason() -> None:
    missing_greeks = _snapshot(greeks={})
    assert missing_greeks["alpha"]["allow_entry"] is False
    assert missing_greeks["alpha"]["reason"] == "MISSING_GREEKS"
    direct = evaluate(chain=_chain(), spot=25_000, futures_ltp=25_020,
                      futures_volume=120_000, vix=13.4, depth={
                          "bids": _levels()[0], "asks": _levels()[1],
                          "obi_5": 1.4, "tbq_tsq": 1.38,
                      })
    assert direct["reason"] == "MISSING_GREEKS"

    missing_chain = _snapshot(chain=_chain(10))
    assert missing_chain["alpha"]["allow_entry"] is False
    assert missing_chain["alpha"]["reason"] == "MISSING_CHAIN"

    bids, asks = _levels(4)
    missing_depth = _snapshot(depth={"bids": bids, "asks": asks, "obi_5": 1.4,
                                     "bid_qty_5": 400, "ask_qty_5": 350,
                                     "tbq": 1_800, "tsq": 1_300})
    assert missing_depth["alpha"]["allow_entry"] is False
    assert missing_depth["alpha"]["reason"] == "MISSING_DEPTH"

    stale = _snapshot(stale=True)
    assert stale["alpha"]["allow_entry"] is False
    assert stale["alpha"]["reason"] == "STALE_FEED"


def test_p0_zero_option_ltp_and_fixture_are_never_live() -> None:
    zero = _snapshot(chain=_chain(ltp=0.0), option_ltp=0.0)
    assert zero["alpha"]["allow_entry"] is False
    assert zero["alpha"]["reason"] == "STALE_FEED"

    fixture = _snapshot(fixture=True)
    assert fixture["stale"] is True
    assert fixture["source"] == "offline-fixture"
    assert fixture["coverage"]["status"] == "OFFLINE_FIXTURE"
    assert fixture["alpha"]["allow_entry"] is False


def test_p0_chain_option_ltp_uses_nearest_strike() -> None:
    rows = _chain()
    assert chain_option_ltp(rows, 25_000, "CE") == pytest.approx(120.0)
    assert chain_option_ltp(rows, 25_000, "PE") == pytest.approx(115.0)


# -- Sleeve A --------------------------------------------------------------- #
def test_sleeve_a_mutex_is_exclusive_and_priority_order_is_enforced() -> None:
    router = CapitalSleeveRouter()
    first = router.try_scalp("PARITY_DISLOCATION_SCALP", 25_000)
    assert first["ok"] is True and first["sleeve"] == "A"
    assert router.available("A") == pytest.approx(10_000)

    overlapping = router.try_scalp("LEAD_LAG_3S_SCALP", 10_000)
    assert overlapping["ok"] is False
    assert overlapping["reason"] == "SLEEVE_MUTEX_BUSY"

    # Sleeve B is a different mutex and remains available concurrently.
    sniper = router.try_sniper("REVERSAL_AND_SMC_SNIPER", 60_000)
    assert sniper["ok"] is True

    closed = router.close("A", 1_000)
    assert closed["ok"] is True and closed["vault_add"] == pytest.approx(500)
    assert router.available("A") == pytest.approx(35_000)
    assert router.snapshot()["house_money_pool"] == pytest.approx(500)

    assert select_scalp_engine(["CONTINUOUS_VELOCITY_AND_PING_PONG_SCALP",
                                "LEAD_LAG_3S_SCALP",
                                "PARITY_DISLOCATION_SCALP"]) == "PARITY_DISLOCATION_SCALP"


def test_sleeve_a_sizing_is_inside_35k_and_has_tight_stop() -> None:
    qty = calculate_scalp_qty(120.0, 75)
    assert qty == 225
    assert 120.0 * qty <= SCALP_SLEEVE_CAPITAL
    assert clamp_scalp_stop(2.0) == 3.5
    assert clamp_scalp_stop(9.0) == 4.5
    plan = scalp_partial_plan(ltp=120.0, lot_size=75)
    assert plan["qty"] == 225
    assert plan["partial_qty"] == 112
    assert plan["lock_at_points"] == 6.5
    assert 3.5 <= plan["sl_points"] <= 4.5


# -- Sleeve B / Gear-5 ------------------------------------------------------- #
def test_sleeve_b_post_t2_gear5_banks_70_and_keeps_30pct_runner() -> None:
    decision = gear5_runner_decision(
        side="CE", smc_bos=True, obi_5=1.5, futures_volume_ratio=2.0,
        tbq=160, tsq=100, option_ltp=120, ema9=110, atp=108,
        oi_change_5m_pct=-4.0,
    )
    assert decision["runner_allowed"] is True
    assert decision["bank_pct"] == 70
    assert decision["runner_pct"] == 30
    assert decision["ratchet"] == "1m_CANDLE_TRAIL"

    fail_closed = gear5_runner_decision(
        side="CE", smc_bos=False, obi_5=0.9, futures_volume_ratio=0.2,
        tbq=80, tsq=100, option_ltp=120, ema9=130, atp=125,
        oi_change_5m_pct=2.0,
    )
    assert fail_closed["runner_allowed"] is False
    assert fail_closed["bank_pct"] == 100


def test_sleeve_b_rejects_overlap_but_accepts_other_macro_engine_after_release() -> None:
    router = CapitalSleeveRouter()
    assert router.try_sniper("HEAVYWEIGHT_5_OF_5_TSUNAMI", 50_000)["ok"] is True
    assert router.try_sniper("COILED_SPRING_BREAKOUT_TRAP", 50_000)["reason"] == "SLEEVE_MUTEX_BUSY"
    assert router.close("B", -500)["ok"] is True
    assert router.try_sniper("COILED_SPRING_BREAKOUT_TRAP", 60_000)["ok"] is True
    assert router.snapshot()["vault"] == pytest.approx(0.0)


def test_scalp_ledger_reports_ce_and_pe_wins_and_losses_separately() -> None:
    ledger = ScalpSleeveLedger()
    assert ledger.start_trade({"side": "CE", "entry": 100, "qty": 75})["ok"] is True
    ledger.finish_trade({"qty_open": 0, "realized_pnl_rs": 500, "status": "CLOSED"})
    assert ledger.start_trade({"side": "PE", "entry": 90, "qty": 75})["ok"] is True
    ledger.finish_trade({"qty_open": 0, "realized_pnl_rs": -200, "status": "CLOSED"})
    stats = ledger.stats()
    assert stats["ce_wins"] == 1 and stats["ce_losses"] == 0
    assert stats["pe_wins"] == 0 and stats["pe_losses"] == 1
    assert stats["ce_scalps"] == 1 and stats["pe_scalps"] == 1


def test_house_money_and_hud_roll_into_grand_total() -> None:
    router = CapitalSleeveRouter()
    assert house_money_entry(ltp=20, hero_score=95, expiry_day=True,
                             now="2026-11-10T14:00:00+05:30")["ok"] is True
    assert house_money_entry(ltp=7, hero_score=95, expiry_day=True,
                             now="2026-11-10T14:00:00+05:30")["ok"] is False
    assert house_money_exit(obi_5=0.67, multiple=2.1)["exit"] is True
    assert house_money_exit(obi_5=0.90, multiple=2.1)["lock_2x"] is True
    assert route_dual_index(55, 81)["symbol"] == "BANKNIFTY"
    assert continuous_scalp_setup(main_sniper_waiting=True, atr_1m=2,
                                  range_low=100, range_high=120)["allowed"] is False
    assert continuous_scalp_setup(main_sniper_waiting=True, atr_1m=2,
                                  range_low=100, range_high=160)["allowed"] is True

    router.try_scalp("PARITY_DISLOCATION_SCALP", 30_000)
    router.close("A", 700)
    router.try_sniper("REVERSAL_AND_SMC_SNIPER", 55_000)
    router.close("B", 1_300)
    snap = router.snapshot()
    assert snap["sleeve_a"]["realized"] == pytest.approx(700)
    assert snap["sleeve_b"]["realized"] == pytest.approx(1_300)
    assert snap["combined_realized"] == pytest.approx(2_000)
    assert snap["vault"] == pytest.approx(1_000)
    assert snap["hud"]["scalp_roi_pct_today"] == pytest.approx(2.0)
    assert snap["hud"]["combined_intraday_net_pnl"] == pytest.approx(2_000)
    assert SCALP_SLEEVE_CAPITAL + SNIPER_SLEEVE_CAPITAL == pytest.approx(100_000)
