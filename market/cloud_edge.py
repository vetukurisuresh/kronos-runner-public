#!/usr/bin/env python3
"""Cloud-edge bridge: the GitHub runner hosts the SSE server and a public tunnel.

A laptop sleeps; the 16 GB GitHub runner does not (during market hours). When the
live session runs with ``--edge`` it starts two children in the same workspace:

* ``dashboard/server.py`` on 127.0.0.1:8080 — the same ``/api/tick-stream`` SSE
  API the local terminal uses, reading ``state/live_feed.json``
* ``cloudflared tunnel --url http://127.0.0.1:8080`` — an ephemeral
  ``https://<random>.trycloudflare.com`` URL, no Cloudflare account needed

The public URL is written to Firebase ``/system/live_stream_url.json`` (mirrored
to ``/kronos_state/live_stream_url.json``, which is already world-readable). The
static GitHub Pages terminal reads that record once at boot and connects its
EventSource straight to the runner, falling back to the 800 ms Firebase poll
whenever the tunnel is stale or down.

Pure helpers (URL parsing, freshness, binary naming) sit at the top so tests can
exercise them without a network or a subprocess.
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "state" / "bin"
LOG_DIR = REPO_ROOT / "state" / "logs"
SERVER_LOG = LOG_DIR / "edge_server.log"
TUNNEL_LOG = LOG_DIR / "edge_tunnel.log"

# The spec path plus a mirror under the already-public /kronos_state tree.
RECORD_PATHS = ("/system/live_stream_url.json", "/kronos_state/live_stream_url.json")
RECORD_TTL = 900.0          # the session heartbeats the record every 45 s
HEARTBEAT_SECONDS = 45.0
# A dropped cloudflared/python child must be back within one second, never
# leaving a market-hours gap until the next scheduled shift.
RESTART_BACKOFF = 1.0
PUSH_TOKEN_ENV = "KRONOS_EDGE_PUSH_TOKEN"

TUNNEL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", re.IGNORECASE)
DOWNLOAD = {
    "linux": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    "win32": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
}


def parse_tunnel_url(text: str) -> str:
    """Extract the quick-tunnel URL from a cloudflared log line/banner."""
    match = TUNNEL_RE.search(text or "")
    return match.group(0).rstrip("/") if match else ""


def record_is_fresh(record: object, now: float | None = None, max_age: float = RECORD_TTL) -> bool:
    """True when the discovery record points at a live, recent tunnel."""
    if not isinstance(record, dict) or record.get("active") is False:
        return False
    url = str(record.get("url") or "").strip().rstrip("/")
    if not TUNNEL_RE.fullmatch(url):
        return False
    try:
        ts = float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return 0.0 < now - ts <= max_age


def binary_name(platform: str | None = None) -> str:
    platform = (platform or sys.platform).lower()
    return "cloudflared.exe" if platform.startswith("win") else "cloudflared"


def download_url(platform: str | None = None) -> str:
    platform = (platform or sys.platform).lower()
    if platform.startswith("win"):
        return DOWNLOAD["win32"]
    if platform.startswith("linux"):
        return DOWNLOAD["linux"]
    return ""


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


class CloudEdge:
    """Owns the runner-hosted SSE server, the tunnel and the Firebase record."""

    def __init__(self, publisher, port: int = 8080, host: str = "127.0.0.1",
                 binary: str | None = None) -> None:
        self.publisher = publisher
        self.port = port
        self.host = host
        self.binary = binary
        self.server: subprocess.Popen | None = None
        self.tunnel: subprocess.Popen | None = None
        self.url = ""
        self.verified = False
        self.error = ""
        self.last_publish = 0.0
        self.last_restart = 0.0
        self._url_event = threading.Event()
        self._stopped = False
        self._server_log = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> dict:
        """Start the local server, then best-effort bring up the tunnel."""
        # This must exist before the server child is spawned, because the child
        # inherits the environment at Popen time.
        if not os.environ.get(PUSH_TOKEN_ENV):
            os.environ[PUSH_TOKEN_ENV] = secrets.token_urlsafe(32)
        ok = self._start_server()
        if not ok:
            return {"ok": False, "reason": self.error}
        atexit.register(self.stop)
        self._start_tunnel()
        self._publish()
        return {"ok": True, "port": self.port,
                "server_pid": self.server.pid if self.server is not None else None,
                "url": self.url or None, "error": self.error or None}

    def tick(self) -> None:
        """Cheap supervisor call from the session loop (runs every second)."""
        if self._stopped:
            return
        now = time.time()
        if self.server and self.server.poll() is not None:
            if now - self.last_restart >= RESTART_BACKOFF:
                self.last_restart = now
                self.error = ""
                self._start_server()
            return
        if self.tunnel is not None and self.tunnel.poll() is not None:
            # The tunnel died: stop advertising its URL immediately so the phone
            # falls back to Firebase instead of waiting for the 15 min TTL.
            if self.url:
                self.url = ""
                self._publish(active=False)
            if now - self.last_restart >= RESTART_BACKOFF:
                self.last_restart = now
                self._start_tunnel()
            return
        if not self.url:
            if now - self.last_restart >= RESTART_BACKOFF:
                self.last_restart = now
                self._start_tunnel()
            return
        if now - self.last_publish >= HEARTBEAT_SECONDS:
            self._publish()

    def notify_tick(self, timeout: float = 0.25) -> bool:
        """Wake the SSE hub the instant a fresh live_feed frame is on disk."""
        token = os.environ.get(PUSH_TOKEN_ENV, "")
        if not token:
            return False
        try:
            response = requests.post(
                f"http://{self.host}:{self.port}/api/internal/tick",
                headers={"X-Kronos-Edge-Token": token},
                timeout=timeout,
            )
            return bool(response.ok)
        except Exception:  # noqa: BLE001 - the file watcher is the fallback
            return False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            if self.url:
                self._publish(active=False)
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass
        for proc in (self.tunnel, self.server):
            if not proc or proc.poll() is not None:
                continue
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._server_log:
                self._server_log.close()
        except Exception:  # noqa: BLE001
            pass

    # -- server ------------------------------------------------------------
    def _start_server(self) -> bool:
        if self.server and self.server.poll() is None:
            return True
        if port_open(self.host, self.port):
            if self._server_alive():
                # The session may already host the dashboard; reuse it and add
                # only the tunnel instead of spawning a duplicate predictor.
                return True
            self.error = f"port {self.port} is held by another process"
            return False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._server_log = SERVER_LOG.open("ab")
        self.server = subprocess.Popen(
            [sys.executable, "-u", "dashboard/server.py",
             "--port", str(self.port), "--host", self.host],
            cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
            stdout=self._server_log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.server.poll() is not None:
                self.error = f"dashboard server exited ({self.server.returncode})"
                return False
            if port_open(self.host, self.port):
                return True
            time.sleep(0.3)
        self.error = "dashboard server did not bind in time"
        return False

    def _server_alive(self, timeout: float = 3.0) -> bool:
        try:
            response = requests.get(f"http://{self.host}:{self.port}/api/ping", timeout=timeout)
            return bool(response.ok and (response.json() or {}).get("ok"))
        except Exception:  # noqa: BLE001
            return False

    # -- tunnel ------------------------------------------------------------
    def _binary_path(self) -> Path:
        if self.binary:
            return Path(self.binary)
        return BIN_DIR / binary_name()

    def _ensure_binary(self) -> str:
        path = self._binary_path()
        if path.is_file():
            return str(path)
        # The workflow installs cloudflared on PATH explicitly (visible logs,
        # its own retries); the download below is only a fallback.
        found = shutil.which(binary_name())
        if found:
            return found
        url = download_url()
        if not url:
            raise RuntimeError(f"no cloudflared binary for {sys.platform}")
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        last: Exception | None = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "kronos-edge"})
                with urllib.request.urlopen(request, timeout=180) as source, partial.open("wb") as target:
                    shutil.copyfileobj(source, target)
                partial.chmod(0o755)
                partial.replace(path)
                return str(path)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2 + attempt * 4)
        raise RuntimeError(f"cloudflared download failed: {last}")

    def _start_tunnel(self) -> bool:
        if self.tunnel and self.tunnel.poll() is None:
            if self.url:
                return True
            # A cloudflared that never printed a URL would otherwise accumulate.
            try:
                self.tunnel.terminate()
                self.tunnel.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        try:
            binary = self._ensure_binary()
        except Exception as exc:  # noqa: BLE001 - edge is best-effort
            self.error = f"cloudflared unavailable: {type(exc).__name__}: {exc}"
            return False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._url_event.clear()
        self.url = ""
        self.verified = False
        log = TUNNEL_LOG.open("ab")
        try:
            self.tunnel = subprocess.Popen(
                [binary, "tunnel", "--no-autoupdate", "--url", f"http://{self.host}:{self.port}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"cloudflared failed to start: {type(exc).__name__}"
            log.close()
            return False
        threading.Thread(target=self._read_tunnel, args=(self.tunnel, log), daemon=True).start()
        if not self._url_event.wait(50):
            self.error = "cloudflared produced no URL in 50s"
            return False
        # DNS/edge propagation can outlive the session's startup budget.  Keep
        # the real URL and verify it in the background; readiness stays red
        # until /api/ping actually answers.
        threading.Thread(target=self._verify_until_live, args=(self.url,),
                         daemon=True, name="kronos-edge-verify").start()
        return True

    def _verify_until_live(self, url: str) -> None:
        deadline = time.time() + 300.0
        while not self._stopped and time.time() < deadline:
            if self._verify(url):
                self.verified = True
                self.error = ""
                return
            time.sleep(3.0)

    def _read_tunnel(self, proc: subprocess.Popen, log) -> None:
        try:
            for line in proc.stdout or []:
                try:
                    log.write(line)
                    log.flush()
                except Exception:  # noqa: BLE001
                    pass
                url = parse_tunnel_url(line)
                if url and not self.url:
                    self.url = url
                    self._url_event.set()
        finally:
            try:
                log.close()
            except Exception:  # noqa: BLE001
                pass

    def _verify(self, url: str, timeout: float = 8.0) -> bool:
        if not url:
            return False
        try:
            response = requests.get(f"{url}/api/ping", timeout=timeout)
            return bool(response.ok and (response.json() or {}).get("ok"))
        except Exception:  # noqa: BLE001
            return False

    # -- discovery record --------------------------------------------------
    def _publish(self, active: bool | None = None) -> dict:
        from scripts.publish_edge_url import edge_payload, publish_edge_record

        active = bool(self.url and self.verified) if active is None else bool(active and self.url)
        body = edge_payload(
            self.url, active=active, port=self.port, host=socket.gethostname(),
        )
        status = publish_edge_record(body, self.publisher)
        self.last_publish = time.time()
        return status
