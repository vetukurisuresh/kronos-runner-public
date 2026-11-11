#!/usr/bin/env python3
"""Dual-channel publisher for the GitHub runner's live edge URL.

Channel A: Firebase ``/system/live_stream_url.json`` (and its public mirror).
Channel B: a one-file ``edge-live`` orphan branch in the project repo, so the
static GitHub Pages terminal can discover the active tunnel through
``raw.githubusercontent.com`` even when Firebase is slow or unreachable.

The record is intentionally identical in both channels. Tick data never travels
through Firebase: it goes straight over the runner's SSE tunnel.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_RECORD = REPO_ROOT / "state" / "live_stream_url.json"
GITHUB_API = "https://api.github.com"
EDGE_BRANCH = "edge-live"
RECORD_FILENAME = "live_stream_url.json"
DEFAULT_REPO = "vetukurisuresh/kronos-trading-brain"
IST = timezone(timedelta(hours=5, minutes=30))


def edge_payload(
    url: str | None,
    *,
    active: bool,
    shift: str = "",
    run_id: str = "",
    port: int = 8080,
    host: str = "",
) -> dict[str, Any]:
    """Build the canonical discovery record used by both channels."""
    clean = str(url or "").strip().rstrip("/")
    live = bool(active and clean)
    now = time.time()
    return {
        "url": clean if live else None,
        "edge_url": clean if live else None,
        "active": live,
        "ts": round(now, 3),
        "iso": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "published_at_ist": datetime.now(tz=IST).isoformat(timespec="seconds"),
        "shift": shift or os.environ.get("KRONOS_SHIFT", ""),
        "runner_status": "LIVE_STREAMING" if live else "STOPPED",
        "port": port,
        "host": host or os.environ.get("RUNNER_NAME", ""),
        "run_id": run_id or os.environ.get("GITHUB_RUN_ID", ""),
        "version": 2,
    }


def write_local_record(record: dict[str, Any], path: Path | None = None) -> Path:
    """Write the discovery record into the runner workspace/artifact."""
    target = Path(path or LOCAL_RECORD)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return target


def _token(explicit: str | None = None) -> str:
    token = str(explicit or os.environ.get("GITHUB_TOKEN") or os.environ.get("SECRET_PAT") or "").strip()
    if token:
        return token
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("SECRET_PAT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _github(method: str, path: str, token: str, payload: dict | None = None, timeout: float = 15.0) -> dict:
    response = requests.request(
        method,
        f"{GITHUB_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kronos-edge-publisher",
        },
        json=payload,
        timeout=timeout,
    )
    body: dict = {}
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    return {"ok": response.status_code in (200, 201, 204), "status": response.status_code, "body": body}


def publish_github_record(
    record: dict[str, Any],
    *,
    repo: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Push the record as the only file on the lightweight ``edge-live`` branch."""
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    token = _token(token)
    if not token:
        return {"ok": False, "reason": "GITHUB_TOKEN/SECRET_PAT missing"}
    content = json.dumps(record, indent=1, ensure_ascii=False) + "\n"
    blob = _github(
        "POST", f"/repos/{repo}/git/blobs", token,
        {"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"},
    )
    if not blob["ok"]:
        return {"ok": False, "stage": "blob", **blob}
    tree = _github(
        "POST", f"/repos/{repo}/git/trees", token,
        {"tree": [{"path": RECORD_FILENAME, "mode": "100644", "type": "blob",
                   "sha": blob["body"]["sha"]}]},
    )
    if not tree["ok"]:
        return {"ok": False, "stage": "tree", **tree}
    commit = _github(
        "POST", f"/repos/{repo}/git/commits", token,
        {"message": "edge-live runner discovery record", "tree": tree["body"]["sha"], "parents": []},
    )
    if not commit["ok"]:
        return {"ok": False, "stage": "commit", **commit}
    sha = commit["body"]["sha"]
    ref = _github("PATCH", f"/repos/{repo}/git/refs/heads/{EDGE_BRANCH}", token,
                  {"sha": sha, "force": True})
    if ref["status"] == 404:
        ref = _github("POST", f"/repos/{repo}/git/refs", token,
                      {"ref": f"refs/heads/{EDGE_BRANCH}", "sha": sha})
    if not ref["ok"]:
        return {"ok": False, "stage": "ref", **ref}
    return {"ok": True, "sha": sha, "branch": EDGE_BRANCH, "repo": repo}


def publish_edge_record(record: dict[str, Any], publisher=None, *, github: bool = True) -> dict[str, Any]:
    """Publish through every available channel; a failure in one never blocks the rest."""
    result: dict[str, Any] = {"local": str(write_local_record(record))}
    if publisher is not None:
        result["firebase"] = {
            "/system/live_stream_url.json": publisher.put("/system/live_stream_url.json", record),
            "/kronos_state/live_stream_url.json": publisher.put("/kronos_state/live_stream_url.json", record),
        }
    if github:
        try:
            result["github"] = publish_github_record(record)
        except Exception as exc:  # noqa: BLE001 - discovery must never kill the session
            result["github"] = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return result
