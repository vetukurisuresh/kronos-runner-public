#!/usr/bin/env python3
"""Headless 08:30 IST Upstox login for the zero-touch morning routine.

Requires repository secrets ``UPSTOX_MOBILE``, ``UPSTOX_PIN``,
``UPSTOX_TOTP_SECRET``, ``UPSTOX_API_KEY`` and ``UPSTOX_API_SECRET``. Exits 0
without touching the network when any of them is missing, so the phone fallback
stays usable.

The browser never sees the API secret: it logs in with the mobile, PIN and a
pyotp-generated authenticator code, then this process exchanges the one-time
``code`` for today's access token. The token is written to
``/upstox_auth/token.json`` in Firebase and, with ``--push-secret``, to the
repository's ``UPSTOX_ACCESS_TOKEN`` Actions secret. No credential or token is
ever printed.

Selector drift on Upstox's login page is expected; a screenshot is written to
``state/logs/upstox_login_<ts>.png`` and the exit code is 3 on failure.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

LOG_DIR = REPO_ROOT / "state" / "logs"
TOKEN_FILE = REPO_ROOT / "state" / "upstox_token.json"

MOBILE_SELECTORS = [
    "#mobileNum",
    "input[type=tel]",
    "input[name=mobile]",
    "input[name=mobileNumber]",
    "#mobileNumber",
    "input[name=userId]",
    "input[type=email]",
]
PIN_SELECTORS = [
    "input[name=pin]",
    "input[type=password]",
    "#pin",
    "input[autocomplete=current-password]",
]
TOTP_SELECTORS = [
    "#otpNum",
    "input[name=otp]",
    "input[name=totp]",
    "input[autocomplete='one-time-code']",
    "input[inputmode=numeric]",
    "#otp",
]
SUBMIT_SELECTORS = [
    "button[type=submit]",
    "button:has-text('Get OTP')",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Verify')",
    "button:has-text('Submit')",
    "button:has-text('Login')",
    "[role=button]:has-text('Continue')",
    "[role=button]:has-text('Verify')",
]
ALLOW_SELECTORS = [
    "button:has-text('Allow')",
    "button:has-text('Authorize')",
    "button:has-text('Accept')",
]


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


def extract_code(url: str) -> str:
    """Pull ``code`` from the OAuth redirect without logging the full URL."""
    try:
        return str((parse_qs(urlparse(str(url)).query).get("code") or [""])[0])
    except (TypeError, ValueError):
        return ""


def _visible(page, selectors: list[str], timeout_ms: int = 15000):
    """Find the first visible field, including login content inside an iframe."""
    deadline = time.time() + max(timeout_ms, 0) / 1000.0
    while time.time() < deadline:
        for frame in [page, *page.frames]:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if locator.is_visible():
                        return locator
                except Exception:  # noqa: BLE001 - detached frames and bad selectors are expected
                    continue
        time.sleep(0.25)
    return None


def _click_any(page, selectors: list[str]) -> bool:
    locator = _visible(page, selectors, 2500)
    if locator is None:
        return False
    try:
        locator.click(timeout=7500)
        return True
    except Exception:  # noqa: BLE001 - fall through to Enter
        return False


def _submit(page) -> None:
    if not _click_any(page, SUBMIT_SELECTORS):
        page.keyboard.press("Enter")


def _fill_first(page, selectors: list[str], value: str, timeout_ms: int = 20000) -> bool:
    field = _visible(page, selectors, timeout_ms)
    if field is None:
        return False
    try:
        field.fill(value)
    except Exception:  # noqa: BLE001 - some fields only accept keyboard input
        field.click()
        page.keyboard.type(value)
    _submit(page)
    return True


def _fill_totp(page, code: str) -> bool:
    field = _visible(page, TOTP_SELECTORS, 30000)
    if field is None:
        return False
    try:
        # Upstox's React OTP field needs real key events before Continue enables;
        # fill() alone leaves the button disabled on the current login page.
        field.click()
        page.keyboard.type(code, delay=45)
    except Exception:  # noqa: BLE001 - fall back to a plain fill
        field.fill(code)
    _submit(page)
    return True


def run_login(mobile: str, pin: str, totp_secret: str, redirect_uri: str,
              headless: bool = True) -> str:
    """Drive Upstox mobile -> PIN -> authenticator 2FA and return the code."""
    from playwright.sync_api import sync_playwright

    import pyotp

    from market.upstox_health import authorize_url

    auth_url = authorize_url(read_env("UPSTOX_API_KEY"), redirect_uri)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    shot = LOG_DIR / f"upstox_login_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    codes: list[str] = []

    def remember(url: str) -> None:
        code = extract_code(url)
        if code and code not in codes:
            codes.append(code)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 420, "height": 900})
        page.on(
            "framenavigated",
            lambda frame: remember(frame.url) if frame == page.main_frame else None,
        )
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
            if not _fill_first(page, MOBILE_SELECTORS, mobile, 30000):
                raise RuntimeError("mobile/user-id field not found")

            # Current Upstox web login: mobile -> OTP/TOTP -> PIN.  Some
            # accounts show only a PIN; the TOTP attempt then times out cleanly.
            _fill_totp(
                page,
                pyotp.TOTP(totp_secret.replace(" ", "").replace("-", "").upper()).now(),
            )
            _fill_first(page, PIN_SELECTORS, pin, 25000)

            # The OAuth consent screen can follow the 2FA screen.
            for _ in range(3):
                _click_any(page, ALLOW_SELECTORS)
                page.wait_for_timeout(750)

            # The static Pages redirect strips ?code= from the address bar almost
            # immediately, so framenavigated captured it before this poll.
            deadline = time.time() + 90000
            while time.time() < deadline and not codes:
                remember(page.url)
                _click_any(page, ALLOW_SELECTORS)
                page.wait_for_timeout(500)

            if not codes:
                raise RuntimeError("no authorization code in redirect")
            return codes[0]
        except Exception:
            try:
                page.screenshot(path=str(shot), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless Upstox morning login")
    parser.add_argument("--push-secret", action="store_true",
                        help="update UPSTOX_ACCESS_TOKEN in GitHub Actions secrets")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--require", action="store_true",
                        help="fail instead of silently falling back when credentials are absent")
    args = parser.parse_args(argv)

    mobile = read_env("UPSTOX_MOBILE")
    pin = read_env("UPSTOX_PIN")
    totp = read_env("UPSTOX_TOTP_SECRET")
    api_key = read_env("UPSTOX_API_KEY")
    api_secret = read_env("UPSTOX_API_SECRET")
    missing = [
        name for name, value in (
            ("UPSTOX_MOBILE", mobile),
            ("UPSTOX_PIN", pin),
            ("UPSTOX_TOTP_SECRET", totp),
            ("UPSTOX_API_KEY", api_key),
            ("UPSTOX_API_SECRET", api_secret),
        ) if not value
    ]
    if missing:
        print(f"auto-login not configured (missing: {', '.join(missing)}) - phone flow stays active")
        return 2 if args.require else 0

    redirect = (read_env("UPSTOX_MOBILE_REDIRECT_URI")
                or "https://vetukurisuresh.github.io/kronos-runner-public/")
    try:
        code = run_login(mobile, pin, totp, redirect, headless=not args.headed)
    except Exception as exc:  # noqa: BLE001
        print(f"auto-login failed: {type(exc).__name__}: {exc}")
        return 3
    if not code:
        print("auto-login finished without a code")
        return 3

    from market.upstox_health import (
        exchange_code,
        record_verification,
        save_access_token,
        verify_token,
    )

    try:
        result = exchange_code(code, api_key, api_secret, redirect)
    except Exception as exc:  # noqa: BLE001
        print(f"token exchange failed: {type(exc).__name__}: {exc}")
        return 3
    if not result.get("ok"):
        print(f"token exchange failed: HTTP {result.get('status')}")
        return 3
    access_token = str(result.get("access_token") or "")
    user = result.get("user")
    if not access_token:
        print("token exchange returned no access_token")
        return 3

    probe = verify_token(access_token)
    if not probe.get("ok"):
        print(f"token minted but profile verification failed: HTTP {probe.get('status')}")
        return 6

    try:
        meta = save_access_token(
            access_token,
            extra={"user": user, "source": "headless_playwright"},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"local token save failed: {type(exc).__name__}: {exc}")
        return 5
    record_verification(True, probe.get("status"), TOKEN_FILE)
    print(f"token minted for {user or 'unknown user'} ({meta['length']} chars)")

    try:
        from scripts.mobile_token_exchange import (
            IST,
            PENDING,
            STATUS_PATH,
            TOKEN_PATH,
            next_expiry,
        )
        from state.firebase_sync import admin, rt_delete, rt_put

        url, firebase_token = admin()
        now = datetime.now(tz=IST)
        expiry = next_expiry(now)
        status = rt_put(url, firebase_token, TOKEN_PATH, {
            "access_token": access_token,
            "user": user,
            "issued_at": now.isoformat(timespec="seconds"),
            "expires_at": expiry.isoformat(timespec="seconds"),
            "source": "headless_playwright",
        })
        if status not in (200, 201, 204):
            raise RuntimeError(f"token write HTTP {status}")
        rt_put(url, firebase_token, STATUS_PATH, {
            "valid": True,
            "date": now.date().isoformat(),
            "user": user,
            "updated_at": now.isoformat(timespec="seconds"),
            "expires_at": expiry.isoformat(timespec="seconds"),
            "source": "headless_playwright",
        })
        rt_delete(url, firebase_token, PENDING)
    except Exception as exc:  # noqa: BLE001
        print(f"Firebase token publish failed: {type(exc).__name__}: {exc}")
        return 5
    print(f"token published to Firebase; expires {expiry.isoformat(timespec='minutes')}")

    try:
        from market.autopilot_readiness import update_auth_readiness

        update_auth_readiness(
            minted_at_ist=now.isoformat(timespec="seconds"),
            verified=True,
            user=str(user or ""),
        )
        print("premarket readiness: auth_minted=true")
    except Exception as exc:  # noqa: BLE001 - token is already useful
        print(f"readiness publish failed: {type(exc).__name__}: {exc}")

    if args.push_secret:
        repo = (os.environ.get("GITHUB_REPOSITORY")
                or read_env("GITHUB_REPOSITORY")
                or "vetukurisuresh/kronos-trading-brain")
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from set_github_secrets import main as upload_secret

            rc = upload_secret([
                "--repo", repo,
                "--secret", f"UPSTOX_ACCESS_TOKEN={access_token}",
            ])
        except SystemExit as exc:
            print(f"GitHub secret upload failed: {exc}")
            return 4
        except Exception as exc:  # noqa: BLE001
            print(f"GitHub secret upload failed: {type(exc).__name__}: {exc}")
            return 4
        if rc != 0:
            print("GitHub secret upload failed; Firebase already has today's token")
            return 4
        print("UPSTOX_ACCESS_TOKEN secret updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
