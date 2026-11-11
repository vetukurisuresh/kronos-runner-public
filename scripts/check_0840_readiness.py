#!/usr/bin/env python3
"""Fail-closed smoke check for the 08:40 autopilot publication.

The runner publishes ``/system/premarket_readiness.json``.  This script is run
by a separate GitHub Actions workflow at 08:41 IST and intentionally fails if
the record is missing, stale, incomplete, explicitly warming, or late.  It never
prints the Firebase URL or any token.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
READY_STATUS = "READY_0840_ALL_SYSTEMS_GO"
EXPECTED_KEYS = ("auth", "runner", "edge", "socket", "bars", "models")
DEFAULT_DEADLINE = time(8, 40, 0)


def _as_ist(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def validate_readiness(
    payload: Any,
    *,
    now: datetime | None = None,
    deadline: time = DEFAULT_DEADLINE,
) -> tuple[bool, list[str]]:
    """Return ``(ok, errors)`` without trusting any UI-provided flag alone."""
    moment = now or datetime.now(tz=IST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    moment = moment.astimezone(IST)
    errors: list[str] = []
    if not isinstance(payload, dict) or not payload:
        return False, ["readiness record is missing or not an object"]

    checks = payload.get("checks")
    if not isinstance(checks, list) or len(checks) != 6:
        errors.append("expected exactly six readiness checks")
        checks = checks if isinstance(checks, list) else []
    else:
        keys = [str(c.get("key") or "") for c in checks if isinstance(c, dict)]
        if sorted(keys) != sorted(EXPECTED_KEYS):
            errors.append("readiness checks do not match the six required keys")
        for check in checks:
            if not isinstance(check, dict):
                errors.append("a readiness check is not an object")
                continue
            if str(check.get("status") or "").upper() != "GREEN":
                errors.append(f"{check.get('key') or 'unknown'} check is not GREEN")
            if str(check.get("dot") or "") != "✅":
                errors.append(f"{check.get('key') or 'unknown'} check has no green tick")

    if payload.get("green") != 6:
        errors.append("green count is not 6/6")
    if payload.get("all_green") is not True:
        errors.append("all_green is not true")
    if str(payload.get("status") or "") != READY_STATUS:
        errors.append("status is not READY_0840_ALL_SYSTEMS_GO")

    ready_at = _as_ist(payload.get("ready_at_ist") or "")
    if ready_at is None:
        errors.append("ready_at_ist is missing or invalid")
    else:
        if ready_at.date() != moment.date():
            errors.append("ready_at_ist is not from today")
        if ready_at.time() > deadline:
            errors.append("ready_at_ist is after the 08:40:00 IST deadline")
    if payload.get("ready_by_0840") is False:
        errors.append("runner marked ready_by_0840 as false")
    return not errors, errors


def fetch_readiness(base_url: str, timeout: float = 20.0) -> Any:
    """Read the public readiness path; raise on a transport/HTTP failure."""
    import requests

    url = str(base_url or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("FIREBASE_DB_URL is not configured")
    response = requests.get(
        f"{url}/system/premarket_readiness.json",
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the 08:40 runner readiness record")
    parser.add_argument("--url", default=os.environ.get("FIREBASE_DB_URL", ""),
                        help="Firebase database URL (never printed)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Report the current readiness state and exit 0 if Firebase is reachable; never used by the scheduled strict gate",
    )
    args = parser.parse_args(argv)
    try:
        payload = fetch_readiness(args.url, args.timeout)
    except Exception as exc:  # noqa: BLE001 - report safe failure, no URL/body
        print(f"readiness smoke failed: {type(exc).__name__}: {exc}")
        return 2
    ok, errors = validate_readiness(payload)
    if ok:
        print("readiness smoke passed: READY_0840_ALL_SYSTEMS_GO (6/6)")
        return 0
    if args.diagnostic:
        status = payload.get("status") if isinstance(payload, dict) else None
        green = payload.get("green") if isinstance(payload, dict) else None
        print(
            "diagnostic wiring OK: Firebase reachable; "
            f"readiness={status or 'MISSING'} green={green if green is not None else 'n/a'}/6"
        )
        for error in errors[:6]:
            print(f" - {error}")
        print("diagnostic mode: strict 08:40 readiness was NOT asserted")
        return 0
    print("readiness smoke failed:")
    for error in errors:
        print(f" - {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
