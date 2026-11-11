"""Mutex-locked ₹35,000 scalp and ₹65,000 sniper capital sleeves.

The two books are deliberately separate.  A scalp can never consume capital
reserved for a macro trade, and a macro trade can never block the scalp sleeve.
The split is hard-coded to the existing ₹1,00,000 account: no extra leverage or
capital is invented.  All methods are pure in-memory state machines so the live
session and the dashboard can replay them deterministically in tests.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable

from execution.pro_money_manager import evaluate_post_t2_continuation

IST = timezone(timedelta(hours=5, minutes=30))
TOTAL_CAPITAL = 100_000.0
SCALP_SLEEVE_CAPITAL = 35_000.0
SNIPER_SLEEVE_CAPITAL = 65_000.0
SLEEVE_A = "A"
SLEEVE_B = "B"
SLEEVE_C = "C"
SLEEVE_CAPITAL = {SLEEVE_A: SCALP_SLEEVE_CAPITAL, SLEEVE_B: SNIPER_SLEEVE_CAPITAL}
VAULT_LOCK_PCT = 0.50
SCALP_MIN_SL = 3.50
SCALP_MAX_SL = 4.50
SCALP_LOCK_TARGET = 6.50
HOUSE_MONEY_START = time(13, 20)
HOUSE_MONEY_END = time(15, 10)

#: Sleeve A is a priority queue, not three simultaneous positions.
SCALP_PRIORITY = {
    "PARITY_DISLOCATION_SCALP": 1,
    "LEAD_LAG_3S_SCALP": 2,
    "CONTINUOUS_VELOCITY_AND_PING_PONG_SCALP": 3,
}
SCALP_ENGINES = tuple(SCALP_PRIORITY)
SNIPER_PRIORITY = {
    "REVERSAL_AND_SMC_SNIPER": 1,
    "HEAVYWEIGHT_5_OF_5_TSUNAMI": 2,
    "COILED_SPRING_BREAKOUT_TRAP": 3,
}
SNIPER_ENGINES = tuple(SNIPER_PRIORITY)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _as_ist(moment: datetime | str | float | None = None) -> datetime:
    if moment is None:
        return datetime.now(tz=IST)
    if isinstance(moment, (int, float)):
        return datetime.fromtimestamp(float(moment), tz=IST)
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def engine_name(value: Any) -> str:
    """Normalise any caller's engine spelling to the canonical constant."""
    return str(value or "").strip().upper().replace(" ", "_").replace("-", "_")


def sleeve_priority(sleeve: str, engine: str) -> int | None:
    key = engine_name(engine)
    table = SCALP_PRIORITY if str(sleeve).upper() == SLEEVE_A else SNIPER_PRIORITY
    return table.get(key)


def calculate_scalp_qty(ltp: Any, lot_size: Any, capital: Any = SCALP_SLEEVE_CAPITAL) -> int:
    """``floor(capital / (ltp * lot_size)) * lot_size`` with zero fake fills."""
    price = _f(ltp)
    lot = int(_f(lot_size))
    budget = _f(capital, SCALP_SLEEVE_CAPITAL)
    if price <= 0 or lot <= 0 or budget <= 0:
        return 0
    return int(math.floor(budget / (price * lot))) * lot


def clamp_scalp_stop(points: Any) -> float:
    """Keep the fast sleeve inside its promised ₹3.50–₹4.50 risk band."""
    return round(max(SCALP_MIN_SL, min(SCALP_MAX_SL, _f(points, SCALP_MIN_SL))), 2)


def scalp_partial_plan(*, ltp: Any, lot_size: Any, capital: Any = SCALP_SLEEVE_CAPITAL) -> dict[str, Any]:
    """Sizing plus the mandatory 50% lock at +₹6.50 option points."""
    price = _f(ltp)
    qty = calculate_scalp_qty(price, lot_size, capital)
    half = max(int(round(qty * 0.5)), 0)
    return {
        "qty": qty,
        "capital": round(price * qty, 2),
        "lot_size": int(_f(lot_size)),
        "sl_points": clamp_scalp_stop(SCALP_MIN_SL + (SCALP_MAX_SL - SCALP_MIN_SL) * 0.5),
        "partial_qty": half,
        "lock_at_points": SCALP_LOCK_TARGET,
        "lock_at_price": round(price + SCALP_LOCK_TARGET, 2) if price else 0.0,
        "within_sleeve": bool(qty > 0 and price * qty <= _f(capital, SCALP_SLEEVE_CAPITAL)),
    }


def select_scalp_engine(candidates: Iterable[Any]) -> str:
    """Return the highest-conviction Sleeve-A engine (Parity > Lead-Lag > Ping-Pong)."""
    ranked: list[tuple[int, str]] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            raw = candidate.get("engine") or candidate.get("name") or candidate.get("key")
            priority = candidate.get("priority")
        else:
            raw, priority = candidate, None
        name = engine_name(raw)
        if name not in SCALP_PRIORITY:
            continue
        ranked.append((int(priority or SCALP_PRIORITY[name]), name))
    return min(ranked, key=lambda row: row[0])[1] if ranked else ""


def house_money_window(now: datetime | str | float | None = None, *, expiry_day: bool = False) -> bool:
    if not expiry_day:
        return False
    moment = _as_ist(now)
    return moment.weekday() < 5 and HOUSE_MONEY_START <= moment.time() <= HOUSE_MONEY_END


def route_dual_index(nifty_score: Any, banknifty_score: Any) -> dict[str, Any]:
    """Send Sleeve A to the index with the higher 0–100 velocity score."""
    nifty, bank = _f(nifty_score), _f(banknifty_score)
    winner = "BANKNIFTY" if bank > nifty else "NIFTY"
    return {"symbol": winner, "nifty_score": round(nifty, 2),
            "banknifty_score": round(bank, 2), "reason": "HIGHER_OPPORTUNITY_VELOCITY"}


def continuous_scalp_setup(
    *, main_sniper_waiting: bool, atr_1m: Any, range_low: Any, range_high: Any,
) -> dict[str, Any]:
    """Velocity / 45–80 pt ping-pong setup; pause below a 25 pt dead zone."""
    width = max(0.0, _f(range_high) - _f(range_low))
    velocity = _f(atr_1m) >= 6.0
    ping_pong = 45.0 <= width <= 80.0
    dead_zone = width < 25.0
    allowed = bool(main_sniper_waiting and not dead_zone and (velocity or ping_pong))
    reason = "DEAD_ZONE_BROKERAGE_SAVER" if dead_zone else (
        "VELOCITY_BURST" if velocity else "PING_PONG_BOX" if ping_pong else
        "NO_EDGE" if main_sniper_waiting else "MAIN_SNIPER_ACTIVE"
    )
    return {"allowed": allowed, "reason": reason, "atr_1m": _f(atr_1m),
            "box_width": round(width, 2), "dead_zone": dead_zone,
            "velocity": velocity, "ping_pong": ping_pong}


def house_money_exit(*, obi_5: Any, multiple: Any) -> dict[str, Any]:
    """2x double-up lock plus millisecond peak-exhaustion exit."""
    book, gain = _f(obi_5), _f(multiple)
    peak_exhaustion = book < 0.68
    lock_2x = gain >= 2.0
    return {"exit": bool(peak_exhaustion), "lock_2x": bool(lock_2x),
            "peak_exhaustion": bool(peak_exhaustion),
            "reason": "PEAK_EXHAUSTION_OBI_BELOW_0.68" if peak_exhaustion else
                      "DOUBLE_UP_LOCKED" if lock_2x else "HOLD"}


def house_money_entry(
    *, ltp: Any, hero_score: Any, expiry_day: bool, now: datetime | str | float | None = None,
) -> dict[str, Any]:
    """Validate the ₹8–₹32, HeroScore >= 90 Zero-to-Hero strike."""
    price = _f(ltp)
    score = _f(hero_score)
    in_window = house_money_window(now, expiry_day=expiry_day)
    ok = bool(in_window and 8.0 <= price <= 32.0 and score >= 90.0)
    reason = "HOUSE_MONEY_READY" if ok else (
        "OUTSIDE_EXPIRY_WINDOW" if not in_window else
        "LTP_OUTSIDE_8_32" if not 8.0 <= price <= 32.0 else "HEROSCORE_BELOW_90"
    )
    return {"ok": ok, "reason": reason, "ltp": price, "hero_score": score,
            "double_up_lock": ok, "exhaustion_exit_obi": 0.68}


def gear5_runner_decision(
    *, side: str, smc_bos: bool, obi_5: Any, futures_volume_ratio: Any,
    tbq: Any = 0.0, tsq: Any = 0.0, option_ltp: Any = 0.0,
    ema9: Any = 0.0, atp: Any = 0.0, oi_change_5m_pct: Any = 0.0,
    smc_bear_choch: bool = False,
) -> dict[str, Any]:
    """Apply the post-T2 Gate-5 rule: bank 70%, retain a 30% ratchet runner."""
    decision = evaluate_post_t2_continuation(
        smc_bull_bos=bool(smc_bos),
        smc_bear_choch=bool(smc_bear_choch),
        obi_5=_f(obi_5), tbq=_f(tbq), tsq=_f(tsq),
        futures_volume_ratio=_f(futures_volume_ratio),
        option_ltp=_f(option_ltp), ema9=_f(ema9), atp=_f(atp),
        oi_change_5m_pct=_f(oi_change_5m_pct),
    )
    decision.update({
        "side": str(side or "").upper(),
        "bank_pct": 70 if decision["runner_allowed"] else 100,
        "runner_pct": 30 if decision["runner_allowed"] else 0,
        "ratchet": "1m_CANDLE_TRAIL" if decision["runner_allowed"] else "BANK_AT_T2",
    })
    return decision


@dataclass
class SleeveAccount:
    key: str
    capital: float
    lock: threading.RLock = field(default_factory=threading.RLock)
    active: dict[str, Any] | None = None
    realized: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def deployed(self) -> float:
        return round(_f((self.active or {}).get("capital_required")), 2)

    @property
    def available(self) -> float:
        return round(max(0.0, self.capital - self.deployed), 2)

    @property
    def busy(self) -> bool:
        return self.active is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sleeve": self.key,
            "capital": round(self.capital, 2),
            "deployed": self.deployed,
            "available": self.available,
            "busy": self.busy,
            "active": dict(self.active) if self.active else None,
            "realized": round(self.realized, 2),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
        }


class CapitalSleeveRouter:
    """Two independent mutexes, one ₹1,00,000 account, no capital sharing."""

    def __init__(self, total_capital: Any = TOTAL_CAPITAL) -> None:
        total = _f(total_capital, TOTAL_CAPITAL)
        if total < TOTAL_CAPITAL:
            raise ValueError("dual-sleeve account requires at least ₹1,00,000 base capital")
        scale = total / TOTAL_CAPITAL
        self.total_capital = round(total, 2)
        self.accounts = {
            SLEEVE_A: SleeveAccount(SLEEVE_A, round(SCALP_SLEEVE_CAPITAL * scale, 2)),
            SLEEVE_B: SleeveAccount(SLEEVE_B, round(SNIPER_SLEEVE_CAPITAL * scale, 2)),
        }
        self.vault = 0.0
        self.house_money_pool = 0.0
        self._vault_lock = threading.RLock()

    # -- capacity / mutex -------------------------------------------------- #
    def account(self, sleeve: str) -> SleeveAccount:
        key = str(sleeve or "").upper()
        if key not in self.accounts:
            raise KeyError(f"unknown sleeve {sleeve!r}")
        return self.accounts[key]

    def available(self, sleeve: str) -> float:
        return self.account(sleeve).available

    def busy(self, sleeve: str) -> bool:
        return self.account(sleeve).busy

    def try_acquire(
        self, sleeve: str, engine: str, capital_required: Any, *,
        side: str = "", **metadata: Any,
    ) -> dict[str, Any]:
        """Atomically lock one sleeve; an overlapping request is rejected."""
        key = str(sleeve or "").upper()
        account = self.account(key)
        name = engine_name(engine)
        allowed = SCALP_PRIORITY if key == SLEEVE_A else SNIPER_PRIORITY
        required = _f(capital_required)
        with account.lock:
            if name not in allowed:
                return {"ok": False, "reason": "ENGINE_NOT_ROUTED_TO_SLEEVE",
                        "sleeve": key, "engine": name, "mutex_locked": account.busy}
            if account.active is not None:
                return {"ok": False, "reason": "SLEEVE_MUTEX_BUSY",
                        "sleeve": key, "engine": account.active.get("engine"),
                        "mutex_locked": True, "active": dict(account.active)}
            if required <= 0:
                return {"ok": False, "reason": "INVALID_CAPITAL", "sleeve": key,
                        "engine": name, "mutex_locked": False}
            if required > account.available:
                return {"ok": False, "reason": "SLEEVE_CAPACITY_EXCEEDED",
                        "sleeve": key, "engine": name, "available": account.available,
                        "required": round(required, 2), "mutex_locked": False}
            account.active = {
                "engine": name, "priority": allowed[name],
                "capital_required": round(required, 2), "side": str(side or "").upper(),
                **metadata,
            }
            return {"ok": True, "reason": "SLEEVE_LOCKED", "sleeve": key,
                    "engine": name, "capital_required": round(required, 2),
                    "available": account.available, "mutex_locked": True,
                    "active": dict(account.active)}

    def release(self, sleeve: str, pnl: Any = 0.0) -> dict[str, Any]:
        """Close the locked position and sweep 50% of a profit into the vault."""
        account = self.account(sleeve)
        amount = round(_f(pnl), 2)
        with account.lock:
            closed = dict(account.active or {})
            if not closed:
                return {"ok": False, "reason": "NO_ACTIVE_POSITION", "sleeve": account.key}
            account.active = None
            account.realized = round(account.realized + amount, 2)
            account.trades += 1
            vault_add = round(amount * VAULT_LOCK_PCT, 2) if amount > 0 else 0.0
            pool_add = round(amount - vault_add, 2) if amount > 0 else 0.0
            with self._vault_lock:
                self.vault = round(self.vault + vault_add, 2)
                self.house_money_pool = round(self.house_money_pool + pool_add, 2)
            if amount > 0:
                account.wins += 1
            elif amount < 0:
                account.losses += 1
            record = {
                **closed, "sleeve": account.key, "pnl": amount,
                "vault_add": vault_add, "house_money_add": pool_add,
            }
            account.history.append(record)
            return {"ok": True, "reason": "SLEEVE_RELEASED", "sleeve": account.key,
                    "pnl": amount, "vault_add": vault_add,
                    "house_money_add": pool_add, "closed": record}

    # -- convenience routers ---------------------------------------------- #
    def try_scalp(self, engine: str, capital_required: Any, *, side: str = "", **metadata: Any) -> dict[str, Any]:
        return self.try_acquire(SLEEVE_A, engine, capital_required, side=side, **metadata)

    def try_sniper(self, engine: str, capital_required: Any, *, side: str = "", **metadata: Any) -> dict[str, Any]:
        return self.try_acquire(SLEEVE_B, engine, capital_required, side=side, **metadata)

    def open_scalp(self, engine: str, capital_required: Any, **kwargs: Any) -> dict[str, Any]:
        return self.try_scalp(engine, capital_required, **kwargs)

    def open_sniper(self, engine: str, capital_required: Any, **kwargs: Any) -> dict[str, Any]:
        return self.try_sniper(engine, capital_required, **kwargs)

    def close(self, sleeve: str, pnl: Any = 0.0) -> dict[str, Any]:
        return self.release(sleeve, pnl)

    def best_scalp_candidate(self, candidates: Iterable[Any]) -> str:
        return select_scalp_engine(candidates)

    # -- telemetry --------------------------------------------------------- #
    def snapshot(self) -> dict[str, Any]:
        a, b = self.accounts[SLEEVE_A], self.accounts[SLEEVE_B]
        return {
            "ok": True,
            "total_capital": self.total_capital,
            "allocated": round(a.capital + b.capital, 2),
            "unallocated": round(self.total_capital - a.capital - b.capital, 2),
            "sleeve_a": a.as_dict(),
            "sleeve_b": b.as_dict(),
            "vault": round(self.vault, 2),
            "house_money_pool": round(self.house_money_pool, 2),
            "combined_realized": round(a.realized + b.realized, 2),
            "hud": {
                "scalp_capital_allocated": a.capital,
                "scalp_capital_deployed": a.deployed,
                "scalp_realized_pnl_today": a.realized,
                "scalp_total_pnl_today": a.realized,
                "scalp_roi_pct_today": round(a.realized / a.capital * 100.0, 2) if a.capital else 0.0,
                "main_capital_allocated": b.capital,
                "main_realized_pnl_today": b.realized,
                "combined_intraday_net_pnl": round(a.realized + b.realized, 2),
                "profit_vault": round(self.vault, 2),
            },
        }


__all__ = [
    "TOTAL_CAPITAL", "SCALP_SLEEVE_CAPITAL", "SNIPER_SLEEVE_CAPITAL",
    "SLEEVE_A", "SLEEVE_B", "SLEEVE_C", "SCALP_PRIORITY", "SNIPER_PRIORITY",
    "CapitalSleeveRouter", "SleeveAccount", "calculate_scalp_qty",
    "clamp_scalp_stop", "scalp_partial_plan", "select_scalp_engine",
    "house_money_window", "house_money_entry", "house_money_exit",
    "gear5_runner_decision", "continuous_scalp_setup", "route_dual_index",
]
