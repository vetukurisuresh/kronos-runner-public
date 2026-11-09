#!/usr/bin/env python3
"""Mobile OAuth handshake: phone code -> daily Upstox token, exchanged cloud-side.

The phone browser never sees the API secret. It only PUTs the one-time ``code``
to ``/upstox_handshake/pending_code``; this script (cron every 5 minutes on the
runner) reads it, exchanges it with Upstox, stores the token in
``/upstox_auth/token`` (not world-readable) and publishes ``/upstox_auth/status``
so the dashboard's health bar can turn green.

    python scripts/mobile_token_exchange.py            # exchange if a code waits
    python scripts/mobile_token_exchange.py --dry-run  # report without calling Upstox
    python scripts/mobile_token_exchange.py --push-secret
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from market.upstox_health import exchange_code  # noqa: E402
from state.firebase_sync import admin, rt_delete, rt_get, rt_put  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
PENDING = "/upstox_handshake/pending_code.json"
TOKEN_PATH = "/upstox_auth/token.json"
STATUS_PATH = "/upstox_auth/status.json"


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


def next_expiry(now: datetime | None = None) -> datetime:
    """Upstox tokens die at 03:30 IST the following day."""
    now = now or datetime.now(tz=IST)
    expiry = now.replace(hour=3, minute=30, second=0, microsecond=0)
    if expiry <= now:
        expiry += timedelta(days=1)
    return expiry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exchange a mobile Upstox code")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--push-secret", action="store_true", help="also update the repo secret")
    args = parser.parse_args(argv)

    try:
        url, token = admin()
    except Exception as exc:  # noqa: BLE001
        print(f"firebase admin unavailable: {exc}")
        return 1

    pending = rt_get(url, token, PENDING)
    if not pending:
        print("no pending mobile code")
        return 0
    code = str((pending or {}).get("code", "")).strip()
    if not code:
        print("pending payload has no code")
        rt_delete(url, token, PENDING)
        return 1
    redirect = str((pending or {}).get("redirect") or read_env("UPSTOX_MOBILE_REDIRECT_URI")).strip()
    if not redirect:
        print("no redirect uri recorded with the code")
        return 1

    print(f"pending code received ({len(code)} chars), redirect {redirect}")
    if args.dry_run:
        print("dry run: not calling Upstox")
        return 0

    result = exchange_code(
        code,
        read_env("UPSTOX_API_KEY"),
        read_env("UPSTOX_API_SECRET"),
        redirect,
    )
    now = datetime.now(tz=IST)
    if not result.get("ok"):
        status = result.get("status")
        print(f"exchange failed: HTTP {status}")
        rt_put(url, token, STATUS_PATH, {
            "valid": False, "reason": f"exchange HTTP {status}",
            "updated_at": now.isoformat(timespec="seconds"),
        })
        # Consume the code so the cron does not retry a dead code every 5 minutes.
        rt_delete(url, token, PENDING)
        return 2

    access_token = result["access_token"]
    expiry = next_expiry(now)
    rt_put(url, token, TOKEN_PATH, {
        "access_token": access_token,
        "user": result.get("user"),
        "issued_at": now.isoformat(timespec="seconds"),
        "expires_at": expiry.isoformat(timespec="seconds"),
    })
    rt_put(url, token, STATUS_PATH, {
        "valid": True,
        "date": now.date().isoformat(),
        "user": result.get("user"),
        "updated_at": now.isoformat(timespec="seconds"),
        "expires_at": expiry.isoformat(timespec="seconds"),
    })
    rt_delete(url, token, PENDING)
    print(f"token stored for {result.get('user')}, expires {expiry.isoformat(timespec='minutes')}")

    if args.push_secret:
        try:
            sys.path.insert(0, str(REPO_ROOT / "scripts"))
            from set_github_secrets import main as upload_secret

            repo = os.environ.get("GITHUB_REPOSITORY") or read_env("GITHUB_REPOSITORY") or "vetukurisuresh/kronos-runner-public"
            rc = upload_secret(["--repo", repo, "--secret", f"UPSTOX_ACCESS_TOKEN={access_token}"])
            print("secret upload:", "ok" if rc == 0 else f"exit {rc}")
        except Exception as exc:  # noqa: BLE001
            print(f"secret upload skipped: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
