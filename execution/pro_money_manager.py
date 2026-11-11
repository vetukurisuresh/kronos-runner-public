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

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config.settings import DEPLOYMENT_PCT, HOUSE_VAULT_PCT, RECOVERY_MULTIPLIER, to_ist
from strategy.expiry_zero_to_hero import detect_peak_exhaustion

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

#: Top-5% sniper desk risk envelope.
SNIPER_MAX_TRADES_PER_DAY = 3
SNIPER_MAX_CONSECUTIVE_SL = 2
SNIPER_PROFIT_VAULT_PCT = 0.50
SNIPER_SL_MIN = 6.0
SNIPER_SL_MAX = 9.0
SNIPER_GUARD_FILE = "state/sniper_risk_guard.json"

#: Expiry-day Zero-to-Hero desk (separate from the Top-5% sniper guard).
HERO_MAX_TRADES_PER_DAY = 3
HERO_STAGE1_MULTIPLE = 2.0
HERO_BOOK_PCT = 0.35
HERO_STAGE1_TRAIL_MULTIPLE = 1.35
HERO_VAULT_PCT = 0.60
HERO_SILENT_WATCH_MINUTES = 5
HERO_GUARD_FILE = "state/expiry_hero_desk.json"


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


# --------------------------------------------------------------------------- #
# Top-5% sniper risk guard: frequency cap, 2-SL breaker, 50% vault
# --------------------------------------------------------------------------- #
def is_sl_outcome(outcome: Any) -> bool:
    text = str(outcome or "").strip().upper()
    return text in {"SL", "TSL", "TRAIL", "STOP", "STOP_LOSS", "SL_HIT"} or text.startswith("SL_")


@dataclass
class SniperRiskGuard:
    """Daily A+ frequency cap, consecutive-SL breaker and 50% profit vault."""

    date: str = ""
    starting_capital: float = 100_000.0
    max_trades: int = SNIPER_MAX_TRADES_PER_DAY
    max_consecutive_sl: int = SNIPER_MAX_CONSECUTIVE_SL
    trades: int = 0
    consecutive_sl: int = 0
    daily_lock: bool = False
    profit_vault: float = 0.0
    realized: float = 0.0
    t1_hits: int = 0
    t2_full_runs: int = 0
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    sl_points: list[float] = field(default_factory=list)

    def ensure_day(self, today: str) -> "SniperRiskGuard":
        if today and self.date != today:
            self.date = today
            self.trades = 0
            self.consecutive_sl = 0
            self.daily_lock = False
            self.profit_vault = 0.0
            self.realized = 0.0
            self.t1_hits = 0
            self.t2_full_runs = 0
            self.outcomes = []
            self.sl_points = []
        return self

    def can_open(self, grade: str = "A+") -> dict[str, Any]:
        if self.daily_lock:
            return {"ok": False, "reason": "DAILY_LOCK_2_SL"}
        if self.trades >= self.max_trades:
            return {"ok": False, "reason": "MAX_3_A_PLUS_TRADES"}
        if str(grade or "").upper() != "A+":
            return {"ok": False, "reason": "LOW_GRADE_SETUP"}
        return {"ok": True, "reason": "A_PLUS_SNIPER_OPEN"}

    def register_entry(self, grade: str = "A+") -> dict[str, Any]:
        check = self.can_open(grade)
        if check["ok"]:
            self.trades += 1
        return {**check, "trades": self.trades, "remaining": max(self.max_trades - self.trades, 0)}

    def register_leg(self, leg: dict[str, Any]) -> dict[str, Any]:
        """Sweep 50% of every positive T1/T2/manual/trail leg into the vault."""
        pnl = round(float(leg.get("pnl") or 0.0), 2)
        self.realized = round(self.realized + pnl, 2)
        swept = round(pnl * SNIPER_PROFIT_VAULT_PCT, 2) if pnl > 0 else 0.0
        self.profit_vault = round(self.profit_vault + swept, 2)
        name = str(leg.get("leg") or "").upper()
        if name == "T1":
            self.t1_hits += 1
        elif name == "T2":
            self.t2_full_runs += 1
        return {
            "pnl": pnl,
            "swept": swept,
            "profit_vault": self.profit_vault,
            "tradable_capital": round(max(self.starting_capital + self.realized - self.profit_vault, 0.0), 2),
        }

    def register_trade(self, *, pnl: float, outcome: str, sl_points: float = 0.0) -> dict[str, Any]:
        """Count a final closure; two stop-outs lock the rest of the day."""
        outcome = str(outcome or "").upper()
        self.outcomes.append({"pnl": round(float(pnl), 2), "outcome": outcome})
        if sl_points:
            self.sl_points.append(round(float(sl_points), 2))
        if is_sl_outcome(outcome):
            self.consecutive_sl += 1
        else:
            self.consecutive_sl = 0
        if self.consecutive_sl >= self.max_consecutive_sl:
            self.daily_lock = True
        return self.snapshot()

    def stats(self) -> dict[str, Any]:
        closed = len(self.outcomes)
        wins = sum(1 for row in self.outcomes if float(row.get("pnl") or 0.0) > 0)
        avg_sl = round(sum(self.sl_points) / len(self.sl_points), 2) if self.sl_points else 0.0
        return {
            "win_rate_pct": round(wins / closed * 100.0, 2) if closed else 0.0,
            "avg_sl_pts": avg_sl,
            "t1_hit_rate_pct": round(self.t1_hits / closed * 100.0, 2) if closed else 0.0,
            "t2_full_run_rate_pct": round(self.t2_full_runs / closed * 100.0, 2) if closed else 0.0,
            "profit_vault": round(self.profit_vault, 2),
            "net_roi_pct": round(self.realized / self.starting_capital * 100.0, 2)
            if self.starting_capital else 0.0,
            "realized": round(self.realized, 2),
            "trades": self.trades,
            "max_trades": self.max_trades,
            "consecutive_sl": self.consecutive_sl,
            "daily_lock": self.daily_lock,
        }

    def snapshot(self) -> dict[str, Any]:
        return {"date": self.date, **self.stats(), "t1_hits": self.t1_hits,
                "t2_full_runs": self.t2_full_runs, "outcomes": list(self.outcomes)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date, "starting_capital": self.starting_capital,
            "max_trades": self.max_trades, "max_consecutive_sl": self.max_consecutive_sl,
            "trades": self.trades, "consecutive_sl": self.consecutive_sl,
            "daily_lock": self.daily_lock, "profit_vault": self.profit_vault,
            "realized": self.realized, "t1_hits": self.t1_hits,
            "t2_full_runs": self.t2_full_runs, "outcomes": list(self.outcomes),
            "sl_points": list(self.sl_points),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SniperRiskGuard":
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})

    def save(self, path: Path | str = SNIPER_GUARD_FILE) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str = SNIPER_GUARD_FILE, today: str = "") -> "SniperRiskGuard":
        target = Path(path)
        try:
            guard = cls.from_dict(json.loads(target.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            guard = cls(date=today)
        return guard.ensure_day(today)


@dataclass
class HeroTradeDesk:
    """Three Hero trades per expiry day + five-minute Silent Watch loop."""

    date: str = ""
    max_trades: int = HERO_MAX_TRADES_PER_DAY
    trades: int = 0
    status: str = "WAITING"
    cooldown_until: datetime | None = None
    profit_vault: float = 0.0
    realized: float = 0.0
    last_exit_at: datetime | None = None
    last_exit_pnl: float = 0.0
    outcomes: list[dict[str, Any]] = field(default_factory=list)

    def ensure_day(self, today: str) -> "HeroTradeDesk":
        if today and self.date != today:
            self.date = today
            self.trades = 0
            self.status = "WAITING"
            self.cooldown_until = None
            self.profit_vault = 0.0
            self.realized = 0.0
            self.last_exit_at = None
            self.last_exit_pnl = 0.0
            self.outcomes = []
        return self

    def cooldown_remaining(self, now: datetime | None = None) -> int:
        if not self.cooldown_until:
            return 0
        remaining = (to_ist(self.cooldown_until) - to_ist(now)).total_seconds()
        return max(0, math.ceil(remaining / 60.0))

    def can_fire(self, now: datetime | None = None, *, allowed: bool = True) -> dict[str, Any]:
        if not allowed:
            return {"ok": False, "reason": "OUTSIDE_HERO_WINDOW"}
        if self.trades >= self.max_trades:
            return {"ok": False, "reason": "MAX_3_HERO_TRADES"}
        if self.cooldown_until and to_ist(now) < to_ist(self.cooldown_until):
            return {
                "ok": False,
                "reason": "SILENT_WATCH_COOLDOWN",
                "cooldown_min": self.cooldown_remaining(now),
            }
        return {"ok": True, "reason": "HERO_READY", "cooldown_min": 0}

    def register_entry(self) -> dict[str, Any]:
        if self.trades >= self.max_trades:
            return {"ok": False, "reason": "MAX_3_HERO_TRADES", "trades": self.trades}
        self.trades += 1
        self.status = "HERO_ACTIVE"
        return {"ok": True, "trades": self.trades, "remaining": self.max_trades - self.trades}

    def register_exit(self, pnl: float, now: datetime | None = None) -> dict[str, Any]:
        """Sweep 60% of the completed Hero trade into the vault and re-arm."""
        amount = round(float(pnl), 2)
        swept = round(max(amount, 0.0) * HERO_VAULT_PCT, 2)
        self.realized = round(self.realized + amount, 2)
        self.profit_vault = round(self.profit_vault + swept, 2)
        self.last_exit_at = to_ist(now)
        self.last_exit_pnl = amount
        self.outcomes.append({"pnl": amount, "swept": swept})
        if self.trades >= self.max_trades:
            self.status = "DONE"
            self.cooldown_until = None
        else:
            self.status = "SILENT_WATCH_FOR_NEXT_WAVE"
            self.cooldown_until = to_ist(now) + timedelta(minutes=HERO_SILENT_WATCH_MINUTES)
        return {
            "pnl": amount,
            "swept": swept,
            "profit_vault": self.profit_vault,
            "status": self.status,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else "",
            "trades": self.trades,
            "remaining": max(self.max_trades - self.trades, 0),
        }

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "date": self.date,
            "trades": self.trades,
            "max_trades": self.max_trades,
            "remaining": max(self.max_trades - self.trades, 0),
            "status": self.status,
            "silent_watch": self.status == "SILENT_WATCH_FOR_NEXT_WAVE",
            "cooldown_min": self.cooldown_remaining(now),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else "",
            "profit_vault": self.profit_vault,
            "realized": self.realized,
            "last_exit_pnl": self.last_exit_pnl,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "max_trades": self.max_trades,
            "trades": self.trades,
            "status": self.status,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else "",
            "profit_vault": self.profit_vault,
            "realized": self.realized,
            "last_exit_at": self.last_exit_at.isoformat() if self.last_exit_at else "",
            "last_exit_pnl": self.last_exit_pnl,
            "outcomes": list(self.outcomes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HeroTradeDesk":
        copied = dict(payload or {})
        for key in ("cooldown_until", "last_exit_at"):
            value = copied.get(key)
            if value:
                try:
                    copied[key] = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError:
                    copied[key] = None
        return cls(**{key: copied[key] for key in cls.__dataclass_fields__ if key in copied})

    def save(self, path: Path | str = HERO_GUARD_FILE) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str = HERO_GUARD_FILE, today: str = "") -> "HeroTradeDesk":
        try:
            desk = cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            desk = cls(date=today)
        return desk.ensure_day(today)


def expectancy_stats(
    rows: Any,
    *,
    month: str | None = None,
    starting_capital: float = 100_000.0,
    profit_vault: float = 0.0,
) -> dict[str, Any]:
    """Monthly win-rate/SL/T1/T2/vault/ROI tracker for the Ledger and HUD."""
    def get(row: Any, key: str, default: Any = 0.0) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    selected = []
    for row in rows or []:
        date = str(get(row, "date", "") or "")
        if month and not date.startswith(month):
            continue
        selected.append(row)
    total = len(selected)
    wins = [row for row in selected if float(get(row, "net", 0.0) or 0.0) > 0]
    losses = [row for row in selected if float(get(row, "net", 0.0) or 0.0) < 0]
    sl_rows = [row for row in selected
               if is_sl_outcome(get(row, "outcome", "") or get(row, "sl_reason", ""))
               or float(get(row, "sl_pnl", 0.0) or 0.0) < 0]
    sl_values = []
    for row in sl_rows:
        entry = float(get(row, "entry", 0.0) or 0.0)
        sl_price = float(get(row, "sl_price", 0.0) or 0.0)
        if entry > 0 and sl_price > 0:
            sl_values.append(abs(entry - sl_price))
    net = sum(float(get(row, "net", 0.0) or 0.0) for row in selected)
    avg_win = sum(float(get(row, "net", 0.0) or 0.0) for row in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(float(get(row, "net", 0.0) or 0.0) for row in losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / total * 100.0 if total else 0.0
    loss_rate = len(losses) / total * 100.0 if total else 0.0
    return {
        "month": month or "ALL",
        "trades": total,
        "win_rate_pct": round(win_rate, 2),
        "avg_sl_pts": round(sum(sl_values) / len(sl_values), 2) if sl_values else 0.0,
        "t1_hit_rate_pct": round(sum(1 for row in selected if float(get(row, "t1_qty", 0.0) or 0.0) > 0) / total * 100.0, 2) if total else 0.0,
        "t2_full_run_rate_pct": round(sum(1 for row in selected if float(get(row, "t2_qty", 0.0) or 0.0) > 0) / total * 100.0, 2) if total else 0.0,
        "profit_vault": round(float(profit_vault), 2),
        "net_roi_pct": round(net / float(starting_capital) * 100.0, 2) if starting_capital else 0.0,
        "net": round(net, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy_rs": round(win_rate / 100.0 * avg_win - loss_rate / 100.0 * avg_loss, 2),
    }


# --------------------------------------------------------------------------- #
# T1 / T2 partial booking
# --------------------------------------------------------------------------- #
T1_BOOK_PCT = 0.5


def init_partial_state(trade: dict[str, Any]) -> dict[str, Any]:
    """Make an older trade payload safe for partial booking."""
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    trade.setdefault("initial_qty", initial)
    trade.setdefault("qty_open", initial)
    trade.setdefault("realized_pnl_rs", 0.0)
    trade.setdefault("booked_legs", [])
    trade.setdefault("t1_booked", False)
    trade.setdefault("t2_booked", False)
    trade.setdefault("exit_reason", None)
    return trade


def _mark_open(trade: dict[str, Any], ltp: float) -> None:
    entry = float(trade.get("entry_price") or 0.0)
    open_qty = int(trade.get("qty_open") or 0)
    initial = int(trade.get("initial_qty") or trade.get("qty") or 0)
    realised = float(trade.get("realized_pnl_rs") or 0.0)
    pnl = realised + (float(ltp) - entry) * open_qty
    trade["live_pnl_rs"] = round(pnl, 0)
    trade["live_pnl_pct"] = round(pnl / (entry * initial) * 100.0, 2) if entry and initial else 0.0


def book_partials(trade: dict[str, Any], ltp: float, *,
                  ride_wave: bool = False) -> list[dict[str, Any]]:
    """T1 books 50% and moves the stop to breakeven; T2 books the rest.

    ``ride_wave`` skips the T2 booking when the 5-level OBI is above 1.75 and
    spot has cleared p90, leaving the remaining half to the Gear-3/4 trail.

    Returns only the legs booked by this call, so the caller can add exactly
    those rupees to the day HUD and narrate each one once.
    """
    init_partial_state(trade)
    if str(trade.get("status") or "").upper() != "OPEN":
        _mark_open(trade, ltp)
        return []
    entry = float(trade.get("entry_price") or 0.0)
    target1 = float(trade.get("target_1") or 0.0)
    target2 = float(trade.get("target_2_p90") or 0.0)
    legs: list[dict[str, Any]] = []

    if not trade.get("t1_booked") and target1 > entry and float(ltp) >= target1:
        qty = min(int(trade.get("qty_open") or 0), max(1, int(trade.get("initial_qty") or 0) // 2))
        if qty > 0:
            pnl = round((target1 - entry) * qty, 2)
            trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
            trade["realized_pnl_rs"] = round(float(trade.get("realized_pnl_rs") or 0.0) + pnl, 2)
            # Gear-2 breakeven locks Entry + Rs 1.50; never lower a trailed stop.
            trade["current_trailing_sl"] = round(
                max(float(trade.get("current_trailing_sl") or 0.0), entry + 1.50), 2
            )
            trade["t1_booked"] = True
            leg = {
                "leg": "T1", "qty": qty, "price": round(target1, 2), "pnl": pnl,
                "remaining": trade["qty_open"], "stop": trade["current_trailing_sl"],
                "realized": trade["realized_pnl_rs"],
            }
            trade.setdefault("booked_legs", []).append(leg)
            legs.append(leg)

    if (not trade.get("t2_booked") and not ride_wave and target2 > entry
            and float(ltp) >= target2 and int(trade.get("qty_open") or 0) > 0):
        qty = int(trade["qty_open"])
        pnl = round((target2 - entry) * qty, 2)
        trade["qty_open"] = 0
        trade["realized_pnl_rs"] = round(float(trade.get("realized_pnl_rs") or 0.0) + pnl, 2)
        trade["t2_booked"] = True
        trade["status"] = "CLOSED"
        trade["exit_reason"] = "T2"
        trade["exit_price"] = round(target2, 2)
        leg = {
            "leg": "T2", "qty": qty, "price": round(target2, 2), "pnl": pnl,
            "remaining": 0, "stop": trade.get("current_trailing_sl"),
            "realized": trade["realized_pnl_rs"],
        }
        trade.setdefault("booked_legs", []).append(leg)
        legs.append(leg)

    _mark_open(trade, ltp)
    return legs


def manage_hero_trade(
    trade: dict[str, Any],
    ltp: float,
    *,
    obi_5: float = 1.0,
    tbq: float = 0.0,
    tsq: float = 0.0,
    iv_velocity_1m: float = 0.0,
    peak_ltp: float | None = None,
) -> dict[str, Any]:
    """35% at 2x, 35% at p90, then a millisecond peak-exhaustion sweep.

    The remaining 30% is deliberately held for the top tick.  Stage 1 raises
    the stop to 1.35x entry, so the trade can never turn into a loser after the
    first double.  On peak exhaustion the remaining quantity is closed at the
    live premium and the caller hands the returned ``total_pnl`` to
    :class:`HeroTradeDesk` for the 60% vault sweep.
    """
    init_partial_state(trade)
    entry = float(trade.get("entry_price") or 0.0)
    price = float(ltp)
    result: dict[str, Any] = {
        "legs": [],
        "stage1": False,
        "stage2": False,
        "peak_exit": False,
        "stop_exit": False,
        "reason": "",
        "total_pnl": round(float(trade.get("realized_pnl_rs") or 0.0), 2),
        "peak_ltp": round(max(float(trade.get("hero_peak_ltp") or 0.0), price), 2),
    }
    if str(trade.get("status") or "").upper() != "OPEN" or entry <= 0:
        return result

    initial = max(int(trade.get("initial_qty") or trade.get("qty") or 0), 0)
    peak = max(float(trade.get("hero_peak_ltp") or entry), price, float(peak_ltp or 0.0))
    trade["hero_peak_ltp"] = round(peak, 2)
    trade["hero_mode"] = True
    trade["hero_stages"] = {
        "stage1_at": 2.0 * entry,
        "stage2_at": round(float(trade.get("target_2_p90") or 0.0), 2),
        "peak_ltp": round(peak, 2),
        "stage1_done": bool(trade.get("hero_stage1_done")),
        "stage2_done": bool(trade.get("hero_stage2_done")),
    }

    # Stage 1: 2x double-up lock, 35% booked, remaining stop at 1.35x entry.
    if (not trade.get("hero_stage1_done") and price >= HERO_STAGE1_MULTIPLE * entry
            and int(trade.get("qty_open") or 0) > 0):
        qty = min(
            int(trade.get("qty_open") or 0),
            max(1, int(round(initial * HERO_BOOK_PCT))),
        )
        if qty > 0:
            pnl = round((price - entry) * qty, 2)
            trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
            trade["realized_pnl_rs"] = round(
                float(trade.get("realized_pnl_rs") or 0.0) + pnl, 2
            )
            trade["current_trailing_sl"] = round(
                max(
                    float(trade.get("current_trailing_sl") or 0.0),
                    entry * HERO_STAGE1_TRAIL_MULTIPLE,
                ),
                2,
            )
            trade["hero_stage1_done"] = True
            trade["hero_stage1_at"] = round(price, 2)
            leg = {
                "leg": "HERO_2X",
                "qty": qty,
                "price": round(price, 2),
                "pnl": pnl,
                "remaining": trade["qty_open"],
                "stop": trade["current_trailing_sl"],
                "realized": trade["realized_pnl_rs"],
            }
            trade.setdefault("booked_legs", []).append(leg)
            result["legs"].append(leg)
            result["stage1"] = True

    # Stage 2: p90 Greek target, another 35% of the original quantity.
    p90 = float(trade.get("target_2_p90") or 0.0)
    if (not trade.get("hero_stage2_done") and p90 > entry and price >= p90
            and int(trade.get("qty_open") or 0) > 0):
        qty = min(
            int(trade.get("qty_open") or 0),
            max(1, int(round(initial * HERO_BOOK_PCT))),
        )
        if qty > 0:
            pnl = round((price - entry) * qty, 2)
            trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
            trade["realized_pnl_rs"] = round(
                float(trade.get("realized_pnl_rs") or 0.0) + pnl, 2
            )
            trade["hero_stage2_done"] = True
            trade["hero_stage2_at"] = round(price, 2)
            leg = {
                "leg": "HERO_P90",
                "qty": qty,
                "price": round(price, 2),
                "pnl": pnl,
                "remaining": trade["qty_open"],
                "stop": trade.get("current_trailing_sl"),
                "realized": trade["realized_pnl_rs"],
            }
            trade.setdefault("booked_legs", []).append(leg)
            result["legs"].append(leg)
            result["stage2"] = True

    # The 1.35x entry trail is a real stop, not decoration.  If the last 30%
    # rolls over before the peak signature, book it and let the desk cool down.
    stop_price = float(trade.get("current_trailing_sl") or 0.0)
    if (int(trade.get("qty_open") or 0) > 0 and stop_price > 0
            and price <= stop_price):
        stop_leg = close_remaining(
            trade, price,
            reason="HERO_TRAIL_SL" if trade.get("hero_stage1_done") else "HERO_SL",
        )
        if stop_leg:
            result["legs"].append(stop_leg)
        trade["hero_stop_exit"] = True
        result["stop_exit"] = True
        result["reason"] = str(trade.get("exit_reason") or "HERO_SL")

    # Stage 3: top-tick exhaustion.  Book the remaining 30% at market.
    detection = detect_peak_exhaustion(
        entry,
        price,
        peak,
        p90,
        obi_5=obi_5,
        tbq=tbq,
        tsq=tsq,
        iv_velocity_1m=iv_velocity_1m,
    )
    if detection["peak_exit"] and int(trade.get("qty_open") or 0) > 0:
        leg = close_remaining(trade, price, reason="HERO_PEAK_EXIT")
        if leg:
            result["legs"].append(leg)
        trade["hero_peak_exit"] = True
        trade["hero_exit_reason"] = detection["reason"]
        result["peak_exit"] = True
        result["reason"] = detection["reason"]

    trade["hero_peak_ltp"] = round(max(float(trade.get("hero_peak_ltp") or 0.0), price), 2)
    trade["hero_stages"] = {
        "stage1_at": round(2.0 * entry, 2),
        "stage2_at": round(p90, 2),
        "peak_ltp": trade["hero_peak_ltp"],
        "stage1_done": bool(trade.get("hero_stage1_done")),
        "stage2_done": bool(trade.get("hero_stage2_done")),
        "peak_exit": bool(trade.get("hero_peak_exit")),
        "last_detection": detection,
    }
    _mark_open(trade, price)
    result["total_pnl"] = round(float(trade.get("realized_pnl_rs") or 0.0), 2)
    result["remaining"] = int(trade.get("qty_open") or 0)
    result["status"] = str(trade.get("status") or "OPEN")
    return result


def close_remaining(trade: dict[str, Any], price: float | None = None,
                    reason: str = "SL") -> dict[str, Any] | None:
    """Realise whatever is still open at the stop (or a supplied fill price)."""
    init_partial_state(trade)
    qty = int(trade.get("qty_open") or 0)
    if qty <= 0:
        return None
    entry = float(trade.get("entry_price") or 0.0)
    fill = float(price if price is not None else (trade.get("current_trailing_sl") or entry))
    pnl = round((fill - entry) * qty, 2)
    trade["qty_open"] = 0
    trade["realized_pnl_rs"] = round(float(trade.get("realized_pnl_rs") or 0.0) + pnl, 2)
    trade["status"] = "CLOSED"
    trade["exit_reason"] = reason
    trade["exit_price"] = round(fill, 2)
    leg = {
        "leg": reason, "qty": qty, "price": round(fill, 2), "pnl": pnl,
        "remaining": 0, "stop": trade.get("current_trailing_sl"),
        "realized": trade["realized_pnl_rs"],
    }
    trade.setdefault("booked_legs", []).append(leg)
    _mark_open(trade, fill)
    return leg


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


# --------------------------------------------------------------------------- #
# Post-T2 Super-Trend Runner
# --------------------------------------------------------------------------- #
SUPER_TREND_T1_PCT = 0.40
SUPER_TREND_T2_BOOK_PCT = 0.30
SUPER_TREND_RUNNER_PCT = 0.30
SUPER_TREND_T1_STOP_BUFFER = 1.50
SUPER_TREND_ATR_MULT = 1.20
SUPER_TREND_CANDLE_BUFFER = 1.50


def evaluate_post_t2_continuation(
    *,
    smc_bull_bos: bool,
    smc_bear_choch: bool = False,
    obi_5: float = 0.0,
    tbq: float = 0.0,
    tsq: float = 0.0,
    futures_volume_ratio: float = 0.0,
    option_ltp: float = 0.0,
    ema9: float = 0.0,
    atp: float = 0.0,
    oi_change_5m_pct: float = 0.0,
) -> dict[str, Any]:
    """Run the four-point continuation test in well under one millisecond.

    Branch B is intentionally conservative: the T2 book is already banked and
    the remaining 30% is only kept alive when at least three independent
    evidence streams still agree.  No flow evidence means no runner.
    """
    started = time.perf_counter()
    checks = [
        {
            "key": "smc_structure",
            "label": "SMC structure still bullish BOS",
            "pass": bool(smc_bull_bos and not smc_bear_choch),
            "value": "BOS intact" if smc_bull_bos and not smc_bear_choch else "bearish CHoCH / no BOS",
        },
        {
            "key": "order_book",
            "label": "Buyers dominate 5-level book",
            "pass": bool(obi_5 >= 1.35 and tsq > 0 and tbq >= 1.20 * tsq),
            "value": f"OBI {obi_5:.2f} · TBQ/TSQ {(tbq / tsq):.2f}x" if tsq else "OBI/book missing",
        },
        {
            "key": "momentum_flow",
            "label": "Healthy futures flow + option above EMA-9/ATP",
            "pass": bool(
                futures_volume_ratio >= 1.0
                and option_ltp > 0
                and option_ltp >= ema9 > 0
                and option_ltp >= atp > 0
            ),
            "value": (
                f"flow {futures_volume_ratio:.2f}x · LTP {option_ltp:.2f} vs "
                f"EMA9 {ema9:.2f}/ATP {atp:.2f}"
            ),
        },
        {
            "key": "writer_unwind",
            "label": "Opposite writers trapped / unwinding",
            "pass": bool(oi_change_5m_pct <= -3.0),
            "value": f"5m ΔOI {oi_change_5m_pct:+.2f}%",
        },
    ]
    passed = sum(1 for check in checks if check["pass"])
    branch = (
        "BRANCH_B_GEAR5_SUPER_TREND_RUNNER"
        if passed >= 3 else "BRANCH_A_EXIT_FULL_AT_T2"
    )
    return {
        "branch": branch,
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "runner_allowed": branch.startswith("BRANCH_B"),
        "reasons": [check["label"] for check in checks if check["pass"]],
        "latency_us": round((time.perf_counter() - started) * 1_000_000.0, 3),
    }


def _book_qty(trade: dict[str, Any], pct: float, price: float, leg: str,
              *, stop: float | None = None) -> dict[str, Any] | None:
    init_partial_state(trade)
    qty = min(int(trade.get("qty_open") or 0), max(1, int(round(int(trade.get("initial_qty") or 0) * pct))))
    if qty <= 0:
        return None
    entry = float(trade.get("entry_price") or 0.0)
    pnl = round((float(price) - entry) * qty, 2)
    trade["qty_open"] = int(trade.get("qty_open") or 0) - qty
    trade["realized_pnl_rs"] = round(float(trade.get("realized_pnl_rs") or 0.0) + pnl, 2)
    trade.setdefault("booked_legs", []).append({
        "leg": leg, "qty": qty, "price": round(float(price), 2), "pnl": pnl,
        "remaining": trade["qty_open"], "stop": stop, "realized": trade["realized_pnl_rs"],
    })
    return trade["booked_legs"][-1]


def manage_post_t2_super_trend(
    trade: dict[str, Any],
    ltp: float,
    *,
    smc_bull_bos: bool,
    smc_bear_choch: bool = False,
    obi_5: float = 0.0,
    tbq: float = 0.0,
    tsq: float = 0.0,
    futures_volume_ratio: float = 0.0,
    ema9: float = 0.0,
    atp: float = 0.0,
    oi_change_5m_pct: float = 0.0,
    atr_1m: float = 0.0,
    latest_1m_low: float = 0.0,
) -> dict[str, Any]:
    """40% T1 / 30% T2 / 30% post-T2 runner with an upward-only trail."""
    init_partial_state(trade)
    price = float(ltp)
    entry = float(trade.get("entry_price") or 0.0)
    target1 = float(trade.get("target_1") or 0.0)
    target2 = float(trade.get("target_2_p90") or 0.0)
    result: dict[str, Any] = {
        "legs": [], "state": "OPEN", "runner_active": False,
        "runner_stop": None, "continuation": None, "reason": "",
    }
    if str(trade.get("status") or "").upper() != "OPEN" or entry <= 0:
        return result

    # 40% at p65; the remaining 60% gets a real breakeven + Rs 1.50 stop.
    if not trade.get("supertrend_t1_booked") and target1 > entry and price >= target1:
        leg = _book_qty(trade, SUPER_TREND_T1_PCT, target1, "SUPER_T1_40")
        if leg:
            trade["current_trailing_sl"] = round(
                max(float(trade.get("current_trailing_sl") or 0.0),
                    entry + SUPER_TREND_T1_STOP_BUFFER), 2
            )
            leg["stop"] = trade["current_trailing_sl"]
            trade["supertrend_t1_booked"] = True
            trade["t1_booked"] = True
            result["legs"].append(leg)

    # At p90, decide whether to bank the rest or arm the 30% runner.
    if not trade.get("supertrend_t2_booked") and target2 > entry and price >= target2:
        continuation = evaluate_post_t2_continuation(
            smc_bull_bos=smc_bull_bos, smc_bear_choch=smc_bear_choch,
            obi_5=obi_5, tbq=tbq, tsq=tsq,
            futures_volume_ratio=futures_volume_ratio,
            option_ltp=price, ema9=ema9, atp=atp,
            oi_change_5m_pct=oi_change_5m_pct,
        )
        result["continuation"] = continuation
        can_trail = atr_1m > 0 and latest_1m_low > 0
        if continuation["runner_allowed"] and can_trail:
            leg = _book_qty(trade, SUPER_TREND_T2_BOOK_PCT, target2, "SUPER_T2_30")
            if leg:
                result["legs"].append(leg)
            trade["supertrend_t2_booked"] = True
            trade["supertrend_runner"] = True
            trade["t2_booked"] = True
            runner_stop = max(
                target2 - SUPER_TREND_ATR_MULT * float(atr_1m),
                float(latest_1m_low) - SUPER_TREND_CANDLE_BUFFER,
            )
            trade["current_trailing_sl"] = round(max(
                float(trade.get("current_trailing_sl") or 0.0), runner_stop
            ), 2)
            result["runner_active"] = True
            result["runner_stop"] = trade["current_trailing_sl"]
            result["state"] = "POST_T2_SUPER_TREND_RUNNER"
            result["reason"] = "GEAR-5: 70% banked, 30% runner open"
        else:
            # Branch A: full exit at T2, or fail closed when no ATR/candle low
            # exists to define a legitimate trail.
            leg = _book_qty(trade, 1.0, target2, "SUPER_T2_EXIT_60")
            if leg:
                result["legs"].append(leg)
            trade["supertrend_t2_booked"] = True
            trade["t2_booked"] = True
            trade["status"] = "CLOSED"
            trade["qty_open"] = 0
            trade["exit_reason"] = "T2_FULL_EXIT"
            trade["exit_price"] = round(target2, 2)
            result["state"] = "CLOSED_T2"
            result["reason"] = (
                "T2: <3 continuation checks" if not continuation["runner_allowed"]
                else "T2: no ATR/candle low for a valid runner trail"
            )
            _mark_open(trade, price)
            return {**result, "total_pnl": round(float(trade.get("realized_pnl_rs") or 0.0), 2),
                    "remaining": 0}

    # After T2, ratchet the runner stop and exit on genuine exhaustion.
    if trade.get("supertrend_runner") and int(trade.get("qty_open") or 0) > 0:
        current_stop = float(trade.get("current_trailing_sl") or 0.0)
        if atr_1m > 0 and latest_1m_low > 0:
            candidate = max(
                price - SUPER_TREND_ATR_MULT * float(atr_1m),
                float(latest_1m_low) - SUPER_TREND_CANDLE_BUFFER,
            )
            if candidate > current_stop:
                trade["current_trailing_sl"] = round(candidate, 2)
                current_stop = trade["current_trailing_sl"]
        result["runner_stop"] = current_stop
        exhaustion = bool(obi_5 < 0.70 or smc_bear_choch)
        if price <= current_stop or exhaustion:
            leg = _book_qty(trade, 1.0, price, "SUPER_RUNNER_EXIT")
            if leg:
                result["legs"].append(leg)
            trade["status"] = "CLOSED"
            trade["qty_open"] = 0
            trade["exit_reason"] = "RUNNER_EXHAUSTION" if exhaustion else "RUNNER_TRAIL"
            trade["exit_price"] = round(price, 2)
            trade["supertrend_runner"] = False
            result["runner_active"] = False
            result["state"] = "CLOSED_RUNNER"
            result["reason"] = "order-flow exhaustion" if exhaustion else "runner trail hit"
        else:
            result["runner_active"] = True
            result["state"] = "POST_T2_SUPER_TREND_RUNNER"
            result["reason"] = f"Runner alive · trail ₹{current_stop:,.2f}"

    _mark_open(trade, price)
    result["total_pnl"] = round(float(trade.get("realized_pnl_rs") or 0.0), 2)
    result["remaining"] = int(trade.get("qty_open") or 0)
    return result


if __name__ == "__main__":  # pragma: no cover
    manager = MoneyManager(capital=100_000)
    manager.open(manager.size(82))
    print(manager.snapshot())
    print(manager.close(35_000, 4_000))
    print(manager.snapshot())
