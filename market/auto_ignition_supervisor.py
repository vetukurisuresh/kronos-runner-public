#!/usr/bin/env python3
"""Zero-touch morning ignition supervisor.

The dashboard is the last thing that is always open on the desk.  When it is
running, this daemon thread checks the live session every five seconds during
the 08:25-15:30 IST window:

* Is today's Upstox token present and verified?
* Is ``state/live_feed.json`` fresh (< 8 s)?
* Is ``scripts/upstox_live_session.py`` still alive?

If the token is missing/expired it runs the existing non-interactive TOTP
login.  If the session is absent or its feed has been dead for more than 20 s,
it starts a single supervised session using ``state/live_session.pid`` as the
single-instance lock.  It never places an order; the session remains the only
component that can route a paper trade.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = REPO_ROOT / "state"
LOG_DIR = STATE_DIR / "logs"
FEED_FILE = STATE_DIR / "live_feed.json"
FAST_FILE = STATE_DIR / "live_fast.json"
PID_FILE = STATE_DIR / "live_session.pid"
TOKEN_FILE = STATE_DIR / "upstox_token.json"
SESSION_SCRIPT = REPO_ROOT / "scripts" / "upstox_live_session.py"
MINT_SCRIPT = REPO_ROOT / "scripts" / "auto_upstox_morning_login.py"
SUPERVISOR_LOG = LOG_DIR / "auto_ignition_supervisor.log"

WINDOW_START = dtime(8, 25)
WINDOW_END = dtime(15, 30)
CHECK_SECONDS = 5.0
FEED_STALE_SEC = 8.0
# A cold session downloads 512 bars for four instruments (NIFTY/BANKNIFTY
# spot + futures) before its first feed write.  Do not classify that warm-up as
# a dead socket.
RESTART_AFTER_SEC = 90.0
SPAWN_GRACE_SEC = 75.0
MINT_RETRY_SEC = 300.0


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def in_session_window(now: datetime | None = None) -> bool:
    moment = now or now_ist()
    return moment.weekday() < 5 and WINDOW_START <= moment.time() <= WINDOW_END


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _age_seconds(path: Path, *, epoch_key: str = "updated_epoch") -> float | None:
    payload = _read_json(path)
    epoch = payload.get(epoch_key) if payload else None
    try:
        stamp = float(epoch)
        if stamp > 0:
            return max(0.0, time.time() - stamp)
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with SUPERVISOR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_ist().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-not-found]

        return bool(psutil.pid_exists(pid) and psutil.Process(pid).is_running())
    except Exception:  # noqa: BLE001 - psutil is optional
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _session_process() -> Any | None:
    """Find the live session by command line; PID alone is not enough.

    The manual session started before this supervisor existed has no PID file.
    psutil lets the supervisor recognise it without spawning a duplicate.  On a
    machine without psutil, the PID file is still used as a safe fallback.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = str(proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or [])
            except (psutil.Error, TypeError):
                continue
            # A shell/terminal whose command text mentions the script is not a
            # live session.  Require an actual Python interpreter process.
            if "python" not in name:
                continue
            if "upstox_live_session.py" in cmdline and proc.pid != os.getpid():
                return proc
    except Exception:  # noqa: BLE001 - psutil is optional
        pass
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError, TypeError):
        return None
    return pid if _pid_alive(pid) else None


def _process_age(process: Any | None) -> float | None:
    if process is None or isinstance(process, int):
        return None
    try:
        return max(0.0, time.time() - float(process.create_time()))
    except (AttributeError, TypeError, ValueError, OSError):
        return None


def _session_pid(process: Any | None) -> int | None:
    if process is None:
        return None
    if isinstance(process, int):
        return process
    try:
        return int(process.pid)
    except (AttributeError, TypeError, ValueError):
        return None


def _token_ok_today() -> bool:
    meta = _read_json(TOKEN_FILE)
    if not meta:
        return False
    saved = str(meta.get("saved_at") or "")
    try:
        stamp = datetime.fromisoformat(saved.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=IST)
        if stamp.astimezone(IST).date() != now_ist().date():
            return False
    except (TypeError, ValueError):
        return False
    return bool(meta.get("verified")) and int(meta.get("verify_status") or 0) == 200


def _write_pid(pid: int) -> None:
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass


def _terminate(process: Any, reason: str) -> None:
    pid = _session_pid(process)
    if not pid:
        return
    _log(f"terminating stale live session pid={pid} reason={reason}")
    try:
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        if int(PID_FILE.read_text(encoding="utf-8").strip()) == pid:
            PID_FILE.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


class AutoIgnitionSupervisor:
    """Background self-ignition loop owned by the dashboard process."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._mint_lock = threading.Lock()
        self._last_check = 0.0
        self._last_action = "not started"
        self._last_error = ""
        self._last_request = 0.0
        self._spawn_count = 0
        self._mint_count = 0
        self._next_mint_at = 0.0
        self._next_spawn_at = 0.0

    def start(self) -> threading.Thread:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="kronos-auto-ignition"
            )
            self._thread.start()
            self._last_action = "supervisor started"
            _log("supervisor started")
            return self._thread

    def note_request(self) -> None:
        self._last_request = time.time()
        if os.environ.get("KRONOS_DISABLE_AUTO_IGNITION", "").lower() not in {"1", "true", "yes"}:
            self.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_SECONDS):
            try:
                self._check_once()
            except Exception as exc:  # noqa: BLE001 - never kill the dashboard
                self._last_error = f"{type(exc).__name__}: {exc}"
                _log(f"check failed: {self._last_error}")

    def _mint_token(self) -> None:
        with self._mint_lock:
            if time.time() < self._next_mint_at:
                return
            if not MINT_SCRIPT.is_file():
                self._last_error = "token mint script missing"
                return
            self._last_action = "minting Upstox token"
            _log("today's token invalid/missing; running headless TOTP login")
            try:
                completed = subprocess.run(
                    [sys.executable, "-u", str(MINT_SCRIPT), "--require"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=240,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
                output = (completed.stdout or "") + (completed.stderr or "")
                _log(f"token mint exit={completed.returncode} tail={output[-500:]!r}")
                self._mint_count += 1
                if completed.returncode == 0 and _token_ok_today():
                    self._last_action = "token minted and verified"
                    self._last_error = ""
                else:
                    self._last_error = "token mint did not verify today"
                    self._next_mint_at = time.time() + MINT_RETRY_SEC
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"token mint failed: {type(exc).__name__}: {exc}"
                self._next_mint_at = time.time() + MINT_RETRY_SEC
                _log(self._last_error)

    def _start_session(self, feed_age: float | None) -> None:
        if time.time() < self._next_spawn_at:
            return
        if not SESSION_SCRIPT.is_file():
            self._last_error = "live session script missing"
            return
        moment = now_ist()
        end = moment.replace(hour=15, minute=35, second=0, microsecond=0)
        remaining = max(1, int((end - moment).total_seconds() // 60))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        session_log = LOG_DIR / f"live_session_auto_{moment:%Y%m%d}.log"
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "KRONOS_AUTO_IGNITED": "1",
        }
        kwargs: dict[str, Any] = {
            "cwd": str(REPO_ROOT),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": session_log.open("a", encoding="utf-8"),
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            # The dashboard already owns :8080 and serves /api + the public
            # tunnel.  Starting the session with --edge makes it try to bind the
            # same port (or spawn a second dashboard), so the supervisor keeps
            # the feed process alone; the dashboard remains the edge.
            process = subprocess.Popen(
                [sys.executable, "-u", str(SESSION_SCRIPT), "--minutes", str(remaining)],
                **kwargs,
            )
            _write_pid(process.pid)
            self._spawn_count += 1
            self._last_action = f"spawned live session pid={process.pid} minutes={remaining}"
            self._last_error = ""
            _log(self._last_action + f" previous_feed_age={feed_age}")
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"session spawn failed: {type(exc).__name__}: {exc}"
            self._next_spawn_at = time.time() + 30.0
            _log(self._last_error)

    def _check_once(self) -> None:
        self._last_check = time.time()
        moment = now_ist()
        if not in_session_window(moment):
            self._last_action = "outside 08:25-15:30 IST window"
            return
        feed_age = _age_seconds(FEED_FILE)
        fast_age = _age_seconds(FAST_FILE, epoch_key="epoch")
        token_ok = _token_ok_today()
        process = _session_process()
        pid = _session_pid(process)
        if pid:
            _write_pid(pid)

        if not token_ok and (feed_age is None or feed_age > FEED_STALE_SEC):
            self._mint_token()

        if process is not None:
            age = _process_age(process)
            if feed_age is not None and feed_age > RESTART_AFTER_SEC:
                if age is not None and age < SPAWN_GRACE_SEC:
                    self._last_action = (
                        f"warming pid={pid} age={age:.0f}s feed_age={feed_age:.1f}s"
                    )
                    return
                self._last_action = f"feed stale {feed_age:.1f}s; restarting session"
                _terminate(process, f"feed_age={feed_age:.1f}s age={age if age is not None else '—'}s")
                self._next_spawn_at = time.time() + 2.0
                return
            self._last_action = (
                f"healthy pid={pid} feed_age={feed_age if feed_age is not None else '—'}s"
            )
            return

        if feed_age is not None and feed_age <= FEED_STALE_SEC:
            self._last_action = f"feed fresh without PID (pid file) age={feed_age:.1f}s"
            return
        self._start_session(feed_age)

    def status(self) -> dict[str, Any]:
        process = _session_process()
        pid = _session_pid(process)
        return {
            "enabled": os.environ.get("KRONOS_DISABLE_AUTO_IGNITION", "").lower()
            not in {"1", "true", "yes"},
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "window_open": in_session_window(),
            "now_ist": now_ist().isoformat(timespec="seconds"),
            "feed_age_sec": round(_age_seconds(FEED_FILE) or 0.0, 2)
            if _age_seconds(FEED_FILE) is not None else None,
            "fast_age_sec": round(_age_seconds(FAST_FILE, epoch_key="epoch") or 0.0, 2)
            if _age_seconds(FAST_FILE, epoch_key="epoch") is not None else None,
            "token_ok_today": _token_ok_today(),
            "session_pid": pid,
            "last_check_epoch": self._last_check,
            "last_request_epoch": self._last_request,
            "last_action": self._last_action,
            "last_error": self._last_error,
            "spawn_count": self._spawn_count,
            "mint_count": self._mint_count,
            "pid_file": str(PID_FILE),
        }


AUTO_IGNITION = AutoIgnitionSupervisor()


def start_auto_ignition() -> AutoIgnitionSupervisor:
    AUTO_IGNITION.start()
    return AUTO_IGNITION


def note_dashboard_request() -> None:
    AUTO_IGNITION.note_request()


def status() -> dict[str, Any]:
    return AUTO_IGNITION.status()


__all__ = [
    "AUTO_IGNITION",
    "AutoIgnitionSupervisor",
    "in_session_window",
    "note_dashboard_request",
    "start_auto_ignition",
    "status",
]
