"""Option-chart verifier, pro money manager and the KRONOS fan classifier."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config.settings import IST  # noqa: E402
from execution.pro_money_manager import (  # noqa: E402
    LimitChaser,
    MoneyManager,
    book_partials,
    close_remaining,
)
from market.option_picker import new_trade  # noqa: E402
from strategy.market_regime_traffic import (  # noqa: E402
    FanRegimeInput,
    classify_fan,
    trades_remaining,
)
from strategy.option_chart_verifier import (  # noqa: E402
    OptionSnapshot,
    oi_unwinding_pct,
    straddle_decay,
    verify_option_chart,
)

SESSION = datetime(2026, 11, 9, 10, 30, tzinfo=IST)  # Monday


# -- option chart ----------------------------------------------------------- #
def _option(**overrides) -> OptionSnapshot:
    base = dict(ltp=120.0, atp=115.0, swing_low=111.0, delta=0.52, oi=900_000, prev_oi=1_000_000,
                best_bid=119.95, best_ask=120.05)
    base.update(overrides)
    return OptionSnapshot(**base)


def test_clean_call_passes() -> None:
    verdict = verify_option_chart(_option(), "CE")
    assert verdict.ok is True
    assert verdict.sl_points == 9.0
    assert any("VWAP" in reason for reason in verdict.reasons)


def test_wide_swing_low_blocks_entry() -> None:
    verdict = verify_option_chart(_option(swing_low=100.0), "CE")
    assert verdict.ok is False
    assert any("exceeds" in blocker for blocker in verdict.blockers)


def test_recovery_requires_micro_stop() -> None:
    snapshot = _option(swing_low=111.5)  # 8.5 points is fine normally, not on recovery
    assert verify_option_chart(snapshot, "CE").ok is True
    assert verify_option_chart(snapshot, "CE", recovery=True).ok is False


def test_low_delta_blocks() -> None:
    assert verify_option_chart(_option(delta=0.30), "CE").ok is False


def test_put_must_sit_below_vwap() -> None:
    assert verify_option_chart(_option(ltp=118.0, atp=115.0), "PE").ok is False
    assert verify_option_chart(_option(ltp=110.0, atp=115.0, swing_low=104.0), "PE").ok is True


def test_oi_unwinding_and_straddle_decay() -> None:
    assert oi_unwinding_pct(900_000, 1_000_000) == 10.0
    assert oi_unwinding_pct(1_100_000, 1_000_000) == -10.0
    assert straddle_decay(100, 95, 90, 85) is True
    assert straddle_decay(100, 105, 90, 95) is False


# -- money manager ---------------------------------------------------------- #
def test_base_sizing_and_recovery_multiplier() -> None:
    manager = MoneyManager(capital=100_000)
    assert manager.base_qty == 300
    assert manager.risk_budget == 3_000
    plan = manager.recovery_plan(90)  # no deficit yet
    assert plan["ok"] is False


def test_stop_out_starts_cooldown_and_blocks_early_recovery() -> None:
    manager = MoneyManager(capital=100_000)
    manager.register_stop_out(2_829, now=SESSION)
    assert manager.deficit_bucket == 2_829
    early = manager.recovery_plan(90, now=SESSION + timedelta(minutes=5))
    assert early["ok"] is False and "cooldown" in early["blockers"][0]
    late = manager.recovery_plan(90, now=SESSION + timedelta(minutes=16))
    assert late["ok"] is True
    assert late["qty"] == 375 and late["risk"] <= 3_000
    assert late["stage1_target_points"] > 15


def test_recovery_needs_eighty_five_conviction() -> None:
    manager = MoneyManager(capital=100_000)
    manager.register_stop_out(2_829, now=SESSION)
    plan = manager.recovery_plan(84, now=SESSION + timedelta(minutes=16))
    assert plan["ok"] is False


def test_recovery_risk_never_exceeds_three_percent() -> None:
    manager = MoneyManager(capital=50_000)  # 3% = Rs 1,500
    manager.register_stop_out(2_000, now=SESSION)
    plan = manager.recovery_plan(90, now=SESSION + timedelta(minutes=16), sl_points=8.0)
    assert plan["ok"] is True
    assert plan["risk"] <= 1_500


def test_vault_locks_seventy_percent_after_five_thousand() -> None:
    manager = MoneyManager(capital=100_000)
    record = manager.close(30_000, 5_092, now=SESSION)
    assert manager.vault_locked is True
    assert manager.vault == 3_564.4
    assert record["vaulted"] == 3_564.4
    assert manager.house_money == 1_527.6


def test_house_money_caps_later_risk() -> None:
    manager = MoneyManager(capital=100_000)
    manager.close(30_000, 5_092, now=SESSION)
    assert manager.riskable("GREEN") == 1_527.6
    assert manager.riskable("RED") == 0.0


def test_t1_books_half_and_moves_stop_to_breakeven() -> None:
    trade = new_trade(100.0, 10.0, qty=300)          # T1 115, T2 130
    legs = book_partials(trade, 115.0)
    assert [leg["leg"] for leg in legs] == ["T1"]
    assert legs[0]["qty"] == 150 and legs[0]["pnl"] == 2250.0
    assert trade["qty_open"] == 150
    assert trade["realized_pnl_rs"] == 2250.0
    assert trade["current_trailing_sl"] == 100.0     # breakeven, not the old 90
    assert trade["status"] == "OPEN"
    assert trade["live_pnl_rs"] == 4500.0             # 2,250 booked + 2,250 open
    # Nothing is booked twice on the next tick.
    assert book_partials(trade, 116.0) == []
    assert trade["realized_pnl_rs"] == 2250.0


def test_t2_books_the_remaining_half_and_closes() -> None:
    trade = new_trade(100.0, 10.0, qty=300)
    book_partials(trade, 115.0)
    legs = book_partials(trade, 130.0)
    assert [leg["leg"] for leg in legs] == ["T2"]
    assert legs[0]["qty"] == 150 and legs[0]["pnl"] == 4500.0
    assert trade["qty_open"] == 0
    assert trade["realized_pnl_rs"] == 6750.0
    assert trade["live_pnl_rs"] == 6750.0
    assert trade["status"] == "CLOSED" and trade["exit_reason"] == "T2"
    assert trade["live_pnl_pct"] == 22.5


def test_stop_after_t1_only_realises_the_open_half() -> None:
    trade = new_trade(100.0, 10.0, qty=300)
    book_partials(trade, 115.0)
    leg = close_remaining(trade, 100.0, reason="SL")
    assert leg is not None and leg["qty"] == 150
    assert leg["pnl"] == 0.0                          # breakeven stop
    assert trade["realized_pnl_rs"] == 2250.0         # T1 banked, T2 half flat
    assert trade["status"] == "CLOSED" and trade["qty_open"] == 0
    assert trade["exit_reason"] == "SL"
    assert close_remaining(trade, 100.0) is None      # nothing left to book


def test_limit_chaser_prices_and_reprices() -> None:
    chaser = LimitChaser()
    assert chaser.entry_price(120.05) == 120.10
    should, price = chaser.needs_modify(120.10, 120.05, elapsed_ms=900)
    assert (should, price) == (False, 120.10)
    should, price = chaser.needs_modify(120.10, 120.20, elapsed_ms=900)
    assert (should, price) == (True, 120.25)
    assert chaser.needs_modify(120.10, 120.20, elapsed_ms=400)[0] is False


# -- fan classifier --------------------------------------------------------- #
def test_entropy_fan_forces_red() -> None:
    state = classify_fan(FanRegimeInput(paths_up=50), now=SESSION)
    assert state.light == "RED" and state.max_trades == 0
    assert "\u0c1f\u0c4d\u0c30\u0c47\u0c21\u0c4d\u0c38\u0c4d" in state.telugu


def test_operator_trap_forces_red_even_with_direction() -> None:
    trap = FanRegimeInput(paths_up=70, inside_bar_overlap=0.8, opening_range_ratio=0.3,
                          straddle_below_vwap=True)
    assert classify_fan(trap, now=SESSION).light == "RED"


def test_sniper_band_is_yellow() -> None:
    state = classify_fan(FanRegimeInput(paths_up=79, dispersion_pct=1.0,
                                        heavyweights_aligned=2), now=SESSION)
    assert state.light == "YELLOW" and state.max_trades == 2


def test_green_needs_cone_heavyweights_and_unwinding() -> None:
    full = FanRegimeInput(paths_up=88, dispersion_pct=0.6, heavyweights_aligned=5,
                          oi_unwinding_pct=8.0)
    assert classify_fan(full, now=SESSION).light == "GREEN"
    partial = FanRegimeInput(paths_up=88, dispersion_pct=0.6, heavyweights_aligned=2,
                             oi_unwinding_pct=8.0)
    assert classify_fan(partial, now=SESSION).light == "YELLOW"


def test_fan_budget_matches_trade_caps() -> None:
    assert trades_remaining("GREEN", 0) == 5
    assert trades_remaining("YELLOW", 1) == 1
    assert trades_remaining("RED", 0) == 0
