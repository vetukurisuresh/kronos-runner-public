"""Post-trade house-money profit compounder.

The first winning scalp locks half of the profit in an untouchable vault and
the other half becomes the only risk pool for the rest of the day.  Once the
day is paid for, the base account is never risked again.  High-conviction A+
setups may then scale from 300 to 375/450 quantity, but only while the extra
risk remains fully inside that house-money pool.

No broker calls and no clock reads: callers pass ``now`` so the state machine
can be replayed deterministically.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.settings import to_ist

BASE_CAPITAL = 100_000.0
BASE_QTY = 300
LOT_SIZE = 75
BASE_RISK_PCT = 0.012
MAX_SCALPS = 6
#: 15s, not 90s: the fast sleeve is a ping-pong scalper and a 90-second lock
#: missed the entire re-entry leg on a 5m candle.  Matches the continuous
#: scalp engine's own COOLDOWN_SECONDS.
COOLDOWN_SECONDS = 15
DEFAULT_SL_POINTS = 4.0
VAULT_LOCK_PCT = 0.50
HOUSE_MONEY_FILE = "state/house_money_compounder.json"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class HouseMoneyCompounder:
    date: str = ""
    starting_capital: float = BASE_CAPITAL
    base_qty: int = BASE_QTY
    max_scalps: int = MAX_SCALPS
    trades: int = 0
    realized: float = 0.0
    vault: float = 0.0
    house_money_pool: float = 0.0
    win_streak: int = 0
    consecutive_sl: int = 0
    active: bool = False
    house_money_mode: bool = False
    locked: bool = False
    lock_reason: str = ""
    cooldown_until: datetime | None = None
    last_entry_at: datetime | None = None
    last_exit_at: datetime | None = None
    last_pnl: float = 0.0
    last_vault_sweep: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)

    def ensure_day(self, today: str) -> "HouseMoneyCompounder":
        if today and self.date != today:
            self.date = today
            self.trades = 0
            self.realized = 0.0
            self.vault = 0.0
            self.house_money_pool = 0.0
            self.win_streak = 0
            self.consecutive_sl = 0
            self.active = False
            self.house_money_mode = False
            self.locked = False
            self.lock_reason = ""
            self.cooldown_until = None
            self.last_entry_at = None
            self.last_exit_at = None
            self.last_pnl = 0.0
            self.last_vault_sweep = 0.0
            self.history = []
        return self

    @property
    def base_risk_budget(self) -> float:
        return round(self.starting_capital * BASE_RISK_PCT, 2)

    def cooldown_remaining(self, now: datetime | None = None) -> int:
        if not self.cooldown_until:
            return 0
        seconds = (to_ist(self.cooldown_until) - to_ist(now)).total_seconds()
        return max(0, math.ceil(seconds))

    @property
    def accelerator_ready(self) -> bool:
        return self.realized >= 5_000.0 and self.win_streak >= 2

    def _normalise_qty(self, qty: float) -> int:
        return max(0, int(qty // LOT_SIZE) * LOT_SIZE)

    def next_quantity(
        self,
        *,
        sl_points: float = DEFAULT_SL_POINTS,
        obi_5: float = 0.0,
        futures_volume_ratio: float = 0.0,
        a_plus: bool | None = None,
    ) -> dict[str, Any]:
        sl = min(4.50, max(3.50, _f(sl_points, DEFAULT_SL_POINTS)))
        if self.locked or self.active or self.trades >= self.max_scalps:
            return {"qty": 0, "risk_rs": 0.0, "funding": "NONE",
                    "reason": self.lock_reason or ("MAX_SCALPS" if self.trades >= self.max_scalps
                                                    else "TRADE_ACTIVE")}
        high_conviction = (
            bool(a_plus) if a_plus is not None
            else (_f(obi_5) >= 1.75 and _f(futures_volume_ratio) >= 2.5)
        )
        accelerated = bool(
            self.accelerator_ready and high_conviction
            and self.house_money_mode and self.house_money_pool > 0
        )
        multiplier = 1.0
        if accelerated:
            # +50% only after the pool can fund the entire 450-lot risk.
            if self.house_money_pool >= self.base_qty * 1.5 * sl:
                multiplier = 1.50
            else:
                multiplier = 1.25
        desired = self._normalise_qty(self.base_qty * multiplier) or self.base_qty

        if self.house_money_mode and self.house_money_pool > 0:
            affordable = self._normalise_qty(self.house_money_pool / sl)
            qty = min(desired, affordable)
            if qty < LOT_SIZE:
                return {"qty": 0, "risk_rs": 0.0, "funding": "HOUSE_MONEY",
                        "reason": "HOUSE_MONEY_EXHAUSTED",
                        "pool": round(self.house_money_pool, 2)}
            return {
                "qty": qty,
                "risk_rs": round(qty * sl, 2),
                "funding": "HOUSE_MONEY",
                "reason": "ACCELERATOR" if accelerated and qty > self.base_qty else "HOUSE_MONEY_SHIELD",
                "base_capital_at_risk_rs": 0.0,
                "pool": round(self.house_money_pool, 2),
                "sl_points": round(sl, 2),
            }

        affordable = self._normalise_qty(self.base_risk_budget / sl)
        qty = min(self._normalise_qty(self.base_qty), affordable)
        if qty < LOT_SIZE:
            return {"qty": 0, "risk_rs": 0.0, "funding": "BASE_CAPITAL",
                    "reason": "BASE_RISK_BUDGET_TOO_SMALL"}
        return {
            "qty": qty,
            "risk_rs": round(qty * sl, 2),
            "funding": "BASE_CAPITAL",
            "reason": "BASE_CAPITAL_START",
            "base_capital_at_risk_rs": round(qty * sl, 2),
            "pool": 0.0,
            "sl_points": round(sl, 2),
        }

    @property
    def next_qty(self) -> int:
        return int(self.next_quantity().get("qty") or 0)

    def can_open(
        self,
        now: datetime | None = None,
        *,
        sl_points: float = DEFAULT_SL_POINTS,
        obi_5: float = 0.0,
        futures_volume_ratio: float = 0.0,
        a_plus: bool | None = None,
    ) -> dict[str, Any]:
        if self.locked:
            return {"ok": False, "reason": self.lock_reason or "LOCKED", "qty": 0}
        if self.active:
            return {"ok": False, "reason": "SCALP_ALREADY_OPEN", "qty": 0}
        if self.trades >= self.max_scalps:
            return {"ok": False, "reason": "MAX_6_SCALPS", "qty": 0}
        waiting = self.cooldown_remaining(now)
        if waiting > 0:
            return {"ok": False, "reason": "POST_TRADE_COOLDOWN", "qty": 0,
                    "cooldown_sec": waiting}
        sizing = self.next_quantity(
            sl_points=sl_points, obi_5=obi_5,
            futures_volume_ratio=futures_volume_ratio, a_plus=a_plus,
        )
        if not sizing.get("qty"):
            return {"ok": False, **sizing}
        return {"ok": True, "reason": sizing.get("reason", "SCALP_READY"), **sizing}

    def register_entry(self, now: datetime | None = None) -> dict[str, Any]:
        if self.active:
            return {"ok": False, "reason": "SCALP_ALREADY_OPEN"}
        self.active = True
        self.last_entry_at = to_ist(now)
        return {"ok": True, "active": True, "trades": self.trades}

    def register_trade(self, pnl: Any, now: datetime | None = None) -> dict[str, Any]:
        """Settle a closed scalp; the first win permanently arms the shield."""
        amount = round(_f(pnl), 2)
        self.active = False
        self.trades += 1
        self.realized = round(self.realized + amount, 2)
        self.last_pnl = amount
        self.last_exit_at = to_ist(now)
        self.cooldown_until = to_ist(now) + timedelta(seconds=COOLDOWN_SECONDS)
        vault_add = 0.0
        if amount > 0:
            self.win_streak += 1
            self.consecutive_sl = 0
            vault_add = round(amount * VAULT_LOCK_PCT, 2)
            pool_add = round(amount - vault_add, 2)
            self.vault = round(self.vault + vault_add, 2)
            self.house_money_pool = round(self.house_money_pool + pool_add, 2)
            self.house_money_mode = True
        elif amount < 0:
            self.win_streak = 0
            self.consecutive_sl += 1
            if self.house_money_mode:
                self.house_money_pool = round(max(0.0, self.house_money_pool + amount), 2)
            if self.consecutive_sl >= 2:
                self.locked = True
                self.lock_reason = "2_CONSECUTIVE_SL"
        self.last_vault_sweep = vault_add
        if self.house_money_mode and self.house_money_pool < LOT_SIZE * DEFAULT_SL_POINTS:
            self.locked = True
            self.lock_reason = "HOUSE_MONEY_EXHAUSTED"

        record = {
            "trade": self.trades,
            "pnl": amount,
            "vault_add": vault_add,
            "vault": self.vault,
            "house_money_pool": self.house_money_pool,
            "realized": self.realized,
            "win_streak": self.win_streak,
            "consecutive_sl": self.consecutive_sl,
            "locked": self.locked,
        }
        self.history.append(record)
        return {**record, **self.snapshot(now)}

    def status(self, now: datetime | None = None) -> str:
        if self.locked:
            return "LOCKED"
        if self.active:
            return "SCALP_ACTIVE"
        if self.cooldown_remaining(now) > 0:
            return "DIAGNOSTIC_COOLDOWN"
        if self.trades >= self.max_scalps:
            return "MAX_SCALPS"
        if self.accelerator_ready:
            return "VOLATILITY_PROFIT_ACCELERATOR"
        if self.house_money_mode:
            return "HOUSE_MONEY_SHIELD"
        return "BASE_CAPITAL_START"

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        sizing = self.next_quantity()
        return {
            "date": self.date,
            "starting_capital": round(self.starting_capital, 2),
            "base_capital_safe_pct": 100.0,
            "trades": self.trades,
            "max_scalps": self.max_scalps,
            "realized": round(self.realized, 2),
            "vault": round(self.vault, 2),
            "house_money_pool": round(self.house_money_pool, 2),
            "win_streak": self.win_streak,
            "consecutive_sl": self.consecutive_sl,
            "house_money_mode": self.house_money_mode,
            "active": self.active,
            "locked": self.locked,
            "lock_reason": self.lock_reason,
            "cooldown_sec": self.cooldown_remaining(now),
            "next_qty": int(sizing.get("qty") or 0),
            "next_risk_rs": round(float(sizing.get("risk_rs") or 0.0), 2),
            "funding": sizing.get("funding", "BASE_CAPITAL"),
            "accelerator_ready": self.accelerator_ready,
            "status": self.status(now),
            "last_pnl": round(self.last_pnl, 2),
            "last_vault_sweep": round(self.last_vault_sweep, 2),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "starting_capital": self.starting_capital,
            "base_qty": self.base_qty,
            "max_scalps": self.max_scalps,
            "trades": self.trades,
            "realized": self.realized,
            "vault": self.vault,
            "house_money_pool": self.house_money_pool,
            "win_streak": self.win_streak,
            "consecutive_sl": self.consecutive_sl,
            "active": self.active,
            "house_money_mode": self.house_money_mode,
            "locked": self.locked,
            "lock_reason": self.lock_reason,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else "",
            "last_entry_at": self.last_entry_at.isoformat() if self.last_entry_at else "",
            "last_exit_at": self.last_exit_at.isoformat() if self.last_exit_at else "",
            "last_pnl": self.last_pnl,
            "last_vault_sweep": self.last_vault_sweep,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HouseMoneyCompounder":
        copied = dict(payload or {})
        for key in ("cooldown_until", "last_entry_at", "last_exit_at"):
            value = copied.get(key)
            if value:
                try:
                    copied[key] = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError:
                    copied[key] = None
        return cls(**{key: copied[key] for key in cls.__dataclass_fields__ if key in copied})

    def save(self, path: Path | str = HOUSE_MONEY_FILE) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str = HOUSE_MONEY_FILE, today: str = "") -> "HouseMoneyCompounder":
        try:
            state = cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            state = cls(date=today)
        return state.ensure_day(today)
