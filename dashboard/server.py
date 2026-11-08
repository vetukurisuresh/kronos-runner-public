#!/usr/bin/env python3
"""Zero-dependency KRONOS broker terminal on http://localhost:8080.

Serves the dark-theme dashboard plus three tiny JSON endpoints:

* ``GET  /api/state``            — the latest cloud forecast artifact
* ``POST /api/run``              — dispatch the 16 GB runner workflow
* ``GET  /api/run_status?run_id=``— live run status
* ``POST /api/pull?run_id=``     — download the artifact into state/

Standard library only (plus ``requests`` for the GitHub calls); no torch, no
model weights, nothing heavy on the laptop.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import threading
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"
API = "https://api.github.com"
WORKFLOW_FILE = "cloud-nifty-forecast.yml"
ARTIFACT_NAME = "nifty-kronos-state"


def read_env(key: str) -> str:
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class GitHub:
    """Minimal REST wrapper; the token never leaves the server process."""

    def __init__(self) -> None:
        self.owner = read_env("GITHUB_USERNAME")
        self.repo = os.environ.get("KRONOS_RUNNER_REPO", "kronos-runner-public")

    def _session(self):
        import requests

        token = read_env("SECRET_PAT")
        if not token:
            raise RuntimeError("SECRET_PAT missing from environment and .env")
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "kronos-dashboard",
            }
        )
        return session

    def dispatch(self, samples: int, replay_evals: int) -> dict:
        session = self._session()
        response = session.post(
            f"{API}/repos/{self.owner}/{self.repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            json={
                "ref": "main",
                "inputs": {
                    "samples": str(samples),
                    "replay_evals": str(replay_evals),
                    "live_loop": "false",
                    "interval_sec": "300",
                },
            },
            timeout=30,
        )
        if response.status_code not in (201, 204):
            raise RuntimeError(f"dispatch HTTP {response.status_code}: {response.text[:200]}")
        return {"dispatched": True}

    def status(self, run_id: str) -> dict:
        session = self._session()
        response = session.get(f"{API}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}", timeout=30)
        response.raise_for_status()
        run = response.json()
        return {"status": run["status"], "conclusion": run.get("conclusion"),
                "url": run["html_url"], "run_id": run["id"], "created_at": run["created_at"]}

    def latest(self, since: str = "") -> dict:
        """Newest dispatch for this workflow, optionally created after ``since``."""
        session = self._session()
        response = session.get(
            f"{API}/repos/{self.owner}/{self.repo}/actions/runs",
            params={"event": "workflow_dispatch", "per_page": 15},
            timeout=30,
        )
        response.raise_for_status()
        runs = [r for r in response.json().get("workflow_runs", [])
                if r.get("path", "").endswith(WORKFLOW_FILE)]
        if since:
            runs = [r for r in runs if r.get("created_at", "") >= since]
        if not runs:
            return {"status": None, "conclusion": None, "url": None, "run_id": None}
        run = runs[0]
        return {"status": run["status"], "conclusion": run.get("conclusion"),
                "url": run["html_url"], "run_id": run["id"], "created_at": run["created_at"]}

    def pull(self, run_id: str) -> dict:
        session = self._session()
        listing = session.get(
            f"{API}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/artifacts", timeout=30
        )
        listing.raise_for_status()
        artifacts = listing.json().get("artifacts", [])
        match = next((item for item in artifacts if item["name"] == ARTIFACT_NAME), None)
        if not match:
            raise RuntimeError(f"artifact {ARTIFACT_NAME} missing (found {[a['name'] for a in artifacts]})")
        download = session.get(match["archive_download_url"], allow_redirects=True, timeout=180)
        download.raise_for_status()
        archive = zipfile.ZipFile(io.BytesIO(download.content))
        name = next((n for n in archive.namelist() if n.endswith("nifty_kronos_state.json")), None)
        if not name:
            raise RuntimeError("artifact did not contain nifty_kronos_state.json")
        payload = archive.read(name)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_bytes(payload)
        return {"bytes": len(payload), "saved": str(STATE_FILE)}


GITHUB = GitHub()
LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (args[0] if args else ""):
            print(f"  api {args[0]} -> {args[1]}")

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            if not STATE_FILE.is_file():
                self._json({"ok": False, "reason": "no forecast artifact yet"}, status=404)
                return
            try:
                payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except ValueError as exc:
                self._json({"ok": False, "reason": f"corrupt state: {exc}"}, status=500)
                return
            self._json({"ok": True, "state": payload,
                        "mtime": datetime.fromtimestamp(STATE_FILE.stat().st_mtime).isoformat()})
            return
        if parsed.path == "/api/run_status":
            query = parse_qs(parsed.query)
            run_id = (query.get("run_id") or [""])[0]
            since = (query.get("since") or [""])[0]
            if not run_id:
                self._json({"ok": False, "reason": "run_id required"}, status=400)
                return
            try:
                payload = GITHUB.latest(since) if run_id == "latest" else GITHUB.status(run_id)
                self._json({"ok": True, **payload})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, status=502)
            return
        super().do_GET()

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if parsed.path == "/api/run":
            if not LOCK.acquire(blocking=False):
                self._json({"ok": False, "reason": "a dispatch is already in flight"}, status=429)
                return
            try:
                GITHUB.dispatch(
                    int(body.get("samples", 32)),
                    int(body.get("replay_evals", 12)),
                )
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, status=502)
            finally:
                LOCK.release()
            return
        if parsed.path == "/api/pull":
            run_id = (parse_qs(parsed.query).get("run_id") or [body.get("run_id", "")])[0]
            try:
                self._json({"ok": True, **GITHUB.pull(str(run_id))})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, status=502)
            return
        self._json({"ok": False, "reason": "unknown endpoint"}, status=404)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRONOS broker terminal")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"KRONOS terminal  ->  http://localhost:{args.port}")
    print(f"serving           ->  {STATIC}")
    print(f"state file        ->  {STATE_FILE} ({'present' if STATE_FILE.is_file() else 'missing'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
