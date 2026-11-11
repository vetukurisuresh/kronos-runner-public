"""Three-light regime controller tests."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config.settings import IST, MAX_TRADES_BY_LIGHT  # noqa: E402
from strategy.market_regime_traffic import (  # noqa: E402
    RegimeInput,
    can_enter,
    classify,
    trades_remaining,
)


def _weekday(hour: int, minute: int = 0, days_ahead: int = 0) -> datetime:
    moment = datetime(2026, 1, 5, hour, minute, tzinfo=IST)  # Monday
    while moment.weekday() != 0:
        moment += timedelta(days=1)
    return moment + timedelta(days=days_ahead)


GREEN_INPUT = RegimeInput(india_vix=13.5, atr_pct=0.55, trend_strength=0.75, oi_shift_pct=3.5)
RED_INPUT = RegimeInput(india_vix=26.0, atr_pct=1.6, trend_strength=0.10, oi_shift_pct=0.5)
MIXED_INPUT = RegimeInput(india_vix=18.0, atr_pct=0.90, trend_strength=0.45, oi_shift_pct=1.0)


def test_green_light_allows_five_trades() -> None:
    state = classify(GREEN_INPUT, now=_weekday(10, 30))
    assert state.light == "GREEN"
    assert state.max_trades == 5
    assert state.max_trades == MAX_TRADES_BY_LIGHT["GREEN"]
    assert state.tradeable is True


def test_high_vix_forces_red() -> None:
    state = classify(RED_INPUT, now=_weekday(10, 30))
    assert state.light == "RED"
    assert state.max_trades == 0
    assert state.tradeable is False
    assert any("VIX" in reason for reason in state.reasons)


def test_mixed_tape_is_yellow_with_two_trades() -> None:
    state = classify(MIXED_INPUT, now=_weekday(11, 0))
    assert state.light == "YELLOW"
    assert state.max_trades == 2


def test_late_day_forces_red() -> None:
    late = RegimeInput(india_vix=13.5, atr_pct=0.55, trend_strength=0.75, minutes_to_close=10)
    state = classify(late, now=_weekday(15, 15))
    assert state.light == "RED"
    assert any("min to close" in reason for reason in state.reasons)


def test_outside_session_is_red() -> None:
    state = classify(GREEN_INPUT, now=_weekday(16, 0))
    assert state.light == "RED"
    assert state.max_trades == 0
    assert "outside" in state.reasons[0]


def test_weekend_is_red() -> None:
    saturday = _weekday(10, 30, days_ahead=5)
    assert saturday.weekday() == 5
    assert classify(GREEN_INPUT, now=saturday).light == "RED"


def test_trade_budget_math() -> None:
    assert trades_remaining("GREEN", 0) == 5
    assert trades_remaining("GREEN", 3) == 2
    assert trades_remaining("GREEN", 9) == 0
    assert trades_remaining("YELLOW", 1) == 1
    assert trades_remaining("RED", 0) == 0
    assert trades_remaining("unknown", 0) == 0


def test_can_enter_respects_budget() -> None:
    assert can_enter("GREEN", 4) is True
    assert can_enter("GREEN", 5) is False
    assert can_enter("RED", 0) is False


def test_state_serialises_for_the_dashboard() -> None:
    payload = classify(GREEN_INPUT, now=_weekday(10, 30)).as_dict()
    assert payload["light"] == "GREEN"
    assert set(payload) == {"light", "max_trades", "score", "reasons"}
