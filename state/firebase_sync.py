#!/usr/bin/env python3
"""Push the KRONOS state to Firebase Realtime Database.

The runner authenticates with a service-account JSON (``FIREBASE_CREDENTIALS_JSON``
as a GitHub secret, or ``state/firebase-key.json`` locally) and writes:

    PUT {db}/kronos_state/live.json          candles, fan, plan, regime
    PUT {db}/kronos_state/telugu_logs.json   Telugu thought stream

Missing configuration is never fatal: the forecast still runs and the dashboard
falls back to the local JSON file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPE = "https://www.googleapis.com/auth/firebase.database"


def _credentials_info() -> dict | None:
    raw = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            print("  firebase: FIREBASE_CREDENTIALS_JSON is not valid JSON")
            return None
    for candidate in (
        Path(os.environ.get("FIREBASE_KEY_PATH", "")) if os.environ.get("FIREBASE_KEY_PATH") else None,
        REPO_ROOT / "state" / "firebase-key.json",
        REPO_ROOT / "firebase-key.json",
    ):
        if candidate and candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except ValueError:
                print(f"  firebase: {candidate} is not valid JSON")
                return None
    return None


def database_url(info: dict | None) -> str:
    configured = (os.environ.get("FIREBASE_DB_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    project = (info or {}).get("project_id") or os.environ.get("FIREBASE_PROJECT_ID", "")
    if not project:
        return ""
    return f"https://{project}-default-rtdb.firebaseio.com"


def _access_token(info: dict) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(info, scopes=[DEFAULT_SCOPE])
    credentials.refresh(Request())
    return credentials.token


def push_state(payload: dict, db_url: str = "", info: dict | None = None) -> dict:
    """PUT the live state and the Telugu logs. Returns a status dict, never raises."""
    info = info if info is not None else _credentials_info()
    url = (db_url or database_url(info)).rstrip("/")
    if not info:
        return {"ok": False, "reason": "no service-account credentials configured"}
    if not url:
        return {"ok": False, "reason": "no FIREBASE_DB_URL configured"}

    try:
        token = _access_token(info)
    except Exception as exc:  # noqa: BLE001 - auth failures are reported, not raised
        return {"ok": False, "reason": f"auth failed: {type(exc).__name__}: {exc}"}

    live = {
        "generated_at": payload.get("generated_at"),
        "runner": payload.get("runner", {}),
        "model": payload.get("model", {}),
        "symbols": payload.get("symbols", {}),
        "replay": payload.get("replay", []),
        "replay_summary": payload.get("replay_summary", {}),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "pushed_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    logs: list[dict] = []
    for block in payload.get("symbols", {}).values():
        for entry in block.get("telugu_logs", []):
            logs.append({**entry, "symbol": block.get("symbol")})
    logs.sort(key=lambda item: item.get("t", ""))

    results = {}
    for name, body in (("live", live), ("telugu_logs", logs)):
        endpoint = f"{url}/kronos_state/{name}.json"
        try:
            response = requests.put(endpoint, params={"access_token": token}, json=body, timeout=45)
            results[name] = {"ok": response.status_code == 200, "status": response.status_code}
            if response.status_code != 200:
                results[name]["body"] = response.text[:200]
        except Exception as exc:  # noqa: BLE001
            results[name] = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    ok = all(item.get("ok") for item in results.values())
    return {"ok": ok, "url": url, "objects": results}


def ensure_public_read_rules(info: dict | None = None, db_url: str = "") -> dict:
    """Allow unauthenticated reads of ``kronos_state`` only (mobile dashboard).

    Writes stay denied for clients; the runner uses a service-account token,
    which bypasses rules. Other database paths are untouched.
    """
    info = info if info is not None else _credentials_info()
    url = (db_url or database_url(info)).rstrip("/")
    if not info or not url:
        return {"ok": False, "reason": "credentials or database URL missing"}
    rules = {"rules": {".read": False, ".write": False,
                        "kronos_state": {".read": True, ".write": False}}}
    try:
        token = _access_token(info)
        response = requests.put(
            f"{url}/.settings/rules.json", params={"access_token": token}, json=rules, timeout=45
        )
        return {"ok": response.status_code == 200, "status": response.status_code,
                "body": response.text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    if os.environ.get("FIREBASE_APPLY_RULES"):
        print(json.dumps(ensure_public_read_rules(), indent=2))
    state_file = REPO_ROOT / "state" / "latest_cloud_state.json"
    if not state_file.is_file():
        print("no state file to push")
        return 1
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    result = push_state(payload)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
