#!/usr/bin/env python3
"""Headless 08:50 IST Upstox login (optional, zero-touch).

Requires repository secrets ``UPSTOX_MOBILE``, ``UPSTOX_PIN`` and
``UPSTOX_TOTP_SECRET``. Exits 0 without touching the network when any of them is
missing, so the scheduled workflow stays green for users who prefer the phone.

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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

LOG_DIR = REPO_ROOT / "state" / "logs"


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


def _first(page, selectors: list[str], timeout: int = 15000):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            return locator
        except Exception:  # noqa: BLE001 - try the next selector
            continue
    return None


def run_login(mobile: str, pin: str, totp_secret: str, redirect_uri: str, headless: bool = True) -> str:
    """Drive the Upstox login page and return the authorization code."""
    from playwright.sync_api import sync_playwright

    import pyotp

    from market.upstox_health import authorize_url

    auth_url = authorize_url(read_env("UPSTOX_API_KEY"), redirect_uri)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    shot = LOG_DIR / f"upstox_login_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 420, "height": 900})
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
            number = _first(page, ["input[type=tel]", "input[name=mobile]", "#mobileNumber"])
            if number is None:
                raise RuntimeError("mobile field not found")
            number.fill(mobile)
            page.keyboard.press("Enter")
            time.sleep(2)

            otp_box = _first(page, ["input[name=otp]", "input[autocomplete='one-time-code']", "input[type=tel]"])
            if otp_box is None:
                raise RuntimeError("OTP field not found")
            otp_box.fill(pyotp.TOTP(totp_secret).now())
            page.keyboard.press("Enter")
            time.sleep(3)

            pin_box = _first(page, ["input[type=password]", "input[name=pin]"])
            if pin_box is None:
                raise RuntimeError("PIN field not found")
            pin_box.fill(pin)
            page.keyboard.press("Enter")

            page.wait_for_url("**code=*", timeout=60000)
            from urllib.parse import parse_qs, urlparse

            return (parse_qs(urlparse(page.url).query).get("code") or [""])[0]
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
    parser.add_argument("--push-secret", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    mobile, pin, totp = read_env("UPSTOX_MOBILE"), read_env("UPSTOX_PIN"), read_env("UPSTOX_TOTP_SECRET")
    missing = [name for name, value in
               (("UPSTOX_MOBILE", mobile), ("UPSTOX_PIN", pin), ("UPSTOX_TOTP_SECRET", totp)) if not value]
    if missing:
        print(f"auto-login not configured (missing: {', '.join(missing)}) - phone flow stays active")
        return 0

    redirect = read_env("UPSTOX_MOBILE_REDIRECT_URI") or "https://vetukurisuresh.github.io/kronos-runner-public/"
    try:
        code = run_login(mobile, pin, totp, redirect, headless=not args.headed)
    except Exception as exc:  # noqa: BLE001
        print(f"auto-login failed: {type(exc).__name__}: {exc}")
        return 3
    if not code:
        print("auto-login finished without a code")
        return 3

    from state.firebase_sync import admin, rt_put
    from datetime import timedelta, timezone

    url, token = admin()
    rt_put(url, token, "/upstox_handshake/pending_code.json", {
        "code": code,
        "redirect": redirect,
        "source": "auto-login",
    })
    print("code delivered to the exchange queue")
    if args.push_secret:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from set_github_secrets import main as upload_secret

            upload_secret(["--repo", os.environ.get("GITHUB_REPOSITORY", "vetukurisuresh/kronos-runner-public"),
                           "--secret", f"UPSTOX_ACCESS_TOKEN={token}"])
        except Exception as exc:  # noqa: BLE001
            print(f"secret upload skipped: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
