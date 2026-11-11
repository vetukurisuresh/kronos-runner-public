"""Canonical end-of-day P&L reconciliation.

Every live, paper and journal row is normalised into one rupee shape.  The
reconciler is deliberately side-effect free so the session, SSE payload, tests
and PDF exporter all publish the same grand total.  A discrepancy is reported
in rupees instead of being silently rounded away.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
REPO_ROOT = Path(__file__).resolve().parents[1]
EOD_SUMMARY_FILE = REPO_ROOT / "state" / "eod_pnl.json"
VAULT_FRACTION = 0.50


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if is_dataclass(row):
        return asdict(row)
    if hasattr(row, "as_dict"):
        try:
            return dict(row.as_dict())
        except (TypeError, ValueError):
            return {}
    return {}


def _sleeve(row: Mapping[str, Any]) -> str:
    tag = " ".join(str(row.get(key) or "") for key in (
        "sleeve", "sleeve_id", "sniper_grade", "playbook_id", "scalp_regime",
    )).upper()
    if bool(row.get("scalp_mode")) or "SCALP" in tag or "35K" in tag or tag.strip() == "A":
        return "scalp_35k"
    return "sniper_65k"


def _trigger_label(row: Mapping[str, Any]) -> str:
    explicit = str(
        row.get("trigger_label") or row.get("trigger_models") or row.get("trigger") or ""
    ).strip()
    if explicit and explicit.upper() not in {"TRUE", "FALSE", "1", "0"}:
        return explicit
    models = row.get("confirmed_models") or (row.get("quorum") or {}).get("confirmed_models") or []
    if isinstance(models, str):
        models = [models]
    names = []
    for item in models:
        text = str(item or "").strip()
        if not text:
            continue
        if "(" in text and ")" in text:
            text = text[text.find("(") + 1:text.find(")")]
        names.append(text)
    confidence = _f(
        row.get("quorum_confidence")
        or (row.get("quorum") or {}).get("confidence")
        or row.get("playbook_score")
    )
    if len(names) >= 2:
        return "MULTI: " + " + ".join(names[:3]) + (f" ({confidence:.0f}%)" if confidence else "")
    if len(names) == 1:
        return "SINGLE-SNIPER: " + names[0] + (f" ({confidence:.0f}%)" if confidence else "")
    playbook = str(row.get("playbook_label") or row.get("playbook_id") or "").strip()
    return playbook.upper() if playbook else "MANUAL_OR_LEGACY"


def _leg_price(row: Mapping[str, Any], *names: str) -> float:
    legs = list(row.get("booked_legs") or [])
    for leg in legs:
        name = str((leg or {}).get("leg") or "").upper()
        if any(token.upper() in name for token in names):
            return _f((leg or {}).get("price"))
    return 0.0


def normalise_row(row: Any) -> dict[str, Any]:
    """Return the exact auditable row used by the EOD total and 8K journal."""
    data = _row_dict(row)
    status = str(data.get("status") or "CLOSED").upper()
    gross = _f(
        data.get("gross")
        or data.get("gross_pnl")
        or data.get("gross_pnl_rs")
        or data.get("realized_pnl_rs")
    )
    costs = _f(data.get("costs") or data.get("brokerage_taxes"))
    net = data.get("net")
    if net is None:
        net = data.get("net_pnl_rs")
    if net is None:
        net = gross - costs if status == "CLOSED" else gross
    net = _f(net)
    if not gross:
        gross = round(net + costs, 2)
    qty = int(_f(data.get("initial_qty") or data.get("qty")))
    entry = _f(data.get("entry_price") or data.get("entry"))
    exit_price = _f(data.get("exit_price") or data.get("exit"))
    t1_fill = _f(data.get("t1_fill") or data.get("t1_price") or _leg_price(data, "T1"))
    t2_fill = _f(data.get("t2_fill") or data.get("t2_price") or _leg_price(data, "T2"))
    runner_exit = _f(
        data.get("runner_exit_price") or _leg_price(data, "RUNNER", "TRAIL", "SL", "SCRATCH")
    )
    return {
        "trade_id": str(data.get("trade_id") or ""),
        "date": str(data.get("date") or ""),
        "sleeve": _sleeve(data),
        "status": status,
        "side": str(data.get("option_type") or data.get("side") or "").upper(),
        "symbol": str(data.get("symbol") or ""),
        "strike": str(data.get("strike") or data.get("strike_symbol") or data.get("option_symbol") or ""),
        "qty": qty,
        "entry_time_ms": _f(data.get("entry_time_ms")),
        "decision_latency_ms": round(_f(data.get("decision_latency_ms") or data.get("latency_ms")), 3),
        "trigger_models": _trigger_label(data),
        "entry_price": round(entry, 2),
        "initial_sl": round(_f(data.get("initial_sl")), 2),
        "t1_fill": round(t1_fill, 2),
        "t2_fill": round(t2_fill, 2),
        "runner_exit_price": round(runner_exit, 2),
        "exit_price": round(exit_price, 2),
        "gross_pnl": round(gross, 2),
        "brokerage_taxes": round(costs, 2),
        "net_pnl": round(net, 2),
        "vaulted": round(max(0.0, net) * VAULT_FRACTION, 2),
        "outcome": str(data.get("outcome") or ("OPEN" if status == "OPEN" else "WIN" if net > 0 else "SL" if net < 0 else "BREAKEVEN")),
    }


def _sum(rows: Iterable[Mapping[str, Any]], key: str) -> float:
    return round(sum(_f(row.get(key)) for row in rows), 2)


def reconcile_eod(
    rows: Iterable[Any],
    *,
    date: str = "",
    scalp_open_mtm: Any = 0.0,
    sniper_open_mtm: Any = 0.0,
    journal_rows: Iterable[Any] | None = None,
    reported_vault: Any | None = None,
) -> dict[str, Any]:
    """Reconcile ledger, open MTM and journal into one grand total."""
    normal = [normalise_row(row) for row in rows]
    if date:
        normal = [row for row in normal if not row.get("date") or row["date"] == date]
    scalp = [row for row in normal if row["sleeve"] == "scalp_35k"]
    sniper = [row for row in normal if row["sleeve"] == "sniper_65k"]
    scalp_realized = _sum(scalp, "net_pnl")
    sniper_realized = _sum(sniper, "net_pnl")
    scalp_mtm = round(_f(scalp_open_mtm), 2)
    sniper_mtm = round(_f(sniper_open_mtm), 2)
    scalp_total = round(scalp_realized + scalp_mtm, 2)
    sniper_total = round(sniper_realized + sniper_mtm, 2)
    grand = round(scalp_total + sniper_total, 2)

    ledger_total = round(scalp_realized + sniper_realized, 2)
    journal_normal = [normalise_row(row) for row in (journal_rows if journal_rows is not None else normal)]
    if date:
        journal_normal = [
            row for row in journal_normal if not row.get("date") or row["date"] == date
        ]
    journal_total = _sum(journal_normal, "net_pnl")
    journal_mtm = round(scalp_mtm + sniper_mtm, 2)
    journal_grand = round(journal_total + journal_mtm, 2)
    discrepancy = round(grand - journal_grand, 2)

    closed = [row for row in normal if row["status"] == "CLOSED"]
    wins = [row for row in closed if row["net_pnl"] > 0]
    losses = [row for row in closed if row["net_pnl"] < 0]
    total = len(closed)
    win_rate = round(len(wins) / total * 100.0, 2) if total else 0.0
    gross_profit = round(sum(row["net_pnl"] for row in wins), 2)
    gross_loss = round(abs(sum(row["net_pnl"] for row in losses)), 2)
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (None if not wins else 999.0)
    avg_sl = round(sum(row["net_pnl"] for row in losses) / len(losses), 2) if losses else 0.0
    avg_win = round(sum(row["net_pnl"] for row in wins) / len(wins), 2) if wins else 0.0
    vault = round(sum(max(0.0, row["net_pnl"]) * VAULT_FRACTION for row in normal), 2)
    supplied_vault = None if reported_vault is None else round(_f(reported_vault), 2)
    vault_delta = None if supplied_vault is None else round(supplied_vault - vault, 2)

    summary = {
        "date": date or datetime.now(tz=IST).date().isoformat(),
        "generated_at": datetime.now(tz=IST).isoformat(timespec="milliseconds"),
        "scalp_35k_realized_pnl": scalp_realized,
        "scalp_35k_open_mtm": scalp_mtm,
        "scalp_35k_total": scalp_total,
        "sniper_65k_realized_pnl": sniper_realized,
        "sniper_65k_open_mtm": sniper_mtm,
        "sniper_65k_total": sniper_total,
        "grand_total_net_pnl": grand,
        "grand_total_gross_pnl": _sum(normal, "gross_pnl"),
        "grand_total_brokerage_taxes": _sum(normal, "brokerage_taxes"),
        "vault_locked_inr": vault,
        "vault_fraction_pct": 50.0,
        "reported_vault_inr": supplied_vault,
        "vault_discrepancy_inr": vault_delta,
        "ledger_total_net_pnl": ledger_total,
        "ledger_open_mtm": round(scalp_mtm + sniper_mtm, 2),
        "ledger_grand_total_net_pnl": round(ledger_total + scalp_mtm + sniper_mtm, 2),
        "journal_total_net_pnl": journal_total,
        "journal_grand_total_net_pnl": journal_grand,
        "ledger_journal_discrepancy_inr": discrepancy,
        "reconciled_zero_discrepancy": discrepancy == 0,
        "trades_won": len(wins),
        "trades_lost": len(losses),
        "trades_total": total,
        "trades_open": sum(1 for row in normal if row["status"] == "OPEN"),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "avg_sl_lost_inr": avg_sl,
        "avg_trend_won_inr": avg_win,
        "avg_sl_vs_trend_ratio": round(abs(avg_win / avg_sl), 2) if avg_sl < 0 else None,
        "rows": normal,
    }
    return summary


def write_eod_summary(summary: Mapping[str, Any], path: Path = EOD_SUMMARY_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(summary), indent=1), encoding="utf-8")
    temporary.replace(path)
    return path


__all__ = [
    "EOD_SUMMARY_FILE",
    "VAULT_FRACTION",
    "normalise_row",
    "reconcile_eod",
    "write_eod_summary",
]
