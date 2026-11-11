"""Persistent paper-trading ledger for the KRONOS terminal.

The original week ledger stored one flat row per trade.  The Pro Trade Ledger
adds the execution detail the HUD and calendar need:

* exact T1 / T2 / trailing-stop sub-bookings (qty, fill, rupees)
* maximum favourable / adverse excursion for the stop cell
* identified setups that never triggered (traffic-light cancellations, strike
  rolls, market reversals)
* a calendar-ready day summary and a per-day payload for the modal

Everything is persisted as JSON.  ``publish_firebase`` is an optional mirror
used by the runner so the static hosted terminal can read the same ledger.

    from state.paper_trade_ledger import PaperLedger, PaperTrade
    ledger = PaperLedger.load()
    ledger.record(...)
    print(ledger.week_table())
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_FILE = REPO_ROOT / "state" / "paper_trade_ledger.json"
START_CAPITAL = 100_000.0
IST = timezone(timedelta(hours=5, minutes=30))

LIGHT_ICON = {"RED": "\U0001F534", "YELLOW": "\U0001F7E1", "GREEN": "\U0001F7E2"}
LIGHT_LABEL = {"RED": "RED 0-Trade", "YELLOW": "YELLOW Sniper", "GREEN": "GREEN Super-Trend"}


def today_ist() -> str:
    """The ledger's trading date, not the host machine's local date."""
    return datetime.now(tz=IST).date().isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        return text.split("T", 1)[1][:5]
    if " " in text:
        return text.rsplit(" ", 1)[-1][:5]
    return text[:5]


def _fallback_id(date: str, symbol: str, strike: str, entry_at: str) -> str:
    return f"{date}|{symbol}|{strike}|{entry_at}"


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

    # -- Pro Trade Ledger detail -------------------------------------------
    trade_id: str = ""
    status: str = "CLOSED"        # OPEN | CLOSED
    initial_qty: int = 0
    qty_open: int = 0
    t1_qty: int = 0
    t1_price: float = 0.0
    t1_pnl: float = 0.0
    t1_at: str = ""
    t2_qty: int = 0
    t2_price: float = 0.0
    t2_pnl: float = 0.0
    t2_at: str = ""
    sl_price: float = 0.0
    sl_pnl: float = 0.0
    sl_at: str = ""
    sl_reason: str = ""
    mfe_price: float = 0.0
    mae_price: float = 0.0
    booked_legs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.status = str(self.status or "CLOSED").upper()
        self.qty = int(_num(self.qty))
        self.initial_qty = int(_num(self.initial_qty, self.qty))
        if not self.booked_legs:
            self.booked_legs = []
        if self.status == "CLOSED":
            self.qty_open = 0
        elif not self.qty_open:
            self.qty_open = max(self.initial_qty - self.t1_qty - self.t2_qty, 0)
        # Old records predate the split fields; a booked leg is still authoritative.
        for leg in self.booked_legs:
            name = str((leg or {}).get("leg") or "").upper()
            if (name == "T1" or name.startswith("SUPER_T1")) and not self.t1_qty:
                self.t1_qty = int(_num(leg.get("qty")))
                self.t1_price = _num(leg.get("price"))
                self.t1_pnl = _num(leg.get("pnl"))
                self.t1_at = str(leg.get("at") or "")
            elif (name == "T2" or name.startswith("SUPER_T2")) and not self.t2_qty:
                self.t2_qty = int(_num(leg.get("qty")))
                self.t2_price = _num(leg.get("price"))
                self.t2_pnl = _num(leg.get("pnl"))
                self.t2_at = str(leg.get("at") or "")
            elif name in {"SL", "TSL", "TRAIL", "STOP"} and not self.sl_price:
                self.sl_price = _num(leg.get("price"))
                self.sl_pnl = _num(leg.get("pnl"))
                self.sl_at = str(leg.get("at") or "")
                self.sl_reason = str(leg.get("leg") or "SL")
        if not self.trade_id:
            self.trade_id = _fallback_id(self.date, self.symbol, self.strike, self.entry_at)

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "PaperTrade":
        names = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in (item or {}).items() if key in names})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def paper_trade_from_live(
    trade: dict[str, Any],
    *,
    date: str,
    symbol: str,
    light: str = "GREEN",
) -> PaperTrade:
    """Map the live SSE trade dict (``booked_legs``) onto a ledger row.

    Open trades keep only *realised* rupees in ``net`` so the scorecard never
    books an unrealised mark.  The browser adds ``live_pnl_rs`` for the row and
    footer while the trade is open.
    """
    legs = list(trade.get("booked_legs") or [])
    initial_qty = int(_num(trade.get("initial_qty"), _num(trade.get("qty"))))
    status = str(trade.get("status") or "OPEN").upper()
    if status not in {"OPEN", "CLOSED"}:
        status = "OPEN"

    def leg(name: str) -> dict[str, Any]:
        return next(
            (row for row in legs if str((row or {}).get("leg") or "").upper() == name),
            {},
        )

    def leg_prefix(prefix: str) -> dict[str, Any]:
        return next(
            (row for row in legs
             if str((row or {}).get("leg") or "").upper().startswith(prefix)),
            {},
        )

    t1 = leg("T1") or leg_prefix("SUPER_T1")
    t2 = leg("T2") or leg_prefix("SUPER_T2")
    sl = next(
        (row for row in legs
         if str((row or {}).get("leg") or "").upper() in {"SL", "TSL", "TRAIL", "STOP"}),
        {},
    )
    t1_qty = int(_num(t1.get("qty")))
    t2_qty = int(_num(t2.get("qty")))
    qty_open = int(_num(trade.get("qty_open"), max(initial_qty - t1_qty - t2_qty, 0)))
    if status == "CLOSED":
        qty_open = 0
    realized = _num(trade.get("realized_pnl_rs"))
    entry = _num(trade.get("entry_price"))
    exit_price = _num(trade.get("exit_price"))
    net = realized if status == "CLOSED" else realized
    outcome = "OPEN" if status == "OPEN" else (
        "WIN" if net > 0 else "SL" if net < 0 else "BREAKEVEN"
    )
    strike = str(trade.get("option_symbol") or trade.get("strike_symbol") or trade.get("strike") or "")
    option_type = str(trade.get("option_type") or (strike.rsplit(" ", 1)[-1] if " " in strike else ""))
    return PaperTrade(
        date=date,
        light=light or "GREEN",
        symbol=symbol,
        strike=strike,
        option_type=option_type,
        qty=initial_qty,
        entry=round(entry, 2),
        exit=round(exit_price, 2) if status == "CLOSED" else 0.0,
        points=round(exit_price - entry, 2) if status == "CLOSED" else 0.0,
        gross=round(realized, 2),
        costs=0.0,
        net=round(net, 2),
        vaulted=round(max(net, 0.0) * 0.70, 2) if status == "CLOSED" else 0.0,
        outcome=outcome,
        entry_at=str(trade.get("entry_at") or ""),
        exit_at=str(trade.get("exit_at") or "") if status == "CLOSED" else "",
        trade_id=str(trade.get("trade_id") or ""),
        status=status,
        initial_qty=initial_qty,
        qty_open=max(qty_open, 0),
        t1_qty=t1_qty,
        t1_price=_num(t1.get("price")),
        t1_pnl=_num(t1.get("pnl")),
        t1_at=str(t1.get("at") or ""),
        t2_qty=t2_qty,
        t2_price=_num(t2.get("price")),
        t2_pnl=_num(t2.get("pnl")),
        t2_at=str(t2.get("at") or ""),
        sl_price=_num(sl.get("price")),
        sl_pnl=_num(sl.get("pnl")),
        sl_at=str(sl.get("at") or ""),
        sl_reason=str(sl.get("leg") or ""),
        mfe_price=_num(trade.get("mfe_price")),
        mae_price=_num(trade.get("mae_price")),
        booked_legs=legs,
    )


@dataclass
class MissedSetup:
    """A KRONOS setup that was identified but never executed."""

    date: str
    time_identified: str
    symbol: str
    strike: str
    option_type: str
    planned_entry: float
    status: str
    reason: str = ""
    light: str = ""
    setup_id: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.time_identified = str(self.time_identified or "")
        self.status = str(self.status or "Watching")
        if not self.setup_id:
            self.setup_id = (
                f"{self.date}|{self.symbol}|{self.strike}|"
                f"{self.option_type}|{self.time_identified}"
            )

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "MissedSetup":
        names = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in (item or {}).items() if key in names})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperLedger:
    def __init__(
        self,
        capital: float = START_CAPITAL,
        vault: float = 0.0,
        trades: list[PaperTrade] | None = None,
        days: dict[str, dict[str, Any]] | None = None,
        missed: list[MissedSetup] | None = None,
    ) -> None:
        self.capital = float(capital)
        self.vault = float(vault)
        self.trades: list[PaperTrade] = list(trades or [])
        self.days: dict[str, dict[str, Any]] = dict(days or {})
        self.missed: list[MissedSetup] = list(missed or [])

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path = LEDGER_FILE) -> "PaperLedger":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(
            capital=_num(raw.get("capital"), START_CAPITAL),
            vault=_num(raw.get("vault")),
            trades=[PaperTrade.from_dict(item) for item in raw.get("trades", [])],
            days=raw.get("days", {}) or {},
            missed=[MissedSetup.from_dict(item) for item in raw.get("missed", [])],
        )

    def save(self, path: Path = LEDGER_FILE) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "capital": round(self.capital, 2),
            "vault": round(self.vault, 2),
            "trades": [trade.as_dict() for trade in self.trades],
            "days": self.days,
            "missed": [setup.as_dict() for setup in self.missed],
        }
        # Atomic replace prevents the dashboard from reading a half-written
        # ledger while the live session is banking a leg.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    def reset(self, path: Path = LEDGER_FILE) -> "PaperLedger":
        self.capital = START_CAPITAL
        self.vault = 0.0
        self.trades.clear()
        self.days.clear()
        self.missed.clear()
        self.save(path)
        return self

    # -- recording ---------------------------------------------------------
    def register_day(self, date: str, light: str, note: str = "") -> None:
        self.days.setdefault(date, {"light": light, "note": note, "trades": []})
        if note:
            self.days[date]["note"] = note

    def record(self, trade: PaperTrade) -> PaperTrade:
        """Append a completed trade (the historical replay path)."""
        self.trades.append(trade)
        day = self.days.setdefault(trade.date, {"light": trade.light, "note": "", "trades": []})
        day["light"] = trade.light or day.get("light", "RED")
        day.setdefault("trades", []).append(trade.as_dict())
        self.vault = round(self.vault + max(trade.vaulted, 0.0), 2)
        self.capital = round(self.capital + trade.net, 2)
        return trade

    @staticmethod
    def _same_trade(left: PaperTrade, right: PaperTrade) -> bool:
        if left.trade_id and right.trade_id and left.trade_id == right.trade_id:
            return True
        return (
            left.date == right.date
            and left.symbol == right.symbol
            and left.strike == right.strike
            and abs(left.entry - right.entry) < 0.01
            and (not left.entry_at or not right.entry_at or left.entry_at == right.entry_at)
        )

    def upsert_trade(self, trade: PaperTrade) -> PaperTrade:
        """Insert or update a live trade without double-counting capital.

        The live session calls this when a trade opens, when T1/T2 books, and
        when the trailing stop closes the remainder.  Capital and vault move by
        the delta, never by the full row twice.
        """
        existing = next((row for row in self.trades if self._same_trade(row, trade)), None)
        if existing is not None:
            old_net = _num(existing.net)
            old_vault = max(_num(existing.vaulted), 0.0)
            for item in fields(PaperTrade):
                setattr(existing, item.name, getattr(trade, item.name))
            existing.__post_init__()
            self.capital = round(self.capital + existing.net - old_net, 2)
            self.vault = round(
                self.vault + max(existing.vaulted, 0.0) - old_vault, 2
            )
            self.days.setdefault(existing.date, {"light": existing.light, "note": "", "trades": []})
            self.days[existing.date]["light"] = existing.light or self.days[existing.date].get("light", "RED")
            return existing

        self.trades.append(trade)
        self.capital = round(self.capital + trade.net, 2)
        self.vault = round(self.vault + max(trade.vaulted, 0.0), 2)
        day = self.days.setdefault(trade.date, {"light": trade.light, "note": "", "trades": []})
        day["light"] = trade.light or day.get("light", "RED")
        return trade

    @staticmethod
    def _same_setup(left: MissedSetup, right: MissedSetup) -> bool:
        if left.setup_id and right.setup_id and left.setup_id == right.setup_id:
            return True
        return (
            left.date == right.date
            and left.symbol == right.symbol
            and left.strike == right.strike
            and left.option_type == right.option_type
            and left.time_identified == right.time_identified
        )

    def upsert_missed(self, setup: MissedSetup) -> MissedSetup:
        """Keep one row per setup while its status evolves (Watching -> Missed)."""
        existing = next((row for row in self.missed if self._same_setup(row, setup)), None)
        if existing is not None:
            existing.status = setup.status
            existing.reason = setup.reason or existing.reason
            existing.light = setup.light or existing.light
            existing.updated_at = setup.updated_at or existing.updated_at
            if setup.planned_entry:
                existing.planned_entry = setup.planned_entry
            return existing
        self.missed.append(setup)
        self.days.setdefault(setup.date, {"light": setup.light or "RED", "note": "", "trades": []})
        return setup

    def record_missed(self, setup: MissedSetup) -> MissedSetup:
        return self.upsert_missed(setup)

    def remove_missed(self, setup_id: str) -> None:
        if not setup_id:
            return
        self.missed = [row for row in self.missed if row.setup_id != setup_id]

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
        win_rate = round(wins / len(trades) * 100.0, 1) if trades else 0.0
        return {
            "date": date,
            "light": light,
            "trades": len(trades),
            "wins": wins,
            "breakevens": breakeven,
            "sls": losses,
            "recoveries": recoveries,
            "win_rate": win_rate,
            "strikes": [t.strike for t in trades],
            "points": round(points, 2),
            "gross": round(gross, 2),
            "costs": round(costs, 2),
            "net": round(net, 2),
            "vault": round(vaulted, 2),
            "missed": sum(1 for m in self.missed if m.date == date),
        }

    def serialize_trade(self, trade: PaperTrade) -> dict[str, Any]:
        """The exact row shape consumed by the Pro Trade Ledger modal."""
        entry = _num(trade.entry)
        qty = int(trade.initial_qty or trade.qty or 0)
        t1_leg = next((leg for leg in trade.booked_legs
                       if str(leg.get("leg", "")).upper() == "T1"), None)
        t2_leg = next((leg for leg in trade.booked_legs
                       if str(leg.get("leg", "")).upper() == "T2"), None)
        sl_leg = next((leg for leg in trade.booked_legs
                       if str(leg.get("leg", "")).upper() in {"SL", "TSL", "TRAIL", "STOP"}), None)
        t1_qty = int(trade.t1_qty or (t1_leg or {}).get("qty") or 0)
        t2_qty = int(trade.t2_qty or (t2_leg or {}).get("qty") or 0)
        t1_price = _num(trade.t1_price if trade.t1_price else (t1_leg or {}).get("price"))
        t2_price = _num(trade.t2_price if trade.t2_price else (t2_leg or {}).get("price"))
        t1_pnl = _num(trade.t1_pnl if trade.t1_pnl else (t1_leg or {}).get("pnl"))
        t2_pnl = _num(trade.t2_pnl if trade.t2_pnl else (t2_leg or {}).get("pnl"))
        sl_price = _num(trade.sl_price if trade.sl_price else (sl_leg or {}).get("price"))
        sl_pnl = _num(trade.sl_pnl if trade.sl_pnl else (sl_leg or {}).get("pnl"))
        t1_at = trade.t1_at or str((t1_leg or {}).get("at") or "")
        t2_at = trade.t2_at or str((t2_leg or {}).get("at") or "")
        sl_at = trade.sl_at or str((sl_leg or {}).get("at") or "")

        mfe = _num(trade.mfe_price)
        mae = _num(trade.mae_price)
        if mfe:
            max_profit = round(max((mfe - entry) * qty, 0.0), 2)
        else:
            max_profit = round(max(_num(trade.net), 0.0), 2)
        if mae:
            max_loss = round(min((mae - entry) * qty, 0.0), 2)
        else:
            max_loss = round(min(_num(trade.net), 0.0), 2)

        return {
            "trade_id": trade.trade_id,
            "date": trade.date,
            "time": _clock(trade.entry_at or trade.exit_at),
            "entry_at": trade.entry_at,
            "exit_at": trade.exit_at,
            "symbol": trade.symbol,
            "strike": trade.strike,
            "option_type": trade.option_type,
            "qty": qty,
            "initial_qty": qty,
            "qty_open": int(trade.qty_open or 0),
            "entry": round(entry, 2),
            "entry_price": round(entry, 2),
            "exit_price": round(_num(trade.exit), 2),
            "points": round(_num(trade.points), 2),
            "gross": round(_num(trade.gross), 2),
            "costs": round(_num(trade.costs), 2),
            "net": round(_num(trade.net), 2),
            "vaulted": round(_num(trade.vaulted), 2),
            "outcome": trade.outcome,
            "status": trade.status,
            "light": trade.light,
            "t1": ({"qty": t1_qty, "price": round(t1_price, 2),
                    "pnl": round(t1_pnl, 2), "at": t1_at} if t1_qty else None),
            "t2": ({"qty": t2_qty, "price": round(t2_price, 2),
                    "pnl": round(t2_pnl, 2), "at": t2_at} if t2_qty else None),
            "sl": ({"price": round(sl_price, 2), "pnl": round(sl_pnl, 2),
                    "at": sl_at, "reason": trade.sl_reason or str((sl_leg or {}).get("leg") or "")}
                   if (sl_price or sl_leg) else None),
            "max_profit": max_profit,
            "max_loss": max_loss,
            "mfe_price": round(mfe, 2),
            "mae_price": round(mae, 2),
            "booked_legs": trade.booked_legs,
        }

    def serialize_missed(self, setup: MissedSetup) -> dict[str, Any]:
        return {
            "setup_id": setup.setup_id,
            "date": setup.date,
            "time": _clock(setup.time_identified),
            "time_identified": setup.time_identified,
            "symbol": setup.symbol,
            "strike": setup.strike,
            "option_type": setup.option_type,
            "planned_entry": round(_num(setup.planned_entry), 2),
            "status": setup.status,
            "reason": setup.reason,
            "light": setup.light,
        }

    def day_ledger(self, date: str) -> dict[str, Any]:
        trades = [t for t in self.trades if t.date == date]
        missed = [m for m in self.missed if m.date == date]
        return {
            "date": date,
            "summary": self.day_summary(date),
            "trades": [self.serialize_trade(t) for t in trades],
            "missed": [self.serialize_missed(m) for m in missed],
        }

    def calendar_days(self) -> list[dict[str, Any]]:
        dates = sorted(
            {t.date for t in self.trades}
            | {m.date for m in self.missed}
            | set(self.days)
        )
        out: list[dict[str, Any]] = []
        for date in dates:
            summary = self.day_summary(date)
            net = _num(summary["net"])
            status = "green" if net > 0.005 else "red" if net < -0.005 else "gray"
            out.append({
                "date": date,
                "net": round(net, 2),
                "trades": summary["trades"],
                "wins": summary["wins"],
                "win_rate": summary["win_rate"],
                "missed": summary["missed"],
                "light": summary["light"],
                "status": status,
                "capital_protected": summary["trades"] == 0,
            })
        return out

    def week_table(self) -> list[dict[str, Any]]:
        dates = sorted({t.date for t in self.trades} | set(self.days) | {m.date for m in self.missed})
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
        dates = sorted(
            {t.date for t in self.trades}
            | {m.date for m in self.missed}
            | set(self.days)
        )
        days_detail = {date: self.day_ledger(date) for date in dates}
        return {
            "start_capital": START_CAPITAL,
            "capital": round(self.capital, 2),
            "vault": round(self.vault, 2),
            "trades": len(self.trades),
            "missed_total": len(self.missed),
            "net": round(sum(t.net for t in self.trades), 2),
            "rows": rows,
            "calendar": self.calendar_days(),
            "days_detail": days_detail,
            "today": today_ist(),
        }

    def publish_firebase(self, path: str = "/kronos_state/trade_ledger.json") -> dict[str, Any]:
        """Optional RTDB mirror. Missing config or offline is never fatal."""
        try:
            import requests

            from state.firebase_sync import admin

            url, token = admin()
            response = requests.put(
                f"{url}{path}", params={"access_token": token},
                json=self.snapshot(), timeout=15,
            )
            return {"ok": response.status_code == 200, "status": response.status_code}
        except Exception as exc:  # noqa: BLE001 - publishing must not stop the ledger
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

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
