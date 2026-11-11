"""Unified command-center read model for the dashboard.

The service joins only verified live artifacts:

* the live Upstox snapshot / full-spectrum book
* the sync verifier's per-stream and per-model counters
* the decision ledger's closed outcomes
* the 30-minute institutional footprint tracker
* the date-keyed archive written by the asynchronous archiver

A missing artifact becomes an explicit waiting state.  It never becomes a
genuine-looking price or a synthetic trade.
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from market.institutional_footprint_tracker import InstitutionalFootprintTracker, as_ist
from market.single_pass_sync_verifier import verify_feed
from state.firebase_log_archiver import ARCHIVE_ROOT, FirebaseLogArchiver, date_key
from state.telugu_narration_logger import SPLIT_SCOPE, SPLIT_STREAM_KEYS
from strategy.human_vs_kronos_analyzer import build_live_accuracy

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
REPO_ROOT = Path(__file__).resolve().parents[1]
TODAY = "today"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _model_cards(feed: Mapping[str, Any]) -> list[dict[str, Any]]:
    cards = feed.get("model_thoughts_te")
    if isinstance(cards, list):
        return [dict(row) for row in cards if isinstance(row, Mapping)]
    return []


def _model_logs(feed: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cards = _model_cards(feed)
    for card in cards:
        text = str(card.get("thinking_te") or "").strip()
        decision = str(card.get("decision_state") or "").strip()
        if not text and not decision:
            continue
        model = str(card.get("name") or card.get("role") or "KRONOS")
        rows.append({
            "stream": "model",
            "title": "🧠 3-Models & Human-Lead Reasoning Log",
            "ts": str(feed.get("updated_at") or ""),
            "time_ist": str(feed.get("updated_at") or "")[11:19],
            "date": as_ist(feed.get("updated_at")).strftime("%Y-%m-%d"),
            "kind": "decision" if card.get("gate_passed") else "thought",
            "text": f"🧠 {model}: {text} · నిర్ణయం: {decision}",
            "meta": {"model": model, "gate_passed": bool(card.get("gate_passed"))},
        })
    brain = feed.get("brain_log_te")
    if isinstance(brain, list):
        for row in brain:
            row = _mapping(row)
            text = str(row.get("message") or "").strip()
            if not text:
                continue
            model = str(row.get("category") or row.get("tag") or "KRONOS")
            rows.append({
                "stream": "model",
                "title": "🧠 3-Models & Human-Lead Reasoning Log",
                "ts": str(row.get("timestamp") or feed.get("updated_at") or ""),
                "time_ist": str(row.get("ts") or "")[:12],
                "date": as_ist(feed.get("updated_at")).strftime("%Y-%m-%d"),
                "kind": "decision",
                "text": f"{model} · {text}",
                "meta": {"model": model},
            })
    return rows


def _current_scalp(feed: Mapping[str, Any]) -> list[dict[str, Any]]:
    block = feed.get("scalper")
    if not isinstance(block, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for symbol, raw in block.items():
        raw = _mapping(raw)
        decision = _mapping(raw.get("decision")) or raw
        if not decision:
            continue
        trigger = str(decision.get("trigger") or decision.get("reason") or "").strip()
        entry = _float(decision.get("entry"))
        if not trigger and not entry:
            continue
        rows.append({
            "stream": "scalper",
            "title": "⚡ ₹35K Scalper Live Telugu Log",
            "ts": str(feed.get("updated_at") or ""),
            "time_ist": str(feed.get("updated_at") or "")[11:19],
            "date": as_ist(feed.get("updated_at")).strftime("%Y-%m-%d"),
            "kind": "entry" if entry else "scan",
            "text": (
                f"⚡ స్కాల్ప్ {symbol}: {trigger or 'స్కాన్'} · "
                f"Entry ₹{entry:,.2f} · OBI {_float(decision.get('obi_5'), 0):.2f}"
            ),
            "meta": {"symbol": symbol, "entry": entry, "trigger": trigger},
        })
    return rows


def _current_intraday(feed: Mapping[str, Any]) -> list[dict[str, Any]]:
    block = feed.get("sniper")
    if not isinstance(block, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for symbol, raw in block.items():
        raw = _mapping(raw)
        trigger = str(raw.get("trigger") or raw.get("reason") or raw.get("action") or "").strip()
        if not trigger:
            continue
        rows.append({
            "stream": "intraday",
            "title": "🎯 ₹65K Intraday Sniper & SMC Telugu Log",
            "ts": str(feed.get("updated_at") or ""),
            "time_ist": str(feed.get("updated_at") or "")[11:19],
            "date": as_ist(feed.get("updated_at")).strftime("%Y-%m-%d"),
            "kind": "entry" if raw.get("trigger") else "setup",
            "text": f"🎯 SMC {symbol}: {trigger} · {raw.get('side') or 'WAIT'}",
            "meta": {"symbol": symbol, "side": raw.get("side"), "trigger": trigger},
        })
    return rows


class CommandCenterService:
    """Serialise the command center and date-wise downloadable archives."""

    def __init__(
        self,
        *,
        feed_path: Path | str | None = None,
        archive_root: Path | str | None = None,
        clock: Any = None,
    ) -> None:
        self.feed_path = Path(feed_path) if feed_path else REPO_ROOT / "state" / "live_feed.json"
        self.archive = FirebaseLogArchiver(
            root=archive_root or ARCHIVE_ROOT,
            start=False,
        )
        self.clock = clock or time.time
        self.footprint = InstitutionalFootprintTracker()

    def _feed(self) -> dict[str, Any]:
        return _load_json(self.feed_path)

    def live(self, symbol: str = "NIFTY") -> dict[str, Any]:
        now = float(self.clock())
        feed = self._feed()
        symbol = str(symbol or "NIFTY").upper()
        day = as_ist(now).strftime("%Y-%m-%d")
        sync = verify_feed(feed, symbol=symbol, now=now)
        accuracy = build_live_accuracy(feed, date=day)
        windows = self.footprint.observe_feed(feed, symbol=symbol, now=now)
        archived = self.archive.load_day(day)
        logs = {key: [] for key in SPLIT_STREAM_KEYS}
        # The live session's own split streams win when it has published them.
        published = _mapping(feed.get("command_center_logs"))
        for key in SPLIT_STREAM_KEYS:
            rows = published.get(key)
            if isinstance(rows, list):
                logs[key] = [dict(row) for row in rows if isinstance(row, Mapping)]
        if not logs["model"]:
            logs["model"] = _model_logs(feed)
        if not logs["scalper"]:
            logs["scalper"] = _current_scalp(feed)
        if not logs["intraday"]:
            logs["intraday"] = _current_intraday(feed)
        return {
            "ok": True,
            "date": day,
            "symbol": symbol,
            "as_of": datetime.fromtimestamp(now, tz=IST).isoformat(timespec="milliseconds"),
            "source": "live_feed",
            "sync": sync,
            "data_gate": sync["data_gate"],
            "accuracy": accuracy,
            "logs": logs,
            "models": _model_cards(feed),
            "whale_windows": windows,
            "archive": {
                "available": bool(archived),
                "scopes": sorted(archived),
                "records": sum(len(rows) for rows in archived.values()),
            },
        }

    def history(self, date: str) -> dict[str, Any]:
        day = date_key(date)
        archived = self.archive.load_day(day)
        logs = {key: [] for key in SPLIT_STREAM_KEYS}
        for key, scope in SPLIT_SCOPE.items():
            rows = archived.get(scope) or []
            logs[key] = [dict(row) for row in rows if isinstance(row, Mapping)]
        whale_rows = archived.get("whale_30m_footprints") or []
        windows = self._archived_windows(day, whale_rows)
        accuracy_rows = archived.get("accuracy_summary") or []
        accuracy = self._archived_accuracy(day, accuracy_rows)
        return {
            "ok": True,
            "date": day,
            "source": "disk_archive" if archived else "no_archive_for_date",
            "archive_available": bool(archived),
            "sync": (accuracy_rows[-1].get("sync") if accuracy_rows else None) or {
                "live_status": "OFFLINE_OR_STALE_FEED",
                "data_gate": {
                    "allowed": False, "status": "BLOCKED",
                    "live_status": "OFFLINE_OR_STALE_FEED",
                    "reason": "NO_VERIFIED_ARCHIVE_FOR_DATE", "zero_dummy": True,
                },
            },
            "accuracy": accuracy,
            "logs": logs,
            "models": [],
            "whale_windows": windows,
            "archive": {
                "available": bool(archived),
                "scopes": sorted(archived),
                "records": sum(len(rows) for rows in archived.values()),
            },
        }

    def _archived_windows(self, day: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if rows:
            for row in reversed(rows):
                windows = row.get("windows")
                if isinstance(windows, list) and windows:
                    return [dict(item) for item in windows if isinstance(item, Mapping)]
            direct = [dict(row) for row in rows if row.get("window") and row.get("metrics")]
            if direct:
                direct.sort(key=lambda item: int(_float(item.get("index"), 0)))
                return direct
        return self.footprint.snapshot(day)

    @staticmethod
    def _archived_accuracy(day: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if rows:
            last = dict(rows[-1])
            last.setdefault("date", day)
            if "decisions" not in last:
                last["decisions"] = []
            return last
        return {
            "ok": True, "date": day, "status": "WAITING_FOR_CLOSED_TRADES",
            "total_decisions": 0, "scored_decisions": 0,
            "winning_trades": 0, "losing_trades": 0,
            "profitable_early_exits": 0, "correctly_blocked_traps": 0,
            "open_decisions": 0, "trade_win_rate_pct": None,
            "decision_accuracy_pct": None, "avg_lead_time_sec": None,
            "lead_samples": 0, "decisions": [],
        }

    def download(self, date: str, scope: str = "all", fmt: str = "txt") -> dict[str, Any]:
        day = date_key(date)
        scope = str(scope or "all").lower()
        fmt = str(fmt or "txt").lower()
        if scope not in {"all", "scalper", "intraday", "whale"}:
            raise ValueError("scope must be all|scalper|intraday|whale")
        if fmt not in {"txt", "jsonl", "csv"}:
            raise ValueError("format must be txt|jsonl|csv")
        payload = self.history(day)
        rows: list[dict[str, Any]] = []
        pairs = (
            (("scalper", "scalper_logs"), ("intraday", "intraday_sniper_logs"),
             ("model", "model_thoughts"))
            if scope == "all" else
            ({"scalper": (("scalper", "scalper_logs"),),
              "intraday": (("intraday", "intraday_sniper_logs"),),
              "whale": (("whale", "whale_30m_footprints"),)}[scope])
        )
        for key, archive_scope in pairs:
            for row in payload["logs"].get(key, []) if key in payload["logs"] else []:
                rows.append({**dict(row), "scope": key, "archive_scope": archive_scope})
        if scope in {"all", "whale"}:
            for window in payload["whale_windows"]:
                rows.append({
                    "scope": "whale",
                    "archive_scope": "whale_30m_footprints",
                    "ts": f"{day}T{window.get('start_ist', '00:00')}:00+05:30",
                    "text": (window.get("whale_zone") or {}).get("text_te") or "",
                    "window": window,
                })
        if scope == "all":
            rows.append({
                "scope": "accuracy",
                "archive_scope": "accuracy_summary",
                "ts": f"{day}T15:30:00+05:30",
                "text": json.dumps(payload["accuracy"], ensure_ascii=False, default=str),
                "accuracy": payload["accuracy"],
            })
        rows.sort(key=lambda item: str(item.get("ts") or ""))
        body = self._render(rows, day, scope, fmt)
        filename = f"kronos_command_center_{day}_{scope}.{fmt}"
        content_type = {
            "txt": "text/plain; charset=utf-8",
            "jsonl": "application/x-ndjson; charset=utf-8",
            "csv": "text/csv; charset=utf-8",
        }[fmt]
        return {
            "body": body,
            "filename": filename,
            "content_type": content_type,
            "rows": len(rows),
            "date": day,
            "scope": scope,
            "format": fmt,
        }

    @staticmethod
    def _render(rows: list[dict[str, Any]], day: str, scope: str, fmt: str) -> bytes:
        if fmt == "jsonl":
            lines = [
                json.dumps({"date": day, **row}, ensure_ascii=False, default=str)
                for row in rows
            ]
            return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        if fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["date", "scope", "ts", "text", "data"])
            for row in rows:
                writer.writerow([
                    day, row.get("scope", ""), row.get("ts", ""), row.get("text", ""),
                    json.dumps(row, ensure_ascii=False, default=str),
                ])
            return buffer.getvalue().encode("utf-8")
        lines = [f"KRONOS COMMAND CENTER · {day} · scope={scope}", "=" * 72]
        for row in rows:
            lines.append(f"{row.get('ts', '')}  [{row.get('scope', '')}]  {row.get('text', '')}")
        if not rows:
            lines.append("No verified archived records for this date.")
        return ("\n".join(lines) + "\n").encode("utf-8")


def build_command_center_service(**kwargs: Any) -> CommandCenterService:
    return CommandCenterService(**kwargs)


__all__ = ["CommandCenterService", "TODAY", "build_command_center_service"]
