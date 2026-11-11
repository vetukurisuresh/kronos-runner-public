"""Human candle-close lag vs. KRONOS intra-candle signals.

Retail traders act after a 1m/5m candle closes plus a reaction delay.  KRONOS
acts on order-book pressure, futures DVTT and the p10/p90 path projection while
the candle is still forming.  This module computes that lead in seconds and in
rupees, then scores the day's decisions without turning open trades into wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
HUMAN_REACTION_SECONDS = 3.5
TRAP_HORIZON_SECONDS = 300


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=IST)
        return moment.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.timestamp()
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out > 100_000_000_000:
        out /= 1000.0
    return out


def _side(value: Any) -> str:
    side = str(value or "").upper()
    if "PE" in side or "PUT" in side or side in {"SHORT", "SELL"}:
        return "PE"
    return "CE"


def human_failure_reason_te(
    *,
    signal_price: float,
    human_price: float | None,
    lead_seconds: float,
    advantage_inr: float | None,
    side: str,
    obi_5: float | None,
    tbq_tsq: float | None,
) -> str:
    """Explain the exact late-entry failure in clear Telugu."""
    obi_txt = f"{obi_5:.2f}" if obi_5 is not None else "—"
    tbq_txt = f"{tbq_tsq:.2f}" if tbq_tsq is not None else "—"
    if advantage_inr is None or human_price is None:
        return (
            f"సాధారణ ట్రేడర్ క్యాండిల్ క్లోజ్ కోసం ఆగుతాడు; KRONOS మాత్రం "
            f"OBI {obi_txt}, TBQ/TSQ {tbq_txt} చదివి {lead_seconds:.0f} సెకన్ల ముందే "
            f"₹{signal_price:,.2f} వద్ద {side} ఎంట్రీని గుర్తించింది."
        )
    if _side(side) == "PE":
        action, comparison = "అమ్మి", "తక్కువ"
    else:
        action, comparison = "కొని", "ఎక్కువ"
    return (
        f"సాధారణ ట్రేడర్ క్యాండిల్ క్లోజ్ అయ్యేవరకు ఆగి ₹{human_price:,.2f} వద్ద "
        f"{action} ₹{abs(advantage_inr):,.2f} {comparison} ధరకు ఎంటర్ అవుతాడు; "
        f"అప్పుడే ఆపరేటర్ ట్రాప్ మొదలవుతుంది. కానీ KRONOS ఆర్డర్-బుక్ ప్రెజర్ "
        f"(OBI {obi_txt}, TBQ/TSQ {tbq_txt}) తో {lead_seconds:.0f} సెకన్ల ముందే "
        f"₹{signal_price:,.2f} వద్ద ఎంట్రీని గుర్తించింది!"
    )


def calculate_lead_gap(
    *,
    signal_time: Any,
    candle_close_time: Any,
    signal_price: Any,
    human_price: Any = None,
    side: str = "CE",
    reaction_seconds: float = HUMAN_REACTION_SECONDS,
    obi_5: Any = None,
    tbq_tsq: Any = None,
) -> dict[str, Any]:
    """Return the exact seconds/rupees advantage over a candle-close trader."""
    signal_epoch = _epoch(signal_time)
    close_epoch = _epoch(candle_close_time)
    if signal_epoch is None or close_epoch is None:
        raise ValueError("signal_time and candle_close_time must be valid timestamps")
    lead = (close_epoch - signal_epoch) + max(0.0, _float(reaction_seconds, HUMAN_REACTION_SECONDS))
    entry = _float(signal_price)
    late = _float(human_price) if human_price not in (None, "") else None
    advantage = None
    if entry > 0 and late is not None and late > 0:
        advantage = late - entry if _side(side) == "CE" else entry - late
        advantage = round(advantage, 2)
    reason = human_failure_reason_te(
        signal_price=entry,
        human_price=late,
        lead_seconds=lead,
        advantage_inr=advantage,
        side=_side(side),
        obi_5=_float(obi_5) if obi_5 not in (None, "") else None,
        tbq_tsq=_float(tbq_tsq) if tbq_tsq not in (None, "") else None,
    )
    return {
        "signal_time": datetime.fromtimestamp(signal_epoch, tz=IST).isoformat(timespec="milliseconds"),
        "candle_close_time": datetime.fromtimestamp(close_epoch, tz=IST).isoformat(timespec="milliseconds"),
        "t_signal_epoch": round(signal_epoch, 3),
        "t_candle_close_epoch": round(close_epoch, 3),
        "lead_time_seconds_ahead_of_human": round(lead, 2),
        "seconds_ahead": round(lead, 2),
        "human_reaction_seconds": round(max(0.0, _float(reaction_seconds, HUMAN_REACTION_SECONDS)), 2),
        "signal_price": round(entry, 2) if entry else None,
        "human_candle_close_price": round(late, 2) if late else None,
        "price_advantage_inr": advantage,
        "side": _side(side),
        "obi_5": _float(obi_5) if obi_5 not in (None, "") else None,
        "tbq_tsq": _float(tbq_tsq) if tbq_tsq not in (None, "") else None,
        "human_failure_reason_te": reason,
    }


@dataclass
class Decision:
    decision_id: str
    action: str
    outcome: str = "open"          # win | loss | early_exit | blocked_trap | open
    pnl: float = 0.0
    lead_time_seconds_ahead: float | None = None
    trap_confirmed: bool = False
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "action": self.action,
            "outcome": self.outcome,
            "pnl": round(float(self.pnl), 2),
            "lead_time_seconds_ahead": (
                round(float(self.lead_time_seconds_ahead), 2)
                if self.lead_time_seconds_ahead is not None else None
            ),
            "trap_confirmed": bool(self.trap_confirmed),
            "at": self.at,
        }


def _outcome(row: Mapping[str, Any]) -> str:
    value = str(row.get("outcome") or row.get("result") or "open").strip().lower()
    if value in {"win", "won", "profit", "target_hit"}:
        return "win"
    if value in {"loss", "lost", "sl_hit", "stop_hit"}:
        return "loss"
    if value in {"early_exit", "early_profit", "profitable_early_exit"}:
        return "early_exit"
    if value in {"blocked_trap", "trap_blocked", "correctly_blocked"}:
        return "blocked_trap"
    return "open"


def build_accuracy_snapshot(
    decisions: Iterable[Mapping[str, Any] | Decision] | None,
    *,
    date: str | None = None,
) -> dict[str, Any]:
    """Score only closed/verified decisions; open risk is never a win."""
    rows: list[dict[str, Any]] = []
    for raw in decisions or []:
        if isinstance(raw, Decision):
            row = raw.as_dict()
        elif isinstance(raw, Mapping):
            row = dict(raw)
            row["outcome"] = _outcome(row)
        else:
            continue
        rows.append(row)

    wins = [row for row in rows if row["outcome"] == "win"]
    losses = [row for row in rows if row["outcome"] == "loss"]
    early = [row for row in rows if row["outcome"] == "early_exit" and _float(row.get("pnl")) > 0]
    blocked = [row for row in rows if row["outcome"] == "blocked_trap" and bool(row.get("trap_confirmed"))]
    open_rows = [row for row in rows if row["outcome"] == "open"]
    scored = len(wins) + len(losses) + len(early) + len(blocked)
    correct = len(wins) + len(early) + len(blocked)
    leads = [
        _float(row.get("lead_time_seconds_ahead"))
        for row in rows
        if row.get("lead_time_seconds_ahead") not in (None, "")
    ]
    trade_win_rate = (len(wins) / (len(wins) + len(losses)) * 100.0) if (wins or losses) else None
    decision_accuracy = (correct / scored * 100.0) if scored else None
    return {
        "ok": True,
        "date": date,
        "status": "LIVE_ACCURACY" if scored else "WAITING_FOR_CLOSED_TRADES",
        "total_decisions": len(rows),
        "scored_decisions": scored,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "profitable_early_exits": len(early),
        "correctly_blocked_traps": len(blocked),
        "open_decisions": len(open_rows),
        "trade_win_rate_pct": round(trade_win_rate, 1) if trade_win_rate is not None else None,
        "decision_accuracy_pct": round(decision_accuracy, 1) if decision_accuracy is not None else None,
        "avg_lead_time_sec": round(sum(leads) / len(leads), 1) if leads else None,
        "lead_samples": len(leads),
        "decisions": rows,
    }


def decisions_from_feed(feed: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Read only decisions actually published by the broker/strategy pipeline."""
    feed = feed or {}
    for key in ("decision_ledger", "accuracy_decisions", "decisions"):
        rows = feed.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def build_live_accuracy(feed: Mapping[str, Any] | None, *, date: str | None = None) -> dict[str, Any]:
    rows = decisions_from_feed(feed)
    snapshot = build_accuracy_snapshot(rows, date=date)
    snapshot["decisions"] = rows
    return snapshot


__all__ = [
    "HUMAN_REACTION_SECONDS",
    "TRAP_HORIZON_SECONDS",
    "Decision",
    "build_accuracy_snapshot",
    "build_live_accuracy",
    "calculate_lead_gap",
    "decisions_from_feed",
    "human_failure_reason_te",
]
