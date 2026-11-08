#!/usr/bin/env python3
"""Zero-dependency KRONOS broker terminal on http://localhost:8080.

Serves the dark-theme dashboard plus three tiny JSON endpoints:

* ``GET  /api/state``            — the latest cloud forecast artifact
* ``GET  /api/live``             — telemetry ribbon + money-manager HUD
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
# Running ``python dashboard/server.py`` puts dashboard/ on sys.path, not the
# repo root; the health/ledger handlers import market.* and state.*.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
STATIC = Path(__file__).resolve().parent / "static"
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"
LIVE_FILE = REPO_ROOT / "state" / "live_telemetry.json"
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
        if parsed.path == "/api/upstox/login":
            try:
                from market.upstox_health import authorize_url

                target = authorize_url(read_env("UPSTOX_API_KEY"), read_env("UPSTOX_REDIRECT_URI"))
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, status=500)
            return
        if parsed.path == "/upstox/callback":
            code = (parse_qs(parsed.query).get("code") or [""])[0]
            try:
                from market.upstox_health import exchange_code, save_access_token, verify_token

                result = exchange_code(
                    code,
                    read_env("UPSTOX_API_KEY"),
                    read_env("UPSTOX_API_SECRET"),
                    read_env("UPSTOX_REDIRECT_URI"),
                )
                if not result.get("ok"):
                    raise RuntimeError(f"token exchange failed ({result.get('status')})")
                token = result["access_token"]
                saved = save_access_token(token, extra={"user": result.get("user")})
                probe = verify_token(token)
                self._html(
                    "<h2>Upstox connected</h2>"
                    f"<p>Token saved ({saved['length']} chars) - live API: "
                    f"{'OK' if probe.get('ok') else 'not verified'}</p>"
                    "<p>You can close this tab.</p><script>setTimeout(()=>window.close(),2500)</script>"
                )
            except Exception as exc:  # noqa: BLE001
                self._html(f"<h2>Upstox connect failed</h2><p>{exc}</p>", status=502)
            return
        if parsed.path == "/api/health":
            self._json(self._health())
            return
        if parsed.path == "/api/ledger":
            from state.paper_trade_ledger import PaperLedger

            ledger = PaperLedger.load()
            payload: dict = {"ok": True, **ledger.snapshot()}
            logs_path = REPO_ROOT / "state" / "paper_week_logs.json"
            if logs_path.is_file():
                try:
                    payload["logs"] = json.loads(logs_path.read_text(encoding="utf-8"))
                except ValueError:
                    payload["logs"] = []
            self._json(payload)
            return
        if parsed.path == "/api/live":
            payload: dict = {"ok": True, "telemetry": [], "money": {}, "traffic": None}
            if LIVE_FILE.is_file():
                try:
                    payload.update(json.loads(LIVE_FILE.read_text(encoding="utf-8")))
                except ValueError:
                    payload["reason"] = "live telemetry is corrupt"
            payload.update(self._derived_ribbon())
            self._json(payload)
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
    def _html(self, body: str, status: int = 200) -> None:
        page = (
            "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='background:#0b1017;color:#dbe6f3;font:16px system-ui;padding:32px;"
            "max-width:640px;margin:auto'>" + body + "</body>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    @staticmethod
    def _health() -> dict:
        from market.upstox_health import HealthInputs, five_checks, load_access_token
        from state.paper_trade_ledger import PaperLedger

        token = load_access_token()
        inputs = HealthInputs(token_present=bool(token), token_valid=bool(token))
        inputs.capital = PaperLedger.load().capital
        if STATE_FILE.is_file():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except ValueError:
                state = {}
            symbols = state.get("symbols") or {}
            inputs.buffer_5m = {name: len(block.get("candles") or [])
                                for name, block in symbols.items()}
            inputs.option_chain_ready = any(
                (block.get("plan") or {}).get("strike") for block in symbols.values()
            )
            inputs.option_strikes = [
                str((block.get("plan") or {}).get("strike"))
                for block in symbols.values()
                if (block.get("plan") or {}).get("strike")
            ]
            inputs.model_online = bool(symbols)
        return {"ok": True, **five_checks(inputs)}

    @staticmethod
    def _derived_ribbon() -> dict:
        """Fallback ribbon built from the newest cloud artifact."""
        if not STATE_FILE.is_file():
            return {"traffic": None}
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            return {"traffic": None}
        rows = []
        for name, block in (state.get("symbols") or {}).items():
            forecast = block.get("forecast") or {}
            rows.append(
                {
                    "key": name,
                    "label": block.get("display", name),
                    "kind": "index",
                    "ltp": block.get("spot"),
                    "atp": (block.get("vwap") or [0])[-1],
                    "oi_change_pct": 0.0,
                    "delta": 0.0,
                    "bid": 0.0,
                    "ask": 0.0,
                    "paths_up": forecast.get("paths_up"),
                    "samples": forecast.get("samples"),
                    "regime": forecast.get("regime"),
                }
            )
        traffic = None
        first = next(iter((state.get("symbols") or {}).values()), None)
        if first:
            decision = first.get("decision") or {}
            traffic = {
                "light": "RED" if (decision.get("action") == "NO_TRADE") else "GREEN",
                "action": decision.get("action"),
                "confidence": decision.get("confidence"),
            }
        return {"telemetry": rows, "traffic": traffic}

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
        if parsed.path == "/api/ledger/reset":
            from state.paper_trade_ledger import PaperLedger

            ledger = PaperLedger().reset()
            self._json({"ok": True, **ledger.snapshot()})
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
