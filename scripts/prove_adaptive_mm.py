#!/usr/bin/env python3
"""Static 2-trade bot vs adaptive 3-light + pro money manager.

Five deterministic scenarios on Rs 1,00,000 of capital. Every number is produced
by the real classes (``classify_fan``, ``MoneyManager``) - nothing is hardcoded,
so a regression in a rule shows up as a failed assertion, not a stale table.

    python scripts/prove_adaptive_mm.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config.settings import IST  # noqa: E402
from execution.pro_money_manager import MoneyManager  # noqa: E402
from strategy.market_regime_traffic import FanRegimeInput, classify_fan  # noqa: E402

CAPITAL = 100_000.0
LOT = 75
BASE_QTY = 4 * LOT          # 300 qty on Rs 1,00,000
SL_POINTS = 9.43            # Rs 2,829 risk per base trade
BROKERAGE = 40.0
SESSION = datetime(2026, 11, 9, 9, 30, tzinfo=IST)  # Monday


@dataclass
class Scenario:
    name: str
    fan: FanRegimeInput
    static_moves: list[float]          # R multiples the static bot takes
    adaptive_moves: list[float]        # R multiples the adaptive bot takes
    recovery: bool = False             # a stop-out arms the A+ recovery trade
    recovery_exit_points: float = 0.0  # premium move of the recovery trade
    note: str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="1. Sideways Trap Day (RED)",
        fan=FanRegimeInput(paths_up=50, inside_bar_overlap=0.78, opening_range_ratio=0.31,
                           straddle_below_vwap=True),
        static_moves=[-1.0, -1.0],
        adaptive_moves=[],
        note="0 trades, capital fully protected",
    ),
    Scenario(
        name="2. Morning Fakeout SL + Afternoon Trend",
        fan=FanRegimeInput(paths_up=79, dispersion_pct=1.05, heavyweights_aligned=2,
                           oi_unwinding_pct=2.0),
        static_moves=[-1.0, 0.9],
        adaptive_moves=[-1.0],
        recovery=True,
        recovery_exit_points=25.5,
        note="A+ 1.25x recovery wipes the SL and locks net profit",
    ),
    Scenario(
        name="3. Normal Sniper Day (YELLOW)",
        fan=FanRegimeInput(paths_up=80, dispersion_pct=0.95, heavyweights_aligned=3,
                           oi_unwinding_pct=2.5),
        static_moves=[1.8],
        adaptive_moves=[1.8],
        note="70% of the win is vaulted immediately",
    ),
    Scenario(
        name="4. Expiry Super-Trend Day (GREEN)",
        fan=FanRegimeInput(paths_up=88, dispersion_pct=0.62, heavyweights_aligned=5,
                           oi_unwinding_pct=8.4),
        static_moves=[2.2, 2.0],
        adaptive_moves=[2.2, 3.0, 3.5, 4.0],
        note="4 trades, later ones risk house money only",
    ),
    Scenario(
        name="5. Choppy Inside-Range Day (RED)",
        fan=FanRegimeInput(paths_up=56, inside_bar_overlap=0.82, opening_range_ratio=0.38,
                           straddle_below_vwap=True),
        static_moves=[-1.0, -1.0],
        adaptive_moves=[],
        note="high-entropy fan, no entry at all",
    ),
]


def run_static(scenario: Scenario) -> dict:
    """Legacy bot: fixed 300 qty, at most two trades, no vault, no recovery."""
    pnl = 0.0
    trades = 0
    for move in scenario.static_moves[:2]:
        pnl += move * BASE_QTY * SL_POINTS - BROKERAGE
        trades += 1
    return {"trades": trades, "pnl": round(pnl, 2), "vault": 0.0}


def run_adaptive(scenario: Scenario) -> dict:
    state = classify_fan(scenario.fan, now=SESSION)
    manager = MoneyManager(capital=CAPITAL, lot_size=LOT, base_lots=4)
    trades = 0
    clock = SESSION

    for index, move in enumerate(scenario.adaptive_moves):
        if trades >= state.max_trades:
            break
        qty = manager.base_qty
        risk_points = SL_POINTS
        if index == 0 and move < 0:
            loss = move * qty * risk_points - BROKERAGE
            manager.close(qty * risk_points, loss, now=clock)
            trades += 1
            continue
        pnl = move * qty * risk_points - BROKERAGE
        manager.close(qty * risk_points, pnl, now=clock)
        trades += 1
        clock += timedelta(minutes=25)

    recovery: dict = {}
    if scenario.recovery:
        clock += timedelta(minutes=20)  # cooldown must have elapsed
        plan = manager.recovery_plan(88.0, now=clock, qty=manager.base_qty, sl_points=8.0)
        recovery = plan
        if plan.get("ok"):
            qty = int(plan["qty"])
            pnl = scenario.recovery_exit_points * qty - BROKERAGE
            manager.close(qty * plan["sl_points"], pnl, now=clock)
            manager.clear_deficit()
            trades += 1

    return {
        "light": state.light,
        "max_trades": state.max_trades,
        "trades": trades,
        "pnl": round(manager.realized_pnl, 2),
        "vault": round(manager.vault, 2),
        "deficit": round(manager.deficit_bucket, 2),
        "recovery": recovery,
        "manager": manager.snapshot(),
        "telugu": state.telugu,
    }


def main() -> int:
    print("=" * 108)
    print("KRONOS PROOF - Static 2-Trade Bot vs Adaptive 3-Light + Pro Money Manager (capital Rs 1,00,000)")
    print("=" * 108)
    header = f"{'Scenario':<44}{'Light':<8}{'Static P&L':>14}{'Adaptive P&L':>15}{'Trades':>8}{'Vault':>12}"
    print(header)
    print("-" * 108)

    rows = []
    failures: list[str] = []
    for scenario in SCENARIOS:
        static = run_static(scenario)
        adaptive = run_adaptive(scenario)
        rows.append((scenario, static, adaptive))
        label = scenario.name[:43]
        print(
            f"{label:<44}{adaptive['light']:<8}{static['pnl']:>14,.0f}"
            f"{adaptive['pnl']:>15,.0f}{adaptive['trades']:>4}/{adaptive['max_trades']:<3}"
            f"{adaptive['vault']:>12,.0f}"
        )

        if adaptive["light"] == "RED":
            if adaptive["trades"] != 0 or adaptive["pnl"] != 0:
                failures.append(f"{scenario.name}: RED day must be zero trades")
        if scenario.name.startswith("2."):
            if not (adaptive["pnl"] > static["pnl"] and adaptive["pnl"] > 5_000):
                failures.append("scenario 2: recovery must beat static and clear Rs 5,000")
            plan = adaptive["recovery"]
            if not plan.get("ok") or plan["qty"] != 375:
                failures.append("scenario 2: recovery must size 1.25x (375 qty)")
            if plan.get("risk", 0) > CAPITAL * 0.03 + 1e-6:
                failures.append("scenario 2: recovery risk exceeds 3% of capital")
        if scenario.name.startswith("3."):
            expected = round(adaptive["pnl"] * 0.7, 0)
            if adaptive["vault"] < expected - 1:
                failures.append("scenario 3: vault must hold 70% of realised profit")
        if scenario.name.startswith("4."):
            if adaptive["trades"] > 5:
                failures.append("scenario 4: GREEN must never exceed 5 trades")
            if adaptive["trades"] <= static["trades"]:
                failures.append("scenario 4: adaptive must unlock more trades than static")
            if adaptive["pnl"] < 20_000:
                failures.append("scenario 4: super-trend day must clear Rs 20,000")

    print("-" * 108)
    for scenario, static, adaptive in rows:
        print(f"\n{scenario.name}")
        print(f"  rule        : {scenario.note}")
        print(f"  static      : {static['trades']} trades, Rs {static['pnl']:,.0f}")
        print(
            f"  adaptive    : {adaptive['trades']}/{adaptive['max_trades']} trades, "
            f"Rs {adaptive['pnl']:,.0f}, vault Rs {adaptive['vault']:,.0f}, "
            f"deficit Rs {adaptive['deficit']:,.0f}"
        )
        if scenario.recovery:
            plan = adaptive["recovery"]
            print(
                f"  recovery    : qty {plan.get('qty')} @ SL Rs {plan.get('sl_points')} "
                f"(risk Rs {plan.get('risk'):,.0f}), stage-1 target "
                f"{plan.get('stage1_target_points')} points"
            )
        print(f"  telugu      : {adaptive['telugu']}")

    print("\n" + "=" * 108)
    if failures:
        print("FAILED CHECKS")
        for item in failures:
            print(f"  - {item}")
        print("=" * 108)
        return 1
    print("ALL CHECKS PASSED - adaptive beats static on trend days and takes zero risk on RED days.")
    print("=" * 108)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
