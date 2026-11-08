"""Pro money management: 35% deployment, A+ recovery ladder, 70% vault.

Rules enforced here:

* base deployment is 35% of capital; 4 lots (300 qty) on a Rs 1,00,000 account
* a stop-out records the exact rupee loss in ``deficit_bucket``, starts a
  15-minute cooldown and unlocks a single *A+ recovery* setup that needs 85/100
  conviction, a micro-SL (``<= Rs 8``) and only 1.25x size - never martingale
* recovery size is only allowed while total rupee risk stays ``<= 3%`` of capital
* stage-1 recovery target wipes the deficit, brokerage and 1R of profit in one
  standard 15-18 point option move
* once realised profit reaches Rs 5,000, 70% is locked in ``vault`` and every
  later trade may risk only the remaining house money
* entries are limit orders at ``best_ask + 1 tick`` with an 800 ms modify path

No broker calls, no clock reads: every method takes ``now`` so the proof script
and the tests can replay a whole session deterministically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from config.settings import DEPLOYMENT_PCT, HOUSE_VAULT_PCT, RECOVERY_MULTIPLIER, to_ist

DEFAULT_LOT_SIZE = 75
DEFAULT_BASE_LOTS = 4
RECOVERY_CONVICTION = 85.0
RECOVERY_SL_POINTS = 8.0
MAX_RISK_PCT = 0.03
COOLDOWN_MINUTES = 15
VAULT_THRESHOLD = 5_000.0
BROKERAGE_PER_TRADE = 40.0
TICK_SIZE = 0.05
MODIFY_AFTER_MS = 800


@dataclass
class MoneyManager:
    capital: float
    deployment_pct: float = DEPLOYMENT_PCT
    recovery_multiplier: float = RECOVERY_MULTIPLIER
    vault_pct: float = HOUSE_VAULT_PCT
    deployed: float = 0.0
    vault: float = 0.0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    trades: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- pro overlay -------------------------------------------------------
    lot_size: int = DEFAULT_LOT_SIZE
    base_lots: int = DEFAULT_BASE_LOTS
    brokerage_per_trade: float = BROKERAGE_PER_TRADE
    max_risk_pct: float = MAX_RISK_PCT
    recovery_conviction: float = RECOVERY_CONVICTION
    recovery_sl_points: float = RECOVERY_SL_POINTS
    vault_threshold: float = VAULT_THRESHOLD
    deficit_bucket: float = 0.0
    recovery_active: bool = False
    cooldown_until: datetime | None = None
    vault_locked: bool = False
    #: the day's opening capital - risk budget stays anchored to it
    starting_capital: float | None = None

    def __post_init__(self) -> None:
        if self.starting_capital is None:
            self.starting_capital = self.capital

    # -- sizing ------------------------------------------------------------
    @property
    def base_budget(self) -> float:
        return self.capital * self.deployment_pct

    @property
    def hard_cap(self) -> float:
        return self.base_budget * self.recovery_multiplier

    @property
    def base_qty(self) -> int:
        return self.base_lots * self.lot_size

    @property
    def risk_budget(self) -> float:
        """Hard rupee risk ceiling per trade (3% of capital)."""
        base = self.starting_capital or self.capital
        return round(base * self.max_risk_pct, 2)

    @property
    def house_money(self) -> float:
        """Realised profit still allowed to work after the vault sweep."""
        return round(max(self.realized_pnl - self.vault, 0.0), 2)

    def available(self, recovering: bool = False) -> float:
        ceiling = self.hard_cap if recovering else self.base_budget
        return max(0.0, ceiling - self.deployed)

    def size(self, confidence: float, a_plus: bool = False) -> float:
        """Order notional for the next trade, respecting the exposure ceiling."""
        if confidence < 0:
            return 0.0
        recovering = a_plus and self.consecutive_losses > 0
        target = self.base_budget * (self.recovery_multiplier if recovering else 1.0)
        scaled = target * min(1.0, max(0.5, confidence / 100.0))
        return round(min(scaled, self.available(recovering)), 2)

    # -- lifecycle ---------------------------------------------------------
    def open(self, notional: float) -> None:
        if notional <= 0:
            raise ValueError("notional must be positive")
        if self.deployed + notional > self.hard_cap + 1e-6:
            raise ValueError(
                f"exposure cap breached: {self.deployed + notional:.2f} > {self.hard_cap:.2f}"
            )
        self.deployed += notional

    def close(self, notional: float, pnl: float, now: datetime | None = None) -> dict[str, float]:
        """Settle a trade: realise PnL and sweep 70% of profit into the vault."""
        self.deployed = max(0.0, self.deployed - notional)
        self.realized_pnl = round(self.realized_pnl + pnl, 2)
        self.capital = round(self.capital + pnl, 2)
        self.trades += 1

        if pnl < 0:
            self.consecutive_losses += 1
            self.register_stop_out(pnl, now)
        else:
            self.consecutive_losses = 0

        vaulted = 0.0
        if self.realized_pnl >= self.vault_threshold and not self.vault_locked:
            vaulted = self.lock_vault()

        record = {
            "trade": self.trades,
            "pnl": round(pnl, 2),
            "vaulted": round(vaulted, 2),
            "vault": round(self.vault, 2),
            "capital": round(self.capital, 2),
            "deployed": round(self.deployed, 2),
        }
        self.history.append(record)
        return record

    # -- deficit SL recovery ----------------------------------------------
    def register_stop_out(self, loss: float, now: datetime | None = None) -> dict[str, Any]:
        """Record the exact loss, start the cooldown, arm one A+ recovery."""
        amount = abs(float(loss))
        self.deficit_bucket = round(self.deficit_bucket + amount, 2)
        self.recovery_active = True
        moment = to_ist(now)
        self.cooldown_until = moment + timedelta(minutes=COOLDOWN_MINUTES)
        return {
            "deficit": self.deficit_bucket,
            "cooldown_until": self.cooldown_until.isoformat(),
        }

    def cooldown_remaining(self, now: datetime | None = None) -> int:
        if not self.cooldown_until:
            return 0
        remaining = (self.cooldown_until - to_ist(now)).total_seconds()
        return max(0, math.ceil(remaining / 60.0))

    def recovery_plan(
        self,
        conviction: float,
        now: datetime | None = None,
        *,
        qty: int | None = None,
        sl_points: float | None = None,
    ) -> dict[str, Any]:
        """A+ recovery sizing. Refuses anything that is not a strict upgrade."""
        blockers: list[str] = []
        if not self.recovery_active or self.deficit_bucket <= 0:
            blockers.append("no deficit to recover")
        waiting = self.cooldown_remaining(now)
        if waiting > 0:
            blockers.append(f"cooldown {waiting} min remaining")
        if conviction < self.recovery_conviction:
            blockers.append(f"conviction {conviction:.0f} < {self.recovery_conviction:.0f}")

        micro_sl = float(sl_points or self.recovery_sl_points)
        if micro_sl > self.recovery_sl_points:
            blockers.append(f"micro-SL {micro_sl:.1f} > Rs {self.recovery_sl_points:.0f}")

        base_qty = int(qty or self.base_qty)
        candidate_qty = int(round(base_qty * self.recovery_multiplier))
        risk = round(candidate_qty * micro_sl, 2)
        if risk > self.risk_budget + 1e-6:
            candidate_qty = int(self.risk_budget // micro_sl)
            risk = round(candidate_qty * micro_sl, 2)

        if blockers:
            return {
                "ok": False,
                "blockers": blockers,
                "deficit": self.deficit_bucket,
                "cooldown_min": waiting,
            }

        target_points = self.stage1_target(candidate_qty, micro_sl)
        return {
            "ok": True,
            "qty": candidate_qty,
            "sl_points": round(micro_sl, 2),
            "risk": risk,
            "risk_pct": round(risk / self.capital * 100.0, 2),
            "stage1_target_points": target_points,
            "stage1_net": round(self.deficit_bucket + self.brokerage_per_trade
                                + candidate_qty * micro_sl, 2),
            "deficit": self.deficit_bucket,
        }

    def stage1_target(self, qty: int, sl_points: float = RECOVERY_SL_POINTS) -> float:
        """Option points needed to clear deficit + brokerage + 1R of profit."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        need = self.deficit_bucket + self.brokerage_per_trade + qty * sl_points
        return round(need / qty, 2)

    def clear_deficit(self) -> None:
        self.deficit_bucket = 0.0
        self.recovery_active = False
        self.cooldown_until = None

    # -- profit vault ------------------------------------------------------
    def lock_vault(self) -> float:
        """Move 70% of realised profit into the untouchable vault (once)."""
        if self.vault_locked:
            return 0.0
        amount = round(max(self.realized_pnl, 0.0) * self.vault_pct, 2)
        self.vault = round(self.vault + amount, 2)
        self.vault_locked = True
        return amount

    def riskable(self, light: str = "GREEN") -> float:
        """Rupee risk still permitted: house money only once the vault is locked."""
        if light.upper() == "RED":
            return 0.0
        if not self.vault_locked:
            return self.risk_budget
        return round(min(self.risk_budget, max(self.house_money, 0.0)), 2)

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict[str, float | bool | str]:
        return {
            "capital": round(self.capital, 2),
            "deployed": round(self.deployed, 2),
            "deployable": round(self.available(), 2),
            "hard_cap": round(self.hard_cap, 2),
            "vault": round(self.vault, 2),
            "vault_locked": self.vault_locked,
            "house_money": self.house_money,
            "realized_pnl": round(self.realized_pnl, 2),
            "deficit": round(self.deficit_bucket, 2),
            "recovery_active": self.recovery_active,
            "qty": int(round(self.base_qty * self.recovery_multiplier)) if self.recovery_active
                   else self.base_qty,
        }


@dataclass
class LimitChaser:
    """Limit at best_ask + 1 tick, re-priced after 800 ms if unfilled."""

    tick_size: float = TICK_SIZE
    modify_after_ms: int = MODIFY_AFTER_MS

    def entry_price(self, best_ask: float, best_bid: float = 0.0) -> float:
        reference = best_ask if best_ask > 0 else best_bid
        if reference <= 0:
            raise ValueError("need a live best bid or ask")
        return round(reference + self.tick_size, 2)

    def needs_modify(self, limit_price: float, best_ask: float, elapsed_ms: int) -> tuple[bool, float]:
        """Return ``(should_modify, new_price)`` for an unfilled limit order."""
        if elapsed_ms < self.modify_after_ms or best_ask <= 0:
            return False, limit_price
        target = self.entry_price(best_ask)
        return (abs(target - limit_price) >= self.tick_size / 2), target


if __name__ == "__main__":  # pragma: no cover
    manager = MoneyManager(capital=100_000)
    manager.open(manager.size(82))
    print(manager.snapshot())
    print(manager.close(35_000, 4_000))
    print(manager.snapshot())
