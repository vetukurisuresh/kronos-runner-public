#!/usr/bin/env python3
"""Dispatch the cloud forecast, wait for it, and pull the JSON artifact locally.

    python scripts/run_cloud_forecast.py --samples 100
    python scripts/run_cloud_forecast.py --run-id 12345678901   # pull only

The artifact is written to ``state/latest_cloud_state.json`` (a few hundred KB).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

API = "https://api.github.com"
REPO_ROOT = Path(__file__).resolve().parents[1]
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


def session_for(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kronos-cloud-forecast",
        }
    )
    return session


def dispatch(session, owner, repo, ref, samples, replay_evals, live_loop=False, interval_sec=300) -> None:
    url = f"{API}/repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    body = {
        "ref": ref,
        "inputs": {
            "samples": str(samples),
            "replay_evals": str(replay_evals),
            "live_loop": "true" if live_loop else "false",
            "interval_sec": str(interval_sec),
        },
    }
    response = session.post(url, json=body, timeout=30)
    if response.status_code not in (201, 204):
        raise SystemExit(
            f"dispatch failed: HTTP {response.status_code}\n{response.text[:400]}"
        )
    print(f"dispatched {WORKFLOW_FILE} (samples={samples}, replay={replay_evals}, live={live_loop})")


def find_run(session, owner, repo, since: datetime) -> dict | None:
    response = session.get(
        f"{API}/repos/{owner}/{repo}/actions/runs",
        params={"event": "workflow_dispatch", "per_page": 15},
        timeout=30,
    )
    response.raise_for_status()
    for run in response.json().get("workflow_runs", []):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created >= since and run.get("path", "").endswith(WORKFLOW_FILE):
            return run
    return None


def wait(session, owner, repo, since, poll, timeout) -> dict:
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        run = find_run(session, owner, repo, since)
        if run:
            status, conclusion = run["status"], run.get("conclusion")
            if (status, conclusion) != seen:
                elapsed = int((datetime.now(tz=timezone.utc) - since).total_seconds())
                print(f"  [{elapsed:>4}s] status={status:<12} conclusion={conclusion or '-'}")
                seen = (status, conclusion)
            if status == "completed":
                return run
        time.sleep(poll)
    raise SystemExit("timed out waiting for the forecast run")


def pull_artifact(session, owner, repo, run_id, destination: Path) -> Path:
    response = session.get(f"{API}/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts", timeout=30)
    response.raise_for_status()
    artifacts = response.json().get("artifacts", [])
    match = next((item for item in artifacts if item["name"] == ARTIFACT_NAME), None)
    if not match:
        raise SystemExit(f"artifact {ARTIFACT_NAME} not found; run artifacts: {[a['name'] for a in artifacts]}")
    download = session.get(match["archive_download_url"], allow_redirects=True, timeout=120)
    download.raise_for_status()
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = archive.namelist()
    target = next((name for name in names if name.endswith("nifty_kronos_state.json")), names[0])
    payload = archive.read(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    data = json.loads(payload.decode("utf-8"))
    print(f"downloaded {destination} ({len(payload) / 1024:.1f} KB)")
    for name, block in data.get("symbols", {}).items():
        print(
            f"  {name}: spot {block['spot']:,.2f} · paths_up "
            f"{block['forecast']['paths_up']}/{block['forecast']['samples']} · "
            f"{block['forecast']['regime']} · {block['decision']['action']}"
        )
    summary = data.get("replay_summary", {})
    print(f"  replay: {summary.get('checkpoints')} checkpoints, "
          f"{summary.get('trades')} trades, T1 hit-rate {summary.get('hit_rate')}%")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the cloud Nifty forecast and pull the JSON")
    parser.add_argument("--repo", default="kronos-runner-public")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--samples", default="32")
    parser.add_argument("--replay-evals", default="12")
    parser.add_argument("--live-loop", action="store_true")
    parser.add_argument("--interval-sec", default="300")
    parser.add_argument("--poll", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--run-id", default=None, help="skip dispatch, pull this run")
    parser.add_argument("--out", default=str(REPO_ROOT / "state" / "latest_cloud_state.json"))
    args = parser.parse_args(argv)

    token = read_env("SECRET_PAT")
    if not token:
        raise SystemExit("SECRET_PAT missing from environment and .env")
    owner = read_env("GITHUB_USERNAME")
    if not owner:
        raise SystemExit("GITHUB_USERNAME missing from environment and .env")
    session = session_for(token)

    if args.run_id:
        run = session.get(f"{API}/repos/{owner}/{args.repo}/actions/runs/{args.run_id}", timeout=30).json()
    else:
        since = datetime.now(tz=timezone.utc).replace(microsecond=0)
        dispatch(session, owner, args.repo, args.ref, args.samples, args.replay_evals,
                 args.live_loop, args.interval_sec)
        print("waiting for the run to appear …")
        run = wait(session, owner, args.repo, since, args.poll, args.timeout)

    print(f"\nRUN URL: {run['html_url']}")
    if run.get("conclusion") != "success":
        print(f"run finished with conclusion={run.get('conclusion')}")
        return 1
    pull_artifact(session, owner, args.repo, run["id"], Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
