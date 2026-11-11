"""Isolated ₹35,000 continuous-scalp accounting sleeve.

The main sniper ledger and the older house-money book are intentionally not
mixed into this object.  A scalp entry, every partial leg, and the final exit
all stay inside ``ScalpSleeveLedger`` so the UI can show *live* sleeve P&L
without touching capital that backs a macro trade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from strategy.continuous_scalp_engine import MAX_SCALP_CAPITAL, SCALP_SLEEVE_CAPITAL

IST = timezone(timedelta(hours=5, minutes=30))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_ist(moment: datetime | str | None) -> datetime:
    if moment is None:
        return datetime.now(tz=IST)
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


@dataclass
class ScalpSleeveLedger:
    date: str = ""
    capital_allocated: float = SCALP_SLEEVE_CAPITAL
    realized_pnl_today: float = 0.0
    total_scalps: int = 0
    ce_scalps: int = 0
    pe_scalps: int = 0
    ce_wins: int = 0
    pe_wins: int = 0
    ce_losses: int = 0
    pe_losses: int = 0
    wins: int = 0
    losses: int = 0
    active: dict[str, Any] | None = None
    last_closed: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    engine_state: str = "WAITING"

    def ensure_day(self, today: str) -> "ScalpSleeveLedger":
        if today and self.date != today:
            self.date = today
            self.realized_pnl_today = 0.0
            self.total_scalps = 0
            self.ce_scalps = 0
            self.pe_scalps = 0
            self.ce_wins = 0
            self.pe_wins = 0
            self.ce_losses = 0
            self.pe_losses = 0
            self.wins = 0
            self.losses = 0
            self.active = None
            self.last_closed = {}
            self.history = []
        return self

    @property
    def allocated(self) -> float:
        return round(min(_f(self.capital_allocated, SCALP_SLEEVE_CAPITAL), MAX_SCALP_CAPITAL), 2)

    @property
    def deployed(self) -> float:
        if not self.active:
            return 0.0
        return round(_f(self.active.get("entry")) * int(self.active.get("qty_open") or 0), 2)

    @property
    def live_open_pnl(self) -> float:
        """Mark-to-market P&L of the active scalp including booked partials."""
        if not self.active:
            return 0.0
        entry = _f(self.active.get("entry"))
        ltp = _f(self.active.get("ltp"), entry)
        qty_open = int(self.active.get("qty_open") or 0)
        realized_partial = _f(self.active.get("realized_pnl_rs"))
        return round(realized_partial + (ltp - entry) * qty_open, 2)

    def start_trade(self, contract: dict[str, Any], *, now: datetime | str | None = None) -> dict[str, Any]:
        if self.active:
            return {"ok": False, "reason": "SCALP_SLEEVE_BUSY"}
        entry = _f(contract.get("entry") or contract.get("ltp"))
        qty = int(contract.get("qty") or 0)
        if entry <= 0 or qty <= 0:
            return {"ok": False, "reason": "INVALID_CONTRACT"}
        deployed = round(entry * qty, 2)
        if deployed > MAX_SCALP_CAPITAL:
            return {"ok": False, "reason": "SCALP_CAPITAL_CAP_EXCEEDED"}
        self.active = {
            "symbol": str(contract.get("symbol") or "NIFTY").upper(),
            "side": str(contract.get("side") or "").upper(),
            "strike": _f(contract.get("strike")),
            "option_symbol": str(contract.get("option_symbol") or ""),
            "entry": round(entry, 2),
            "ltp": round(entry, 2),
            "qty": qty,
            "qty_initial": qty,
            "qty_open": qty,
            "deployed": deployed,
            "realized_pnl_rs": 0.0,
            "opened_at": _as_ist(now).isoformat(timespec="seconds"),
            "stop": _f(contract.get("stop")),
            "target_1": _f(contract.get("target_1")),
            "target_2": _f(contract.get("target_2")),
        }
        self.engine_state = "AUTO_SCALPING"
        return {"ok": True, "active": dict(self.active)}

    def mark(self, ltp: Any) -> dict[str, Any]:
        if not self.active:
            return {"ok": False, "reason": "NO_ACTIVE_SCALP"}
        price = _f(ltp)
        if price <= 0:
            return {"ok": False, "reason": "INVALID_LTP"}
        self.active["ltp"] = round(price, 2)
        self.active["live_pnl_rs"] = self.live_open_pnl
        return {"ok": True, "live_open_pnl": self.live_open_pnl}

    def sync_trade(self, trade: dict[str, Any], *, ltp: Any = None) -> None:
        """Mirror partials/trailing state from the mutable scalp trade."""
        if not self.active or not isinstance(trade, dict):
            return
        if ltp is not None and _f(ltp) > 0:
            self.active["ltp"] = round(_f(ltp), 2)
        self.active["qty_open"] = int(trade.get("qty_open") or 0)
        self.active["realized_pnl_rs"] = round(_f(trade.get("realized_pnl_rs")), 2)
        self.active["stop"] = round(_f(trade.get("current_trailing_sl")), 2)
        self.active["trade_status"] = str(trade.get("status") or "")
        self.active["booked_legs"] = list(trade.get("booked_legs") or [])
        self.active["live_pnl_rs"] = self.live_open_pnl

    def finish_trade(self, trade: dict[str, Any], *, now: datetime | str | None = None) -> dict[str, Any] | None:
        if not self.active:
            return None
        self.sync_trade(trade)
        pnl = round(_f(trade.get("realized_pnl_rs")), 2)
        side = str(self.active.get("side") or "")
        record = {
            "symbol": self.active.get("symbol"),
            "side": side,
            "strike": self.active.get("strike"),
            "entry": self.active.get("entry"),
            "exit": _f(self.active.get("ltp")),
            "qty": self.active.get("qty"),
            "pnl": pnl,
            "opened_at": self.active.get("opened_at"),
            "closed_at": _as_ist(now).isoformat(timespec="seconds"),
            "booked_legs": self.active.get("booked_legs") or [],
        }
        self.realized_pnl_today = round(self.realized_pnl_today + pnl, 2)
        self.total_scalps += 1
        if side == "CE":
            self.ce_scalps += 1
            if pnl > 0:
                self.ce_wins += 1
            elif pnl < 0:
                self.ce_losses += 1
        elif side == "PE":
            self.pe_scalps += 1
            if pnl > 0:
                self.pe_wins += 1
            elif pnl < 0:
                self.pe_losses += 1
        if pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1
        self.history.append(record)
        self.last_closed = record
        self.active = None
        self.engine_state = "WAITING"
        return record

    def close(self, ltp: Any, *, reason: str = "MANUAL_CLOSE",
              now: datetime | str | None = None) -> dict[str, Any] | None:
        """Close the sleeve when no mutable trade object is available."""
        if not self.active:
            return None
        price = _f(ltp)
        entry = _f(self.active.get("entry"))
        qty = int(self.active.get("qty_open") or 0)
        pnl = round(_f(self.active.get("realized_pnl_rs")) + (price - entry) * qty, 2)
        trade = {
            "status": "CLOSED",
            "qty_open": 0,
            "realized_pnl_rs": pnl,
            "current_trailing_sl": self.active.get("stop"),
            "booked_legs": list(self.active.get("booked_legs") or []) + [{
                "leg": reason, "qty": qty, "price": round(price, 2), "pnl": pnl,
            }],
            "exit_reason": reason,
        }
        return self.finish_trade(trade, now=now)

    def active_strike_text(self) -> str:
        if not self.active:
            return ""
        return f"{self.active.get('symbol') or ''} {int(_f(self.active.get('strike')))} {self.active.get('side') or ''}".strip()

    def stats(self) -> dict[str, Any]:
        finished = self.wins + self.losses
        rate = (self.wins / finished * 100.0) if finished else 0.0
        return {
            "total_scalps": self.total_scalps,
            "ce_scalps": self.ce_scalps,
            "pe_scalps": self.pe_scalps,
            "ce_wins": self.ce_wins,
            "pe_wins": self.pe_wins,
            "ce_losses": self.ce_losses,
            "pe_losses": self.pe_losses,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(rate, 2),
            "active_strike": self.active_strike_text(),
            "engine_state": self.engine_state,
        }

    def snapshot(
        self,
        ltp: Any = None,
        *,
        main_sniper_total_pnl_today: Any = 0.0,
        engine_state: str | None = None,
    ) -> dict[str, Any]:
        if ltp is not None and self.active:
            self.mark(ltp)
        if engine_state:
            self.engine_state = str(engine_state)
        live = self.live_open_pnl
        total = round(self.realized_pnl_today + live, 2)
        allocated = self.allocated
        roi = round((total / allocated) * 100.0, 2) if allocated > 0 else 0.0
        main_pnl = round(_f(main_sniper_total_pnl_today), 2)
        stats = self.stats()
        return {
            "scalp_capital_allocated": allocated,
            "scalp_capital_deployed": self.deployed,
            "scalp_live_open_pnl": live,
            "scalp_realized_pnl_today": round(self.realized_pnl_today, 2),
            "scalp_total_pnl_today": total,
            "scalp_roi_pct_today": roi,
            "scalp_stats": stats,
            "scalp_active": dict(self.active or {}),
            "scalp_last_closed": dict(self.last_closed or {}),
            "combined_intraday_net_pnl": round(main_pnl + total, 2),
            "main_sniper_total_pnl_today": main_pnl,
            "scalp_engine_state": self.engine_state,
            "scalp_live_ticker": self._ticker_text(),
        }

    def _ticker_text(self) -> str:
        if not self.active:
            return "WAITING FOR 5/5 SCALP IGNITION"
        entry = _f(self.active.get("entry"))
        ltp = _f(self.active.get("ltp"), entry)
        qty = int(self.active.get("qty_open") or 0)
        pnl = self.live_open_pnl
        pct = (pnl / (entry * int(self.active.get("qty") or 0)) * 100.0
               if entry and int(self.active.get("qty") or 0) else 0.0)
        return (
            f"{self.active.get('symbol')} {int(_f(self.active.get('strike')))} "
            f"{self.active.get('side')} ({qty} Qty @ ₹{entry:.2f} → ₹{ltp:.2f}) "
            f"{'+' if pnl >= 0 else '-'}₹{abs(pnl):,.0f} ({pct:+.1f}%)"
        )


__all__ = ["ScalpSleeveLedger"]
