"""Persistent 1-week paper-trading ledger.

Starting capital ``Rs 1,00,000``. Every trade is stored with its strike, points
captured, gross, costs and vault sweep, and the ledger aggregates a Day 1-5
summary table plus the running account balance.

    from state.paper_trade_ledger import PaperLedger
    ledger = PaperLedger.load()
    ledger.record(...)
    print(ledger.week_table())
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_FILE = REPO_ROOT / "state" / "paper_trade_ledger.json"
START_CAPITAL = 100_000.0

LIGHT_ICON = {"RED": "\U0001F534", "YELLOW": "\U0001F7E1", "GREEN": "\U0001F7E2"}
LIGHT_LABEL = {"RED": "RED 0-Trade", "YELLOW": "YELLOW Sniper", "GREEN": "GREEN Super-Trend"}


@dataclass
class PaperTrade:
    date: str                     # YYYY-MM-DD
    light: str                    # RED | YELLOW | GREEN
    symbol: str                   # NIFTY / BANKNIFTY
    strike: str                   # NIFTY 24950 CE
    option_type: str              # CE | PE
    qty: int
    entry: float
    exit: float
    points: float
    gross: float
    costs: float
    net: float
    vaulted: float = 0.0
    recovery: bool = False
    outcome: str = ""             # WIN | SL | BREAKEVEN
    entry_at: str = ""
    exit_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperLedger:
    capital: float = START_CAPITAL
    vault: float = 0.0
    trades: list[PaperTrade] = field(default_factory=list)
    days: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path = LEDGER_FILE) -> "PaperLedger":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return cls()
        return cls(
            capital=float(raw.get("capital", START_CAPITAL)),
            vault=float(raw.get("vault", 0.0)),
            trades=[PaperTrade(**item) for item in raw.get("trades", [])],
            days=raw.get("days", {}),
        )

    def save(self, path: Path = LEDGER_FILE) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "capital": round(self.capital, 2),
            "vault": round(self.vault, 2),
            "trades": [trade.as_dict() for trade in self.trades],
            "days": self.days,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def reset(self, path: Path = LEDGER_FILE) -> "PaperLedger":
        self.capital = START_CAPITAL
        self.vault = 0.0
        self.trades.clear()
        self.days.clear()
        self.save(path)
        return self

    # -- recording ---------------------------------------------------------
    def register_day(self, date: str, light: str, note: str = "") -> None:
        self.days.setdefault(date, {"light": light, "note": note, "trades": []})

    def record(self, trade: PaperTrade) -> PaperTrade:
        self.trades.append(trade)
        day = self.days.setdefault(trade.date, {"light": trade.light, "note": "", "trades": []})
        day["light"] = trade.light or day.get("light", "RED")
        day.setdefault("trades", []).append(trade.as_dict())
        self.vault = round(self.vault + max(trade.vaulted, 0.0), 2)
        self.capital = round(self.capital + trade.net, 2)
        return trade

    # -- reporting ---------------------------------------------------------
    def day_summary(self, date: str) -> dict[str, Any]:
        trades = [t for t in self.trades if t.date == date]
        light = (self.days.get(date) or {}).get("light") or (trades[0].light if trades else "RED")
        wins = sum(1 for t in trades if t.net > 0)
        losses = sum(1 for t in trades if t.net < 0)
        breakeven = sum(1 for t in trades if abs(t.net) < 1e-9)
        gross = sum(t.gross for t in trades)
        costs = sum(t.costs for t in trades)
        net = sum(t.net for t in trades)
        vaulted = sum(t.vaulted for t in trades)
        points = sum(t.points for t in trades)
        recoveries = sum(1 for t in trades if t.recovery)
        return {
            "date": date,
            "light": light,
            "trades": len(trades),
            "wins": wins,
            "breakevens": breakeven,
            "sls": losses,
            "recoveries": recoveries,
            "strikes": [t.strike for t in trades],
            "points": round(points, 2),
            "gross": round(gross, 2),
            "costs": round(costs, 2),
            "net": round(net, 2),
            "vault": round(vaulted, 2),
        }

    def week_table(self) -> list[dict[str, Any]]:
        dates = sorted({t.date for t in self.trades} | set(self.days))
        rows: list[dict[str, Any]] = []
        balance = START_CAPITAL
        for index, date in enumerate(dates[:5], start=1):
            row = self.day_summary(date)
            balance = round(balance + row["net"], 2)
            row["day"] = index
            row["balance"] = balance
            rows.append(row)
        return rows

    def snapshot(self) -> dict[str, Any]:
        rows = self.week_table()
        return {
            "start_capital": START_CAPITAL,
            "capital": round(self.capital, 2),
            "vault": round(self.vault, 2),
            "trades": len(self.trades),
            "net": round(sum(t.net for t in self.trades), 2),
            "rows": rows,
        }

    # -- rendering ---------------------------------------------------------
    def render_table(self) -> str:
        rows = self.week_table()
        if not rows:
            return "No paper trades recorded yet."
        header = (
            f"{'Day':<4}{'Date':<12}{'Regime':<24}{'Trd':>4}{'W/BE/SL':>9}"
            f"{'Pts':>8}{'Gross':>11}{'Costs':>9}{'Net':>11}{'Vault':>10}{'Balance':>12}"
        )
        lines = [header, "-" * len(header)]
        for row in rows:
            icon = LIGHT_ICON.get(row["light"], "")
            regime = f"{icon} {LIGHT_LABEL.get(row['light'], row['light'])}"
            lines.append(
                f"{row['day']:<4}{row['date']:<12}{regime:<24}{row['trades']:>4} "
                f"{row['wins']}/{row['breakevens']}/{row['sls']:<4}"
                f"{row['points']:>8.1f}{row['gross']:>11,.0f}{row['costs']:>9,.0f}"
                f"{row['net']:>11,.0f}{row['vault']:>10,.0f}{row['balance']:>12,.0f}"
            )
        total_net = sum(row["net"] for row in rows)
        lines.append("-" * len(header))
        lines.append(
            f"{'':<4}{'TOTAL':<12}{'':<24}{sum(r['trades'] for r in rows):>4}"
            f"{'':>9}{sum(r['points'] for r in rows):>8.1f}"
            f"{sum(r['gross'] for r in rows):>11,.0f}{sum(r['costs'] for r in rows):>9,.0f}"
            f"{total_net:>11,.0f}{sum(r['vault'] for r in rows):>10,.0f}"
            f"{rows[-1]['balance']:>12,.0f}"
        )
        return "\n".join(lines)
