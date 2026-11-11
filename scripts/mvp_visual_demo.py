"""Time-machine blind replay + live shadow tester.

Replays a deterministic synthetic session through the real strategy chain and
prints what the engine would have decided, candle by candle. Useful for a
5-minute sanity check on a fresh runner with no market data and no API keys.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import IST, get_settings  # noqa: E402
from execution.pro_money_manager import MoneyManager  # noqa: E402
from execution.trailing_sl_engine import create, update  # noqa: E402
from model.kronos_ensemble import KronosSignal, decide  # noqa: E402
from strategy.high_winrate_filter import evaluate  # noqa: E402
from strategy.market_regime_traffic import RegimeInput, classify  # noqa: E402
from strategy.price_action_smc import analyze  # noqa: E402


def synthetic_candles(count: int = 60, seed: int = 7) -> list[dict[str, float]]:
    generator = random.Random(seed)
    price = 24_800.0
    candles = []
    start = datetime(2026, 11, 5, 9, 15, tzinfo=IST)  # a Thursday
    for index in range(count):
        drift = generator.uniform(-18, 22)
        opened = price
        closed = opened + drift
        high = max(opened, closed) + generator.uniform(1, 9)
        low = min(opened, closed) - generator.uniform(1, 9)
        candles.append(
            {
                "start": int((start + timedelta(minutes=index)).timestamp()),
                "open": round(opened, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(closed, 2),
                "volume": round(generator.uniform(1_000, 9_000), 2),
            }
        )
        price = closed
    return candles


def replay(count: int = 60, ticks: int = 5) -> list[str]:
    candles = synthetic_candles(count)
    regime = classify(RegimeInput(india_vix=14.1, atr_pct=0.61, trend_strength=0.71, oi_shift_pct=3.2),
                      now=datetime(2026, 11, 5, 10, 30, tzinfo=IST))
    money = MoneyManager(capital=get_settings().capital)
    lines = [f"regime={regime.light} max_trades={regime.max_trades}"]

    trail = None
    for index in range(20, len(candles) - 1):
        window = candles[index - 19 : index + 1]
        try:
            analysis = analyze(window, lookback=2)
        except ValueError:
            continue

        signal = decide([
            KronosSignal("base", analysis.bias or 1, 80.0),
            KronosSignal("small", analysis.bias or 1, 79.0),
            KronosSignal("mini", analysis.bias or 1, 78.0),
        ])
        if not signal.actionable:
            continue

        confluence = evaluate(
            kronos_confidence=signal.confidence,
            signal_direction=signal.direction,
            smc_bias=analysis.bias,
            oi_velocity_pct=13.5,
            heavy_vwap_aligned=4,
            regime_light=regime.light,
        )
        if not confluence.passed:
            continue

        entry = candles[index]["close"]
        # The initial stop must sit on the losing side, which flips with direction.
        initial_sl = entry - 25 if signal.direction > 0 else entry + 25
        trail = create(entry, initial_sl, signal.direction)
        notional = money.size(signal.confidence)
        if notional <= 0:
            continue
        money.open(notional)
        for tick in candles[index + 1 : index + 1 + ticks]:
            trail = update(trail, tick["close"])
        pnl = (trail.stop - entry) * trail.direction
        money.close(notional, pnl)
        lines.append(
            f"{datetime.fromtimestamp(candles[index]['start'], IST):%H:%M} "
            f"{signal.label:<5} conf={signal.confidence:5.1f} "
            f"vwap={analysis.vwap_side:<5} gear={trail.gear} pnl={pnl:+.2f}"
        )

    lines.append(f"summary: {money.snapshot()}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blind replay demo")
    parser.add_argument("--candles", type=int, default=60)
    arguments = parser.parse_args(argv)
    for line in replay(arguments.candles):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
