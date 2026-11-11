"""₹35,000 scalp sleeve ledger with the canonical 50% vault rule.

This is the audit-facing record behind the headline sleeve total.  It stays
separate from the session's mutex/active state: the execution layer owns the
position, this ledger owns the banked rupees and the exact per-trade evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from state.eod_pnl_reconciler import normalise_row

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SCALP_SLEEVE_RS = 35_000.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ScalpSleeveLedger:
    date: str = ""
    allocated_inr: float = SCALP_SLEEVE_RS
    rows: list[dict[str, Any]] = field(default_factory=list)

    def ensure_day(self, date: str) -> "ScalpSleeveLedger":
        if date and self.date != date:
            self.date = date
            self.rows = []
        return self

    def record(self, trade: Any) -> dict[str, Any]:
        row = normalise_row(trade)
        if row["sleeve"] != "scalp_35k":
            row["sleeve"] = "scalp_35k"
        trade_id = row.get("trade_id")
        if trade_id:
            for index, old in enumerate(self.rows):
                if old.get("trade_id") == trade_id:
                    self.rows[index] = row
                    return row
        self.rows = [old for old in self.rows if old.get("trade_id") != trade_id]
        self.rows.append(row)
        return row

    @classmethod
    def from_trades(cls, trades: Iterable[Any], *, date: str = "") -> "ScalpSleeveLedger":
        book = cls(date=date)
        for trade in trades:
            row = normalise_row(trade)
            if row["sleeve"] == "scalp_35k":
                book.rows.append(row)
        return book

    @property
    def realized_pnl(self) -> float:
        return round(sum(_f(row.get("net_pnl")) for row in self.rows), 2)

    @property
    def locked_vault(self) -> float:
        return round(sum(max(0.0, _f(row.get("net_pnl"))) * 0.50 for row in self.rows), 2)

    def stats(self) -> dict[str, Any]:
        closed = [row for row in self.rows if str(row.get("status")) == "CLOSED"]
        wins = [row for row in closed if _f(row.get("net_pnl")) > 0]
        losses = [row for row in closed if _f(row.get("net_pnl")) < 0]
        return {
            "sleeve": "SCALP_35K",
            "capital_allocated": self.allocated_inr,
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
            "realized_pnl": self.realized_pnl,
            "vault_locked": self.locked_vault,
        }

    def snapshot(self, *, open_mtm: Any = 0.0) -> dict[str, Any]:
        mtm = round(_f(open_mtm), 2)
        return {
            "date": self.date or datetime.now(tz=IST).date().isoformat(),
            "scalp_capital_allocated": self.allocated_inr,
            "scalp_realized_pnl_today": self.realized_pnl,
            "scalp_live_open_pnl": mtm,
            "scalp_total_pnl_today": round(self.realized_pnl + mtm, 2),
            "vault_locked_inr": self.locked_vault,
            "scalp_stats": self.stats(),
            "rows": list(self.rows),
        }


__all__ = ["SCALP_SLEEVE_RS", "ScalpSleeveLedger"]
