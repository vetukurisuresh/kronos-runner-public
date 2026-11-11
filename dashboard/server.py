#!/usr/bin/env python3
"""Zero-dependency KRONOS broker terminal on http://localhost:8080.

Serves the dark-theme dashboard plus three tiny JSON endpoints:

* ``GET  /api/state``            — the latest cloud forecast artifact
* ``GET  /api/live``             — telemetry ribbon + money-manager HUD
* ``GET  /api/command-center/live`` — unified sync/accuracy/log/footprint read model
* ``GET  /api/command-center/history`` — date-keyed archive
* ``GET  /api/command-center/download-logs`` — full-day text/JSONL/CSV download
* ``POST /api/run``              — dispatch the 16 GB runner workflow
* ``GET  /api/run_status?run_id=``— live run status
* ``POST /api/pull?run_id=``     — download the artifact into state/

Standard library only (plus ``requests`` for the GitHub calls); no torch, no
model weights, nothing heavy on the laptop.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import io
import json
import os
import select
import socket
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

# SSE handlers are long-lived by design. They must never keep an otherwise
# finished server process (or a test run) alive during shutdown.
ThreadingHTTPServer.daemon_threads = True

REPO_ROOT = Path(__file__).resolve().parents[1]
# Running ``python dashboard/server.py`` puts dashboard/ on sys.path, not the
# repo root; the health/ledger handlers import market.* and state.*.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from dashboard.command_center import CommandCenterService  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
STATE_FILE = REPO_ROOT / "state" / "latest_cloud_state.json"
LIVE_FILE = REPO_ROOT / "state" / "live_telemetry.json"
EXPIRY_HERO_MODE_FILE = REPO_ROOT / "state" / "expiry_hero_mode.json"
SCALPER_MODE_FILE = REPO_ROOT / "state" / "scalper_mode.json"
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
COMMAND_CENTER = CommandCenterService()

PUSH_TOKEN_ENV = "KRONOS_EDGE_PUSH_TOKEN"
STREAM_SYMBOLS = ("NIFTY", "BANKNIFTY")


class TickHub:
    """Event-driven, in-memory fan-out for the two index SSE streams.

    The session POSTs an internal notification immediately after writing the
    authoritative feed. The condition wakes every SSE client at once, instead
    of each client re-reading the feed on a 250 ms timer. A 20 ms file watcher
    remains as a fail-safe for a separately-launched dashboard.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._version = 0
        self._frames: dict[str, tuple[int, bytes, float]] = {}
        self._watch_started = False
        self._watch_mtime = 0

    def publish(self, symbol: str, payload: dict, dispatch_ms: float | None = None) -> None:
        body_payload = dict(payload)
        if dispatch_ms is not None:
            body_payload["edge_pipe"] = {
                "transport": "github-runner",
                "dispatch_ms": round(max(0.0, float(dispatch_ms)), 3),
                "published_epoch": round(time.time(), 3),
            }
        body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._condition:
            self._version += 1
            self._frames[symbol] = (self._version, body, time.time())
            self._condition.notify_all()

    def publish_snapshot(self, symbol: str, dispatch_ms: float | None = None) -> None:
        from market.live_ticks import snapshot

        self.publish(symbol, snapshot(symbol), dispatch_ms=dispatch_ms)

    def publish_all(self, dispatch_ms: float | None = None) -> None:
        started = time.perf_counter()
        from market.live_ticks import snapshot

        payloads = {symbol: snapshot(symbol) for symbol in STREAM_SYMBOLS}
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        measured = elapsed_ms if dispatch_ms is None else dispatch_ms
        for symbol, payload in payloads.items():
            self.publish(symbol, payload, dispatch_ms=measured)

    def frame(self, symbol: str) -> tuple[int, bytes, float] | None:
        with self._condition:
            return self._frames.get(symbol)

    def wait(self, symbol: str, seen_version: int, timeout: float = 1.0) -> tuple[int, bytes, float] | None:
        with self._condition:
            if seen_version == self._version:
                self._condition.wait(timeout)
            return self._frames.get(symbol)

    def start_watcher(self) -> None:
        with self._condition:
            if self._watch_started:
                return
            self._watch_started = True
        threading.Thread(target=self._watch_feed, daemon=True, name="kronos-tick-hub").start()

    def _watch_feed(self) -> None:
        while True:
            try:
                mtime = LIVE_FILE.stat().st_mtime_ns if LIVE_FILE.is_file() else 0
            except OSError:
                mtime = 0
            if mtime and mtime != self._watch_mtime:
                self._watch_mtime = mtime
                try:
                    self.publish_all()
                except Exception:  # noqa: BLE001 - watcher must never die
                    pass
            time.sleep(0.02)


TICK_HUB = TickHub()


class Handler(SimpleHTTPRequestHandler):
    # SSE needs a persistent HTTP/1.1 connection with chunked framing; the
    # stdlib default (HTTP/1.0) makes browsers abort the stream.
    protocol_version = "HTTP/1.1"
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):  # quieter console
        try:
            line = fmt % args if args else fmt
        except Exception:  # noqa: BLE001 - logging must never break a request
            line = str(fmt)
        if "/api/" in line:
            print(f"  api {line}")

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.send_header("Cache-Control", "no-cache, no-store, no-transform, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        """Every API response must be readable by the GitHub Pages origin."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, Cache-Control, Last-Event-ID, X-Kronos-Edge-Token",
        )
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Kronos-Dispatch-Ms")
        self.send_header("Vary", "Origin")

    def _pdf(self, body: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._cors_headers()
        self.send_header("Cache-Control", "no-cache, no-store, no-transform, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, body: bytes, filename: str, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._cors_headers()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
        if urlparse(self.path).path.startswith("/api/"):
            self.send_response(204)
            self._cors_headers()
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json({"ok": False, "reason": "unknown endpoint"}, status=404)

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        # The page probes this artifact as a state source.  An absent artifact
        # is an empty state, not a missing file, so the browser console stays
        # clean instead of logging a 404 on every load.
        if parsed.path in ("/latest_cloud_state.json", "/state/latest_cloud_state.json"):
            try:
                payload = (json.loads(STATE_FILE.read_text(encoding="utf-8"))
                           if STATE_FILE.is_file() else {})
            except ValueError:
                payload = {}
            self._json(payload)
            return
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
        if parsed.path == "/api/ping":
            # Public liveness probe for the Cloudflare tunnel URL.
            self._json({"ok": True, "server": "kronos-edge", "ts": round(time.time(), 3)})
            return
        if parsed.path == "/api/live-ticks":
            from market.live_ticks import snapshot

            symbol = (parse_qs(parsed.query).get("symbol") or ["NIFTY"])[0].upper()
            self._json(snapshot(symbol))
            return
        if parsed.path == "/api/full-spectrum":
            from market.live_ticks import snapshot

            symbol = (parse_qs(parsed.query).get("symbol") or ["NIFTY"])[0].upper()
            payload = snapshot(symbol)
            self._json({
                "ok": payload.get("ok", False), "symbol": symbol,
                "full_spectrum": (payload.get("full_spectrum") or {}).get(symbol),
                "coverage": ((payload.get("full_spectrum") or {}).get(symbol) or {}).get("coverage"),
                "kronos_inputs": (payload.get("kronos_inputs") or {}).get(symbol),
            })
            return
        if parsed.path in {"/api/autopilot/health", "/api/readiness"}:
            from market.autopilot_readiness import build_from_runtime, load_readiness
            from market.upstox_health import load_access_token, load_token_meta

            feed_path = REPO_ROOT / "state" / "live_feed.json"
            try:
                feed = json.loads(feed_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                feed = {}
            payload = build_from_runtime(
                feed=feed,
                readiness=load_readiness(),
                token_present=bool(load_access_token()),
                token_meta=load_token_meta(),
            )
            self._json({"ok": True, **payload})
            return
        if parsed.path == "/api/tick-stream":
            symbol = (parse_qs(parsed.query).get("symbol") or ["NIFTY"])[0].upper()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, no-transform, must-revalidate")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self._cors_headers()
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Encoding", "identity")
            self.end_headers()
            TICK_HUB.start_watcher()
            try:
                # Always build one fresh frame for a new client so a cached
                # frame from another test/session can never be replayed.
                TICK_HUB.publish_snapshot(symbol)
                frame = TICK_HUB.frame(symbol)
                if frame is None:
                    raise RuntimeError("tick hub has no frame")
                seen_version, body, _ = frame
                self._sse_write(body)
                while True:
                    # Break promptly when a browser/tab closes the stream; a
                    # non-daemon request thread would otherwise delay shutdown.
                    try:
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable and not self.connection.recv(1, socket.MSG_PEEK):
                            break
                    except OSError:
                        break
                    next_frame = TICK_HUB.wait(symbol, seen_version, timeout=0.25)
                    if next_frame is None:
                        continue
                    seen_version, body, _ = next_frame
                    self._sse_write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
                return
        if parsed.path == "/api/scalper-mode":
            from strategy.adaptive_scalper import normalize_scalper_mode

            mode = "AUTO"
            try:
                raw = json.loads(SCALPER_MODE_FILE.read_text(encoding="utf-8"))
                mode = normalize_scalper_mode(raw.get("mode"))
            except (OSError, ValueError, TypeError):
                pass
            self._json({"ok": True, "mode": mode,
                        "modes": ["AUTO", "PING_PONG", "VELOCITY", "TREND_ONLY"]})
            return
        if parsed.path == "/api/expiry-hero":
            from strategy.expiry_zero_to_hero import normalize_mode

            mode = "AUTO"
            try:
                raw = json.loads(EXPIRY_HERO_MODE_FILE.read_text(encoding="utf-8"))
                mode = normalize_mode(raw.get("mode"))
            except (OSError, ValueError, TypeError):
                pass
            self._json({"ok": True, "mode": mode, "modes": ["AUTO", "FORCE_ON", "OFF"]})
            return
        if parsed.path == "/api/health":
            self._json(self._health())
            return
        if parsed.path == "/api/trade/mode":
            from execution.order_router import OrderRouter
            from state.paper_trade_ledger import PaperLedger

            ledger = PaperLedger.load()
            hud = self._live_hud()
            payload = OrderRouter().snapshot(
                paper_capital=ledger.capital,
                paper_realized=float(hud.get("realized_today") or 0.0),
            )
            payload["live_hud"] = hud
            from execution.trading_cutoff import cutoff_state

            payload["cutoff"] = cutoff_state()
            self._json(payload)
            return
        if parsed.path == "/api/trade/margin":
            from execution.order_router import ModeRejected, OrderRouter

            router = OrderRouter()
            try:
                router.require_real()
                margin = router.broker.funds_and_margin("SEC")
                self._json({"ok": True, **margin})
            except ModeRejected as exc:
                self._json({"ok": False, "reason": str(exc)}, status=409)
            except Exception as exc:  # noqa: BLE001 - broker errors are safe strings
                self._json({"ok": False, "reason": str(exc)}, status=502)
            return
        if parsed.path == "/api/ledger":
            from state.paper_trade_ledger import PaperLedger

            ledger = PaperLedger.load()
            payload: dict = {"ok": True, **ledger.snapshot()}
            # The modal merges this with the SSE stream so another symbol's
            # still-open trade is not missing when NIFTY is not the active pane.
            feed_path = REPO_ROOT / "state" / "live_feed.json"
            if feed_path.is_file():
                try:
                    feed = json.loads(feed_path.read_text(encoding="utf-8"))
                    payload["live_trades"] = feed.get("trade") or {}
                    payload["live_hud"] = feed.get("hud") or {}
                except (OSError, ValueError):
                    payload["live_trades"] = {}
            logs_path = REPO_ROOT / "state" / "paper_week_logs.json"
            if logs_path.is_file():
                try:
                    payload["logs"] = json.loads(logs_path.read_text(encoding="utf-8"))
                except ValueError:
                    payload["logs"] = []
            self._json(payload)
            return
        if parsed.path in {"/api/journal/summary", "/api/journal/download-pdf"}:
            from reports.chart_snapshot_renderer import render_trade_chart
            from reports.pro_journal_pdf import (
                generate_journal_pdf,
                load_missed_setups,
                resolve_period,
            )
            from state.trade_journal_store import load_journal

            query = parse_qs(parsed.query)
            period = (query.get("period") or ["daily"])[0].strip().lower()
            if period not in {"daily", "weekly", "monthly", "custom"}:
                self._json({"ok": False, "reason": "period must be daily|weekly|monthly|custom"}, status=400)
                return
            start_arg = (query.get("from") or [""])[0].strip() or None
            end_arg = (query.get("to") or [""])[0].strip() or None
            if period == "custom" and not (start_arg and end_arg):
                self._json({"ok": False, "reason": "custom range needs from and to"}, status=400)
                return
            try:
                scope = resolve_period(period, start_arg, end_arg)
                if parsed.path == "/api/journal/download-pdf":
                    pdf_path = generate_journal_pdf(
                        period, start_arg, end_arg, output_dir=None,
                    )
                    self._pdf(pdf_path.read_bytes(), pdf_path.name)
                    return
                store = load_journal()
                rows = store.records_between(scope["start"], scope["end"])
                summary = store.summary(rows)
                starting = float(store.starting_capital or 100_000.0)
                net = float(summary.get("net_pnl") or 0.0)
                summary["starting_capital"] = round(starting, 2)
                summary["ending_capital"] = round(starting + net, 2)
                summary["roi_pct"] = round(net / starting * 100.0, 2) if starting else 0.0
                summary["t1_gross_profit_inr"] = round(
                    sum(float(row.get("t1_gross_profit_inr") or 0.0) for row in rows), 2
                )
                summary["t2_gross_profit_inr"] = round(
                    sum(float(row.get("t2_or_final_gross_profit_inr") or 0.0) for row in rows), 2
                )
                preview_dir = Path(
                    os.environ.get("KRONOS_JOURNAL_OUTPUT")
                    or (REPO_ROOT / "reports" / "output")
                ) / "previews"
                preview_dir.mkdir(parents=True, exist_ok=True)
                charts: dict[str, str] = {}
                for row in rows:
                    key = str(row.get("trade_id") or "")
                    if not key:
                        continue
                    try:
                        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:80]
                        chart_path = render_trade_chart(
                            row, preview_dir / f"{safe}.png", dpi=90,
                        )
                        charts[key] = (
                            "data:image/png;base64,"
                            + base64.b64encode(chart_path.read_bytes()).decode("ascii")
                        )
                    except Exception as exc:  # noqa: BLE001 - preview never blocks JSON
                        print(f"  journal preview failed for {key}: {exc}")
                self._json({
                    "ok": True,
                    **scope,
                    "range": {"from": scope["start"], "to": scope["end"], "label": scope["label"]},
                    "summary": summary,
                    "trades": rows,
                    "charts": charts,
                    "missed": load_missed_setups(scope["start"], scope["end"]),
                })
            except Exception as exc:  # noqa: BLE001 - report failure as JSON
                self._json({"ok": False, "reason": f"journal build failed: {exc}"}, status=500)
            return
        if parsed.path == "/api/model-audit":
            from model.tri_model_orchestrator import build_audit_snapshot

            feed_path = REPO_ROOT / "state" / "live_feed.json"
            try:
                feed = json.loads(feed_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                feed = {}
            self._json(build_audit_snapshot(feed))
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
        if parsed.path == "/api/command-center/live":
            query = parse_qs(parsed.query)
            symbol = (query.get("symbol") or ["NIFTY"])[0].upper()
            try:
                self._json(COMMAND_CENTER.live(symbol))
            except Exception as exc:  # noqa: BLE001 - read-model failures stay JSON
                self._json({"ok": False, "reason": f"command center failed: {exc}"}, status=500)
            return
        if parsed.path in {"/api/command-center/history", "/api/command-center/download-logs"}:
            query = parse_qs(parsed.query)
            date = (query.get("date") or [""])[0].strip()
            if not date:
                self._json({"ok": False, "reason": "date=YYYY-MM-DD required"}, status=400)
                return
            try:
                if parsed.path == "/api/command-center/download-logs":
                    scope = (query.get("scope") or ["all"])[0]
                    fmt = (query.get("format") or ["txt"])[0]
                    payload = COMMAND_CENTER.download(date, scope=scope, fmt=fmt)
                    self._download(payload["body"], payload["filename"], payload["content_type"])
                    return
                self._json(COMMAND_CENTER.history(date))
            except ValueError as exc:
                self._json({"ok": False, "reason": str(exc)}, status=400)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": f"archive read failed: {exc}"}, status=500)
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

    def _sse_write(self, payload_body: bytes) -> None:
        body = b"data: " + payload_body + b"\n\n"
        self.wfile.write(f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n")
        self.wfile.flush()

    @staticmethod
    def _live_hud() -> dict:
        """Today's live HUD for the mode endpoint; never raises."""
        try:
            feed = json.loads((REPO_ROOT / "state" / "live_feed.json").read_text(encoding="utf-8"))
            return feed.get("hud") or {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _health() -> dict:
        from market.upstox_health import (
            HealthInputs,
            five_checks,
            load_access_token,
            load_token_meta,
        )
        from state.paper_trade_ledger import PaperLedger

        token = load_access_token()
        meta = load_token_meta()
        # A token verified yesterday is not a token for today. The legacy chip
        # must fail closed exactly like the six-check API.
        saved = str(meta.get("saved_at") or "")
        valid = (
            bool(meta.get("verified")) and saved[:10] == datetime.now().date().isoformat()
        ) if token else None
        inputs = HealthInputs(token_present=bool(token), token_valid=valid)
        inputs.capital = PaperLedger.load().capital

        # Live session heartbeat (scripts/upstox_live_session.py) drives the feed chip.
        feed_path = REPO_ROOT / "state" / "live_feed.json"
        if feed_path.is_file():
            try:
                feed = json.loads(feed_path.read_text(encoding="utf-8"))
            except ValueError:
                feed = {}
            from time import time as _now

            fresh = feed and _now() - float(feed.get("updated_epoch") or 0) < 25
            if fresh:
                rate = float(feed.get("tick_rate") or 0.0)
                # A saved price with no socket traffic is not a live feed.
                inputs.feed_connected = bool(feed.get("socket_connected")) and rate > 0.2
                inputs.ticks_per_sec = rate if inputs.feed_connected else 0.0
                inputs.latency_ms = float(feed.get("socket_last_frame_age") or 0) * 1000.0
                inputs.buffer_1m = feed.get("buffer_1m") or {}
                # Never shrink a warmed buffer: the live engine starts empty while
                # the snapshot already carries 512 sealed bars.
                for name, count in (feed.get("buffer_5m") or {}).items():
                    inputs.buffer_5m[name] = max(int(count), int(inputs.buffer_5m.get(name, 0)))
        # The cloud artifact is useful context, but an old artifact must not
        # turn the health chips green. Only a recent artifact counts as a fact.
        state_fresh = False
        if STATE_FILE.is_file():
            try:
                state_fresh = time.time() - STATE_FILE.stat().st_mtime < 300
            except OSError:
                state_fresh = False
        if STATE_FILE.is_file() and state_fresh:
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
        if parsed.path == "/api/internal/tick":
            expected = os.environ.get(PUSH_TOKEN_ENV, "")
            supplied = self.headers.get("X-Kronos-Edge-Token", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                self._json({"ok": False, "reason": "edge push token required"}, status=403)
                return
            started = time.perf_counter()
            TICK_HUB.publish_all()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._json({"ok": True, "dispatch_ms": round(elapsed_ms, 3),
                        "symbols": list(STREAM_SYMBOLS)})
            return
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
        if parsed.path == "/api/scalper-mode":
            from strategy.adaptive_scalper import normalize_scalper_mode

            mode = normalize_scalper_mode(body.get("mode"))
            try:
                SCALPER_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
                SCALPER_MODE_FILE.write_text(
                    json.dumps({
                        "mode": mode,
                        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                    }, indent=1),
                    encoding="utf-8",
                )
            except OSError as exc:
                self._json({"ok": False, "reason": str(exc)}, status=500)
                return
            self._json({"ok": True, "mode": mode})
            return
        if parsed.path == "/api/expiry-hero":
            from strategy.expiry_zero_to_hero import normalize_mode

            mode = normalize_mode(body.get("mode"))
            try:
                EXPIRY_HERO_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
                EXPIRY_HERO_MODE_FILE.write_text(
                    json.dumps({
                        "mode": mode,
                        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                    }, indent=1),
                    encoding="utf-8",
                )
            except OSError as exc:
                self._json({"ok": False, "reason": str(exc)}, status=500)
                return
            self._json({"ok": True, "mode": mode})
            return
        if parsed.path == "/api/trade/mode":
            from execution.order_router import ModeRejected, OrderRouter
            from state.paper_trade_ledger import PaperLedger

            ledger = PaperLedger.load()
            hud = self._live_hud()
            router = OrderRouter()
            try:
                result = router.set_mode(
                    str(body.get("mode") or "").upper(),
                    confirm=bool(body.get("confirm") or body.get("submitted")),
                    paper_capital=ledger.capital,
                    paper_realized=float(hud.get("realized_today") or 0.0),
                )
                result["live_hud"] = hud
                self._json(result)
            except ModeRejected as exc:
                self._json({"ok": False, "reason": str(exc)}, status=409)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, status=502)
            return
        if parsed.path == "/api/trade/close-now":
            from execution.order_router import (  # noqa: F401 - guarded by mode
                MANUAL_CLOSE_REASON,
                ModeRejected,
                OrderRouter,
            )

            symbol = str(body.get("symbol") or "NIFTY").upper()
            requested_mode = str(body.get("mode") or "").upper()
            feed_path = REPO_ROOT / "state" / "live_feed.json"
            try:
                feed = json.loads(feed_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                feed = {}
            trade_block = feed.get("trade") or {}
            trade = trade_block.get(symbol) if isinstance(trade_block, dict) else None
            if not isinstance(trade, dict) or str(trade.get("status") or "").upper() != "OPEN":
                self._json({"ok": False, "reason": f"no open {symbol} position"}, status=409)
                return
            router = OrderRouter()
            try:
                state = router.load()
                mode = state["mode"]
                if requested_mode and requested_mode != mode:
                    raise ModeRejected(
                        f"UI asked for {requested_mode} close but terminal mode is {mode}"
                    )
                ltp = float((feed.get("option") or {}).get(symbol, {}).get("ltp") or 0.0)
                if ltp <= 0:
                    raise ModeRejected("live option LTP is unavailable")
                if mode == "PAPER":
                    leg = router.close_paper_at_live(trade, ltp)
                    # If the runner is live it owns the in-memory trade; ask it to
                    # close on its next cycle. A stale/standalone dashboard closes
                    # the persisted feed immediately so the one-click button is
                    # truthful even when the market session is not running.
                    fresh = bool(feed.get("socket_connected")) and (
                        time.time() - float(feed.get("updated_epoch") or 0) < 15
                    )
                    request = router.make_close_request(symbol, mode="PAPER") if fresh else None
                    if not fresh:
                        try:
                            from state.paper_trade_ledger import PaperLedger, paper_trade_from_live, today_ist

                            feed.setdefault("trade", {})[symbol] = trade
                            hud = feed.get("hud") or {}
                            realized = round(float(hud.get("realized_today") or 0.0)
                                             + float(leg.get("pnl") or 0.0), 2)
                            hud["realized_today"] = realized
                            hud["capital"] = round(float(hud.get("starting_capital") or 100000.0)
                                                   + realized, 2)
                            feed["hud"] = hud
                            feed_path.write_text(json.dumps(feed, indent=1), encoding="utf-8")
                            ledger = PaperLedger.load()
                            ledger.upsert_trade(paper_trade_from_live(
                                trade, date=today_ist(), symbol=symbol,
                            ))
                            ledger.save()
                        except (OSError, ValueError):
                            pass
                    self._json({"ok": True, **leg, "close_request": request,
                                "feed_updated": not fresh, "reason": MANUAL_CLOSE_REASON})
                    return
                result = router.close_live_at_market(trade)
                cancelled = []
                for order_id in trade.get("broker_orders") or []:
                    if str(order_id).startswith("SL-"):
                        try:
                            cancelled.append(router.broker.cancel_order(str(order_id)[3:]))
                        except Exception:  # noqa: BLE001 - exit still stands
                            pass
                self._json({"ok": True, **result, "cancelled": cancelled,
                            "reason": MANUAL_CLOSE_REASON})
            except ModeRejected as exc:
                self._json({"ok": False, "reason": str(exc)}, status=409)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, status=502)
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

    TICK_HUB.start_watcher()
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
