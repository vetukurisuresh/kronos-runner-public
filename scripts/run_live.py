"""Master orchestration loop.

Wires the layers together in the only order that is safe:

    session guard -> regime light -> ensemble -> confluence -> money manager
    -> order manager -> narration

Everything is dependency-free at import time so the relay job can start even
while heavy wheels are still installing.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import (  # noqa: E402
    INDEX_SYMBOLS,
    entry_allowed,
    get_settings,
    in_session_window,
    kill_switch_enabled,
    session_phase,
    to_ist,
)
from execution.order_manager import OrderManager, OrderRejected, atm_order  # noqa: E402
from execution.pro_money_manager import MoneyManager  # noqa: E402
from model.kronos_ensemble import KronosSignal, decide  # noqa: E402
from state.telugu_narration_logger import TeluguNarrator  # noqa: E402
from strategy.high_winrate_filter import evaluate  # noqa: E402
from strategy.market_regime_traffic import RegimeInput, classify, trades_remaining  # noqa: E402

TICK_SECONDS = int(os.environ.get("KRONOS_TICK_SECONDS", "60"))


def synthetic_signals(seed: float) -> list[KronosSignal]:
    """Deterministic stand-in until the weights are wired to the live feed."""
    confidence = 74.0 + (seed % 12)
    return [
        KronosSignal("base", 1, min(confidence + 4, 99)),
        KronosSignal("small", 1, min(confidence, 99)),
        KronosSignal("mini", 1, min(confidence - 3, 99)),
    ]


def run_once(manager: OrderManager, money: MoneyManager, narrator: TeluguNarrator,
             now: datetime | None = None) -> dict:
    moment = to_ist(now)
    if kill_switch_enabled():
        narrator.say("kill_switch")
        return {"status": "killed"}

    if not in_session_window(moment):
        return {"status": "market_closed", "phase": session_phase(moment)}

    regime = classify(
        RegimeInput(india_vix=14.2, atr_pct=0.62, trend_strength=0.68, oi_shift_pct=3.4),
        now=moment,
    )
    if not regime.tradeable:
        narrator.say(f"{regime.light.lower()}_light" if regime.light != "RED" else "red_light",
                     data=regime.as_dict())
        return {"status": "blocked", "regime": regime.light, "reasons": regime.reasons}

    decision = decide(synthetic_signals(moment.minute))
    if not decision.actionable:
        narrator.say("no_signal", english=decision.reason)
        return {"status": "no_signal", "reason": decision.reason}

    confluence = evaluate(
        kronos_confidence=decision.confidence,
        signal_direction=decision.direction,
        smc_bias=decision.direction,
        oi_velocity_pct=14.0,
        heavy_vwap_aligned=4,
        regime_light=regime.light,
    )
    if not confluence.passed:
        narrator.say("no_signal", english=f"confluence {confluence.score:.0f}/100")
        return {"status": "filtered", "score": confluence.score}

    notional = money.size(decision.confidence, a_plus=confluence.score >= 100)
    if notional <= 0:
        return {"status": "no_budget"}
    money.open(notional)

    try:
        fill = manager.place(
            atm_order(24_800, [24_750, 24_800, 24_850], "BUY", 50, 120.0,
                      option_type="CE" if decision.direction > 0 else "PE"),
            now=moment,
            regime_light=regime.light,
            executed=len(manager.fills),
        )
    except OrderRejected as exc:
        money.close(notional, 0.0)
        narrator.say("no_signal", english=f"order rejected: {exc}")
        return {"status": "rejected", "reason": str(exc)}

    narrator.say("entry", data={"symbol": fill.order.symbol, "confidence": decision.confidence})
    return {
        "status": "entered",
        "symbol": fill.order.symbol,
        "confidence": decision.confidence,
        "regime": regime.light,
        "remaining": trades_remaining(regime.light, len(manager.fills)),
    }


def relay(window_minutes: int = 195) -> int:
    deadline = datetime.now(tz=to_ist().tzinfo) + timedelta(minutes=max(window_minutes, 1))
    money = MoneyManager(capital=get_settings().capital)
    manager = OrderManager()
    narrator = TeluguNarrator.instance()
    narrator.say("boot", data={"mode": get_settings().mode, "relay_minutes": window_minutes})
    print(f"relay started for {window_minutes} min - {INDEX_SYMBOLS['NIFTY']}")

    while to_ist() < deadline:
        if not in_session_window():
            print("session closed - relay exiting cleanly")
            return 0
        result = run_once(manager, money, narrator)
        print(f"[{to_ist():%H:%M:%S}] {result}")
        time.sleep(max(TICK_SECONDS, 5))
    narrator.say("exit", english="relay window elapsed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRONOS master loop")
    parser.add_argument("--once", action="store_true", help="single evaluation, then exit")
    parser.add_argument("--relay", action="store_true", help="run until the relay window elapses")
    parser.add_argument("--window", type=int, default=int(os.environ.get("WINDOW_MINUTES", "195")))
    arguments = parser.parse_args(argv)

    if arguments.relay:
        return relay(arguments.window)

    manager = OrderManager()
    money = MoneyManager(capital=get_settings().capital)
    narrator = TeluguNarrator.instance()
    result = run_once(manager, money, narrator)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
