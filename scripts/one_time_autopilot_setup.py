#!/usr/bin/env python3
"""One-time zero-touch Upstox/GitHub credential provisioner.

This is intentionally a *separate* localhost onboarding surface: the trading
dashboard never asks for a credential or token.  It reads what already exists,
shows only presence/length metadata, and can push the repository secrets with
`gh secret set` (values go through stdin, never argv/stdout).

    python scripts/one_time_autopilot_setup.py --check
    python scripts/one_time_autopilot_setup.py --sync-only
    python scripts/one_time_autopilot_setup.py --onboard
    python scripts/one_time_autopilot_setup.py --mint

The interactive page binds only 127.0.0.1.  It is one-time; after a successful
sync/mint a lock file records the configuration without storing any secret.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
FIREBASE_KEY_FILE = REPO_ROOT / "state" / "firebase-key.json"
LOCK_FILE = REPO_ROOT / "state" / "autopilot.lock.json"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SECRETS = (
    "UPSTOX_API_KEY",
    "UPSTOX_API_SECRET",
    "UPSTOX_MOBILE",
    "UPSTOX_PIN",
    "UPSTOX_TOTP_SECRET",
    "FIREBASE_CREDENTIALS_JSON",
    "FIREBASE_DB_URL",
    "SECRET_PAT",
)
VARIABLES = ("UPSTOX_MOBILE_REDIRECT_URI",)
DEFAULT_REDIRECT = "https://vetukurisuresh.github.io/kronos-runner-public/"


def read_env_map() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def read_env(key: str, env: dict[str, str] | None = None) -> str:
    return (os.environ.get(key) or (env or {}).get(key) or "").strip()


def upsert_env(values: dict[str, str], path: Path = ENV_FILE) -> None:
    """Write only supplied keys; never print values."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    supplied = {key: str(value) for key, value in values.items() if value}
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in supplied:
                out.append(f"{key}={supplied[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in supplied.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def repo_from_git() -> str:
    try:
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=REPO_ROOT, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", remote)
    return match.group(1) if match else ""


def resolve_repo(explicit: str = "") -> str:
    return (explicit or os.environ.get("GITHUB_REPOSITORY") or repo_from_git()).strip()


def firebase_info() -> tuple[str, str]:
    """Return (credentials JSON, database URL) without logging either."""
    env = read_env_map()
    raw = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip() or env.get("FIREBASE_CREDENTIALS_JSON", "")
    if not raw and FIREBASE_KEY_FILE.is_file():
        raw = FIREBASE_KEY_FILE.read_text(encoding="utf-8", errors="replace").strip()
    url = os.environ.get("FIREBASE_DB_URL", "").strip() or env.get("FIREBASE_DB_URL", "")
    if not url and raw:
        try:
            project = json.loads(raw).get("project_id")
            if project:
                region = env.get("FIREBASE_DB_REGION", "asia-southeast1")
                url = f"https://{project}-default-rtdb.{region}.firebasedatabase.app"
        except ValueError:
            pass
    return raw, url.strip().rstrip("/")


def collect_values() -> dict[str, str]:
    env = read_env_map()
    firebase_json, firebase_url = firebase_info()
    values = {key: read_env(key, env) for key in SECRETS + VARIABLES}
    values["FIREBASE_CREDENTIALS_JSON"] = firebase_json
    values["FIREBASE_DB_URL"] = firebase_url
    values["UPSTOX_MOBILE_REDIRECT_URI"] = (
        values.get("UPSTOX_MOBILE_REDIRECT_URI")
        or os.environ.get("UPSTOX_REDIRECT_URI", "")
        or DEFAULT_REDIRECT
    )
    values["UPSTOX_ACCESS_TOKEN"] = read_env("UPSTOX_ACCESS_TOKEN", env)
    return values


def missing_keys(values: dict[str, str], *, include_token: bool = False) -> list[str]:
    keys = list(SECRETS)
    if include_token:
        keys.append("UPSTOX_ACCESS_TOKEN")
    return [key for key in keys if not values.get(key)]


def valid_totp_secret(value: str) -> bool:
    """Accept the base32 authenticator key, never a six-digit current OTP."""
    normalized = re.sub(r"[\s-]", "", str(value or "")).upper()
    if len(normalized) < 16:
        return False
    padding = "=" * (-len(normalized) % 8)
    try:
        base64.b32decode(normalized + padding, casefold=True)
    except (binascii.Error, ValueError):
        return False
    return True


def invalid_keys(values: dict[str, str]) -> list[str]:
    if values.get("UPSTOX_TOTP_SECRET") and not valid_totp_secret(values["UPSTOX_TOTP_SECRET"]):
        return ["UPSTOX_TOTP_SECRET"]
    return []


def _gh_env(values: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    if values.get("SECRET_PAT"):
        env["GH_TOKEN"] = values["SECRET_PAT"]
    env.setdefault("GH_PROMPT_DISABLED", "1")
    return env


def gh_names(kind: str, repo: str, env: dict[str, str]) -> list[str]:
    command = ["gh", kind, "list", "--repo", repo]
    proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=45)
    if proc.returncode != 0:
        return []
    return [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]


def _gh_set(kind: str, name: str, value: str, repo: str, env: dict[str, str]) -> bool:
    if not value:
        return False
    proc = subprocess.run(
        ["gh", kind, "set", name, "--repo", repo],
        input=value, capture_output=True, text=True, env=env, timeout=60,
    )
    return proc.returncode == 0


def sync_github(values: dict[str, str], repo: str) -> dict[str, list[str]]:
    """Push present values through stdin; return names only, never values."""
    if not repo:
        raise RuntimeError("GitHub repository is not resolvable")
    env = _gh_env(values)
    pushed: list[str] = []
    failed: list[str] = []
    for name in SECRETS + ("UPSTOX_ACCESS_TOKEN",):
        if not values.get(name):
            continue
        if name == "UPSTOX_TOTP_SECRET" and not valid_totp_secret(values[name]):
            failed.append(name)
            continue
        ok = _gh_set("secret", name, values[name], repo, env)
        (pushed if ok else failed).append(name)
    for name in VARIABLES:
        if not values.get(name):
            continue
        ok = _gh_set("variable", name, values[name], repo, env)
        (pushed if ok else failed).append(name)
    return {"pushed": pushed, "failed": failed,
            "existing_secrets": gh_names("secret", repo, env),
            "existing_variables": gh_names("variable", repo, env)}


def mint(values: dict[str, str], repo: str) -> dict[str, object]:
    """Run the headless login exactly once and publish the token."""
    missing = missing_keys(values)
    if missing:
        return {"ok": False, "reason": "missing: " + ", ".join(missing)}
    invalid = invalid_keys(values)
    if invalid:
        return {"ok": False, "reason": "invalid TOTP format: " + ", ".join(invalid)}
    try:
        from market.upstox_health import (
            exchange_code,
            record_verification,
            save_access_token,
            verify_token,
        )
        from scripts.auto_upstox_morning_login import run_login
        from state.firebase_sync import admin, rt_put
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"import failed: {type(exc).__name__}: {exc}"}

    redirect = values["UPSTOX_MOBILE_REDIRECT_URI"]
    try:
        code = run_login(
            values["UPSTOX_MOBILE"], values["UPSTOX_PIN"],
            values["UPSTOX_TOTP_SECRET"], redirect, headless=True,
        )
        result = exchange_code(
            code, values["UPSTOX_API_KEY"], values["UPSTOX_API_SECRET"], redirect,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"login failed: {type(exc).__name__}: {exc}"}
    if not result.get("ok") or not result.get("access_token"):
        return {"ok": False, "reason": f"token exchange failed: HTTP {result.get('status')}"}

    token = str(result["access_token"])
    probe = verify_token(token)
    if not probe.get("ok"):
        return {"ok": False, "reason": f"profile verification failed: HTTP {probe.get('status')}"}
    save_access_token(token, extra={"user": result.get("user"), "source": "one_time_setup"})
    record_verification(True, probe.get("status"))
    values["UPSTOX_ACCESS_TOKEN"] = token

    # Publish the token to the same Firebase path the dashboard uses.
    try:
        url, firebase_token = admin()
        status = rt_put(url, firebase_token, "/upstox_auth/token.json", {
            "access_token": token,
            "user": result.get("user"),
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "one_time_setup",
        })
        if status not in (200, 201, 204):
            return {"ok": False, "reason": f"Firebase token write HTTP {status}"}
        rt_put(url, firebase_token, "/upstox_auth/status.json", {
            "valid": True, "user": result.get("user"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"Firebase publish failed: {type(exc).__name__}"}

    pushed = _gh_set("secret", "UPSTOX_ACCESS_TOKEN", token, repo, _gh_env(values))
    if not pushed:
        return {"ok": False, "reason": "token minted but GitHub secret upload failed"}
    try:
        from market.autopilot_readiness import update_auth_readiness

        minted_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        update_auth_readiness(
            minted_at_ist=minted_at,
            verified=True,
            user=str(result.get("user") or ""),
        )
    except Exception:  # noqa: BLE001 - the token/lock are already durable
        pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "configured": True,
        "repo": repo,
        "configured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "token_secret": "UPSTOX_ACCESS_TOKEN",
    }, indent=1), encoding="utf-8")
    return {"ok": True, "user": result.get("user"), "token_saved": True}


def _form_html(missing: list[str], token: str) -> str:
    rows = []
    for key in missing:
        label = html.escape(key)
        if key == "FIREBASE_CREDENTIALS_JSON":
            rows.append(
                f"<label>{label}<textarea name='{label}' rows='8' spellcheck='false' "
                "placeholder='Paste the service-account JSON once'></textarea></label>"
            )
        else:
            kind = "password" if any(word in key for word in ("PIN", "TOTP", "SECRET", "PAT")) else "text"
            rows.append(f"<label>{label}<input name='{label}' type='{kind}' autocomplete='off'></label>")
    body = "".join(rows)
    return f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>KRONOS one-time autopilot setup</title>
<style>body{{background:#080b11;color:#e6edf3;font:15px system-ui;max-width:640px;margin:30px auto;padding:18px}}
h1{{color:#4aa3ff;font-size:22px}}p{{color:#9fb0c3;line-height:1.6}}label{{display:block;margin:12px 0 5px;color:#cfe2ff;font-size:12px}}
input,textarea{{width:100%;box-sizing:border-box;background:#0f1620;color:#e6edf3;border:1px solid #2a3b4e;border-radius:7px;padding:9px}}
button{{margin-top:16px;background:#17345a;color:#dbeaff;border:1px solid #4aa3ff;border-radius:7px;padding:10px 18px;font-weight:700}}
small{{color:#7f90a4}}.warn{{color:#ffd66e}}</style>
<h1>🔐 One-time autopilot onboarding</h1>
<p>This page runs only on <b>127.0.0.1</b>. Values are written to the git-ignored local environment and sent to GitHub only after you submit. The trading dashboard never asks for them.</p>
<form method='post' action='/save?token={token}'>{body}<button type='submit'>Save once &amp; continue</button></form>
<p><small>Leave a field blank if you want to fill it later. Do not paste credentials into chat.</small></p>"""


def run_onboarding(missing: list[str], values: dict[str, str], port: int = 8099,
                   open_browser: bool = True) -> bool:
    """Serve a one-time localhost form; blocks until saved or Ctrl-C."""
    token = secrets.token_urlsafe(18)
    done = threading.Event()
    saved: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):  # noqa: N802
            if urlparse(self.path).path != "/":
                self.send_error(404)
                return
            body = _form_html(missing, token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/save" or (parse_qs(parsed.query).get("token") or [""])[0] != token:
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            for key in missing:
                value = (form.get(key) or [""])[0].strip()
                if value:
                    saved[key] = value
            if "UPSTOX_TOTP_SECRET" in saved and not valid_totp_secret(saved["UPSTOX_TOTP_SECRET"]):
                self.send_error(400, "TOTP secret must be the base32 authenticator key, not a six-digit code")
                return
            if "FIREBASE_CREDENTIALS_JSON" in saved:
                try:
                    json.loads(saved["FIREBASE_CREDENTIALS_JSON"])
                    FIREBASE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                    FIREBASE_KEY_FILE.write_text(saved["FIREBASE_CREDENTIALS_JSON"], encoding="utf-8")
                    try:
                        FIREBASE_KEY_FILE.chmod(0o600)
                    except OSError:
                        pass
                except ValueError:
                    self.send_error(400, "Firebase JSON is not valid JSON")
                    return
            upsert_env({key: value for key, value in saved.items() if key != "FIREBASE_CREDENTIALS_JSON"})
            body = b"Saved. You can close this tab and return to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    server = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"one-time onboarding: {url} (local only; no secrets are printed)")
    if open_browser:
        webbrowser.open(url)
    try:
        print("waiting for the one-time form (Ctrl-C to stop)…")
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("onboarding cancelled")
        return False
    finally:
        server.shutdown()
        server.server_close()
    values.update(saved)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-time autopilot credential provisioner")
    parser.add_argument("--repo", default="", help="owner/repo (defaults to origin)")
    parser.add_argument("--check", action="store_true", help="report presence and existing GitHub names only")
    parser.add_argument("--sync-only", action="store_true", help="push available values to GitHub")
    parser.add_argument("--mint", action="store_true", help="mint and publish a live token")
    parser.add_argument("--onboard", action="store_true", help="open the one-time localhost form for missing values")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)

    repo = resolve_repo(args.repo)
    values = collect_values()
    missing = missing_keys(values)
    print(f"repo: {repo or '(unknown)'}")
    print("present:", ", ".join(k for k in SECRETS + VARIABLES if values.get(k)) or "(none)")
    print("missing:", ", ".join(missing) or "(none)")
    invalid = invalid_keys(values)
    print("invalid:", ", ".join(invalid) or "(none)")
    print("existing GitHub secrets:", ", ".join(gh_names("secret", repo, _gh_env(values))) or "(none)" if repo else "repo not resolved")
    print("existing GitHub variables:", ", ".join(gh_names("variable", repo, _gh_env(values))) or "(none)" if repo else "repo not resolved")

    if args.check:
        return 2 if invalid else 0

    if invalid:
        print("replace invalid credential fields through --onboard; nothing was uploaded.")
        return 2

    if missing and args.onboard:
        if not run_onboarding(missing, values, args.port, not args.no_browser):
            return 2
        values = collect_values()
        missing = missing_keys(values)

    if args.sync_only or not missing:
        result = sync_github(values, repo)
        print("synced:", ", ".join(result["pushed"]) or "(none)")
        if result["failed"]:
            print("failed:", ", ".join(result["failed"]))
            return 3

    # A completed one-time setup mints automatically; --sync-only remains the
    # explicit no-login option and --check remains read-only.
    should_mint = args.mint or (not args.check and not args.sync_only)
    if should_mint:
        minted = mint(values, repo)
        print("token mint:", "ok" if minted.get("ok") else minted.get("reason"))
        return 0 if minted.get("ok") else 4

    if missing:
        print("onboarding required; run with --onboard (localhost only).")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
