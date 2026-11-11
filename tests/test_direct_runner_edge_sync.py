"""Direct GitHub runner edge: CORS, event-shaped SSE, static routing, workflows."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import requests
import yaml

from scripts.publish_edge_url import edge_payload, write_local_record

ROOT = Path(__file__).resolve().parents[1]


def _serve():
    from dashboard.server import Handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_all_mobile_api_routes_answer_cors_preflight():
    server = _serve()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in (
            "/api/tick-stream",
            "/api/live-ticks",
            "/api/model-audit",
            "/api/scalper-mode",
            "/api/journal/download-pdf",
        ):
            response = requests.options(
                base + path, headers={"Origin": "https://vetukurisuresh.github.io"}, timeout=5
            )
            assert response.status_code == 204, path
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert response.headers["X-Accel-Buffering"] == "no"
            assert "Content-Type" in response.headers["Access-Control-Allow-Headers"]
        for path in ("/api/live-ticks?symbol=NIFTY", "/api/scalper-mode"):
            response = requests.get(
                base + path, headers={"Origin": "https://vetukurisuresh.github.io"}, timeout=10
            )
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert response.headers["X-Accel-Buffering"] == "no"
    finally:
        server.shutdown()
        server.server_close()


def test_event_driven_sse_hub_wakes_a_waiter_without_poll_delay():
    """The in-memory condition hub is the SSE push path (no 250 ms file poll)."""
    import threading

    from dashboard.server import TICK_HUB

    marker = f"edge-{time.time_ns()}"
    current = TICK_HUB.frame("NIFTY")
    seen = current[0] if current else 0
    result = {}
    reader = threading.Thread(
        target=lambda: result.update(frame=TICK_HUB.wait("NIFTY", seen, timeout=1.0)),
        daemon=True,
    )
    reader.start()
    time.sleep(0.02)
    started = time.perf_counter()
    TICK_HUB.publish("NIFTY", {"ok": True, "marker": marker}, dispatch_ms=0.05)
    reader.join(timeout=1.0)
    elapsed_ms = (time.perf_counter() - started) * 1000
    frame = result.get("frame")
    assert frame is not None
    assert marker.encode() in frame[1]
    assert elapsed_ms < 15.0, f"event push took {elapsed_ms:.1f} ms"


def test_frontend_routes_api_and_sse_through_edge_and_never_polls_firebase_ticks():
    index = (ROOT / "dashboard/static/index.html").read_text(encoding="utf-8")
    workspace = (ROOT / "dashboard/static/pro_workspace.js").read_text(encoding="utf-8")
    assert "let EDGE_BASE_URL" in index
    assert "resolveLiveRunnerEdgeUrl" in index
    assert "edge-live/live_stream_url.json" in index
    assert "window.EventSource=function" in index
    assert "window.fetch=function" in index
    assert "DIRECT GITHUB RUNNER PIPE" in index
    assert "edgeApi" in workspace
    assert "pollCloudTick(); setInterval" not in index
    assert "setInterval(pollCloudTick" not in index


def test_edge_publisher_record_has_both_discovery_fields_and_local_mirror(tmp_path):
    record = edge_payload("https://x-y-z.trycloudflare.com", active=True, shift="shift-1", run_id="42")
    assert record["url"] == record["edge_url"] == "https://x-y-z.trycloudflare.com"
    assert record["active"] is True
    assert record["runner_status"] == "LIVE_STREAMING"
    assert record["shift"] == "shift-1"
    target = write_local_record(record, tmp_path / "live_stream_url.json")
    assert json.loads(target.read_text(encoding="utf-8"))["edge_url"] == record["edge_url"]


def test_runner_workflows_have_overlap_fast_restart_and_public_sync():
    edge = yaml.safe_load((ROOT / ".github/workflows/02-live-market-edge.yml").read_text(encoding="utf-8"))
    public_sync = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-pages.yml").read_text(encoding="utf-8")
    )
    text = (ROOT / ".github/workflows/02-live-market-edge.yml").read_text(encoding="utf-8")
    schedules = [item["cron"] for item in edge[True]["schedule"]]
    assert "02 3 * * 1-5" in schedules
    assert "05 7 * * 1-5" in schedules
    assert edge["permissions"]["contents"] == "write"
    assert "sleep 1" in text
    assert "KRONOS_SHIFT" in text
    assert public_sync[True]["push"]["branches"] == ["main"]
    sync_steps = public_sync["jobs"]["sync"]["steps"]
    public_checkout = next(
        step for step in sync_steps
        if step.get("with", {}).get("repository") == "vetukurisuresh/kronos-runner-public"
    )
    assert public_checkout["with"]["token"] == "${{ secrets.SECRET_PAT }}"
    assert any("rsync" in step.get("run", "") for step in sync_steps)
    assert (ROOT / "dashboard/static/.nojekyll").exists()


def test_apk_build_answers_flet_flutter_prompt():
    mobile = (ROOT / ".github/workflows/build_mobile_apk.yml").read_text(encoding="utf-8")
    assert "flet build" in mobile
    assert "--yes" in mobile
    assert "--module-name main_apk.py" in mobile


def test_cloudflared_watchdog_restarts_within_one_second():
    from market import cloud_edge

    assert cloud_edge.RESTART_BACKOFF <= 1.0
