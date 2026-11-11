"""Fault-tolerant, non-blocking date-keyed archival for KRONOS.

The live trading loop only calls :meth:`FirebaseLogArchiver.enqueue`, which is
an in-memory ``put_nowait``.  A daemon worker owns disk and Firebase I/O:

1. append the record to ``state/archive/YYYY-MM-DD/<scope>.jsonl``
2. PATCH the same record to ``/kronos_archive/YYYY-MM-DD/<scope>``
3. retry with exponential backoff
4. retain the local JSONL as the source of truth if Firebase stays unavailable

Firebase is therefore a black-box archiver, never a latency contributor to the
live SSE path.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = REPO_ROOT / "state" / "archive"
SCOPES = (
    "scalper_logs",
    "intraday_sniper_logs",
    "model_thoughts",
    "whale_30m_footprints",
    "accuracy_summary",
)
DATE_FORMAT = "%Y-%m-%d"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def ist_now(epoch: float | None = None) -> datetime:
    return datetime.fromtimestamp(float(epoch if epoch is not None else time.time()), tz=IST)


def date_key(value: str | datetime | float | None = None) -> str:
    if value is None:
        return ist_now().strftime(DATE_FORMAT)
    if isinstance(value, str):
        return datetime.strptime(value[:10], DATE_FORMAT).strftime(DATE_FORMAT)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=IST)
        return moment.astimezone(IST).strftime(DATE_FORMAT)
    return ist_now(float(value)).strftime(DATE_FORMAT)


def _safe_scope(scope: str) -> str:
    clean = str(scope or "").strip().lower()
    if not clean or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in clean):
        raise ValueError("scope must contain lowercase letters, digits or underscore")
    return clean


def _default_push(path: str, record: Mapping[str, Any]) -> None:
    # Imported lazily so importing this module never needs credentials/network.
    from state.firebase_sync import admin, rt_patch

    url, token = admin()
    status = rt_patch(url, token, f"{path}.json", {record["record_id"]: dict(record)})
    if status not in (200, 201, 204):
        raise RuntimeError(f"Firebase PATCH HTTP {status}")


class FirebaseLogArchiver:
    """Queue-backed disk + Firebase writer with retry and local fallback."""

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        firebase_push: Callable[[str, Mapping[str, Any]], Any] | None = None,
        clock: Callable[[], float] = time.time,
        max_queue: int = 20_000,
        retries: int = 4,
        start: bool = True,
    ) -> None:
        self.root = Path(root) if root else Path(os.environ.get("KRONOS_ARCHIVE_DIR", ARCHIVE_ROOT))
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.firebase_push = firebase_push or _default_push
        self.max_queue = max(100, int(max_queue))
        self.retries = max(1, int(retries))
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=self.max_queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stats: dict[str, Any] = {
            "queued": 0,
            "persisted": 0,
            "firebase_ok": 0,
            "firebase_failed": 0,
            "local_fallback": 0,
            "retries": 0,
            "dropped": 0,
            "enqueue_max_ms": 0.0,
        }
        if start:
            self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="kronos-archive")
        self._thread.start()

    def enqueue(
        self,
        scope: str,
        payload: Mapping[str, Any] | str,
        *,
        date: str | datetime | float | None = None,
        ts: str | datetime | float | None = None,
    ) -> dict[str, Any]:
        """Return immediately; the worker performs all I/O."""
        clean = _safe_scope(scope)
        day = date_key(date if date is not None else ts)
        moment = ts if ts is not None else self.clock()
        if isinstance(moment, datetime):
            moment_iso = moment.astimezone(IST).isoformat(timespec="milliseconds")
        elif isinstance(moment, str):
            moment_iso = moment
        else:
            moment_iso = ist_now(float(moment)).isoformat(timespec="milliseconds")
        body = payload if isinstance(payload, Mapping) else {"text": str(payload)}
        record = dict(body)
        record.update(
            {
                "record_id": uuid.uuid4().hex,
                "date": day,
                "scope": clean,
                "ts": moment_iso,
            }
        )
        started = time.perf_counter()
        try:
            self.queue.put_nowait(record)
            queued = True
        except queue.Full:
            # Overflow is the only path allowed to touch disk inline.  It keeps
            # the black box lossless even during an extreme burst.
            self._append_local(record)
            queued = False
            with self._lock:
                self.stats["dropped"] += 1
                self.stats["local_fallback"] += 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self.stats["queued"] += 1
            self.stats["enqueue_max_ms"] = max(self.stats["enqueue_max_ms"], round(elapsed_ms, 4))
        return {"queued": queued, "record_id": record["record_id"],
                "enqueue_ms": round(elapsed_ms, 4), "date": day, "scope": clean}

    def _archive_path(self, day: str, scope: str) -> Path:
        return self.root / day / f"{scope}.jsonl"

    def _append_local(self, record: Mapping[str, Any]) -> Path:
        path = self._archive_path(str(record["date"]), str(record["scope"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return path

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                record = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if record is None:
                self.queue.task_done()
                break
            try:
                self._append_local(record)
                with self._lock:
                    self.stats["persisted"] += 1
                self._push(record)
            finally:
                self.queue.task_done()

    def _push(self, record: Mapping[str, Any]) -> None:
        path = f"/kronos_archive/{record['date']}/{record['scope']}"
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.firebase_push(path, record)
                with self._lock:
                    self.stats["firebase_ok"] += 1
                return
            except Exception as exc:  # noqa: BLE001 - retry is the contract
                last_error = exc
                with self._lock:
                    self.stats["retries"] += 1
                time.sleep(min(2.0, 0.15 * (2 ** attempt)))
        with self._lock:
            self.stats["firebase_failed"] += 1
            self.stats["local_fallback"] += 1
            self.stats["last_error"] = type(last_error).__name__ if last_error else "unknown"

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self.queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self.queue.unfinished_tasks == 0

    def close(self, timeout: float = 5.0) -> bool:
        self.flush(timeout=timeout)
        self._stop.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)
        return not self._thread or not self._thread.is_alive()

    def load_day(self, date: str) -> dict[str, list[dict[str, Any]]]:
        """Read every archived scope for a date, newest rows last."""
        day = date_key(date)
        out: dict[str, list[dict[str, Any]]] = {}
        directory = self.root / day
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.jsonl")):
            rows: list[dict[str, Any]] = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
            except OSError:
                continue
            rows.sort(key=lambda item: str(item.get("ts") or ""))
            out[path.stem] = rows
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "queue_depth": self.queue.qsize(),
                "root": str(self.root),
                "scopes": list(SCOPES),
                **dict(self.stats),
            }


__all__ = [
    "ARCHIVE_ROOT",
    "DATE_FORMAT",
    "FirebaseLogArchiver",
    "IST",
    "SCOPES",
    "date_key",
    "ist_now",
]
