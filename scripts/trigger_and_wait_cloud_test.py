#!/usr/bin/env python3
"""Trigger the cloud ensemble verification and wait for it live.

Dispatches ``verify-kronos-ensemble.yml`` on ``kronos-runner-public``, polls the
run every 10 seconds, then downloads the job log so a failure can be diagnosed
without leaving the terminal.

Only ``requests`` is used, and the token is read from ``SECRET_PAT`` (or the
git-ignored ``.env``) — it is never printed.

Usage::

    python scripts/trigger_and_wait_cloud_test.py
    python scripts/trigger_and_wait_cloud_test.py --samples 8 --timeout 5400
    python scripts/trigger_and_wait_cloud_test.py --watch-only 12345678901
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

# The Windows console is cp1252; job logs contain box-drawing and tick glyphs.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - non-TTY streams
    pass

API = "https://api.github.com"
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = "verify-kronos-ensemble.yml"


def load_token() -> str:
    token = (os.environ.get("SECRET_PAT") or "").strip()
    if token:
        return token
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("SECRET_PAT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("SECRET_PAT not found in the environment or .env")


def owner_from(token: str) -> str:
    if os.environ.get("GITHUB_USERNAME"):
        return os.environ["GITHUB_USERNAME"].strip()
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("GITHUB_USERNAME="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("GITHUB_USERNAME not found")


def session_for(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kronos-cloud-verifier",
        }
    )
    return session


def dispatch(session: requests.Session, owner: str, repo: str, ref: str, samples: str) -> None:
    url = f"{API}/repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    response = session.post(url, json={"ref": ref, "inputs": {"samples": samples}}, timeout=30)
    if response.status_code not in (204, 201):
        hints = {
            401: "token invalid",
            403: "token lacks the `workflow` scope, or Actions are disabled",
            404: "workflow not found on that ref (push it to main first)",
            422: "bad ref or inputs",
        }
        raise SystemExit(
            f"dispatch failed: HTTP {response.status_code} "
            f"({hints.get(response.status_code, 'unexpected')})\n{response.text[:400]}"
        )
    print(f"dispatched {WORKFLOW_FILE} on {owner}/{repo}@{ref} (samples={samples})")


def latest_run(session: requests.Session, owner: str, repo: str, since: datetime) -> dict | None:
    url = f"{API}/repos/{owner}/{repo}/actions/runs"
    response = session.get(url, params={"event": "workflow_dispatch", "per_page": 10}, timeout=30)
    response.raise_for_status()
    for run in response.json().get("workflow_runs", []):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if created >= since and run.get("path", "").endswith(WORKFLOW_FILE):
            return run
    return None


def wait_for_run(session, owner, repo, since, poll, timeout) -> dict:
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        run = latest_run(session, owner, repo, since)
        if run:
            status, conclusion = run["status"], run.get("conclusion")
            if (status, conclusion) != seen:
                print(f"  status={status:<12} conclusion={conclusion or '-'}")
                seen = (status, conclusion)
            if status == "completed":
                return run
        time.sleep(poll)
    raise SystemExit("timed out waiting for the workflow run")


def print_jobs(session, owner, repo, run_id) -> None:
    response = session.get(f"{API}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", timeout=30)
    if response.status_code != 200:
        print(f"(jobs unavailable: HTTP {response.status_code})")
        return
    for job in response.json().get("jobs", []):
        print(f"  job {job['name']}: {job['status']}/{job.get('conclusion')}")
        for step in job.get("steps", []):
            print(f"     - {step['name']:<42} {step.get('conclusion')}")


def print_logs(session, owner, repo, run_id, tail: int = 160) -> None:
    url = f"{API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    response = session.get(url, allow_redirects=True, timeout=120)
    if response.status_code != 200:
        print(f"(logs unavailable: HTTP {response.status_code} - {response.text[:200]})")
        return
    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
    except zipfile.BadZipFile:
        print("(logs were not a zip archive)")
        return

    for name in sorted(archive.namelist()):
        if not name.endswith(".txt"):
            continue
        content = archive.read(name).decode("utf-8", errors="replace").splitlines()
        print(f"\n----- {name} (last {tail} lines) -----")
        for line in content[-tail:]:
            print(line.encode("ascii", "replace").decode("ascii"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trigger and watch the cloud ensemble test")
    parser.add_argument("--repo", default="kronos-runner-public")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--samples", default="8", help="Monte Carlo samples per model")
    parser.add_argument("--poll", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--watch-only", default=None, help="skip dispatch, watch this run id")
    parser.add_argument("--tail", type=int, default=160)
    arguments = parser.parse_args(argv)

    token = load_token()
    owner = owner_from(token)
    session = session_for(token)

    if arguments.watch_only:
        print(f"watching existing run {arguments.watch_only}")
        run = session.get(
            f"{API}/repos/{owner}/{arguments.repo}/actions/runs/{arguments.watch_only}", timeout=30
        )
        run.raise_for_status()
        run = run.json()
    else:
        since = datetime.now(tz=timezone.utc).replace(microsecond=0)
        dispatch(session, owner, arguments.repo, arguments.ref, arguments.samples)
        print("waiting for the run to appear…")
        run = wait_for_run(session, owner, arguments.repo, since, arguments.poll, arguments.timeout)

    run_id = run["id"]
    print(f"\nRUN ID   : {run_id}")
    print(f"RUN URL  : {run['html_url']}")
    print(f"STATUS   : {run['status']} / {run.get('conclusion')}")
    print("\njob breakdown:")
    print_jobs(session, owner, arguments.repo, run_id)
    print_logs(session, owner, arguments.repo, run_id, arguments.tail)

    Path("state/logs").mkdir(parents=True, exist_ok=True)
    (Path("state/logs") / f"cloud_run_{run_id}.json").write_text(
        json.dumps({"run": run}, indent=2), encoding="utf-8"
    )

    if run.get("conclusion") != "success":
        print(f"\nFAILED: conclusion={run.get('conclusion')}")
        return 1
    print("\nSUCCESS: cloud ensemble verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
