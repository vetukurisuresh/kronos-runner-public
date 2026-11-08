"""Upstox OAuth + the five-point data-sync health bar.

Two responsibilities, both pure Python:

* build the login URL and exchange the ``code`` for an access token, then persist
  it without ever printing it (``.env`` + ``state/upstox_token.json``)
* turn the runtime facts (token, feed, buffers, option chain, model) into the
  five green/yellow/red indicators the dashboard renders

Network calls are isolated in ``exchange_code`` / ``verify_token`` so the rest of
the module is testable offline.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = REPO_ROOT / "state" / "upstox_token.json"
ENV_FILE = REPO_ROOT / ".env"

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"

GREEN = "GREEN"
YELLOW = "YELLOW"
RED = "RED"

DOT = {GREEN: "\U0001F7E2", YELLOW: "\U0001F7E1", RED: "\U0001F534"}


@dataclass
class HealthCheck:
    key: str
    label: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "dot": DOT.get(self.status, "\u26aa"),
            "detail": self.detail,
        }


@dataclass
class HealthInputs:
    """Everything the five checks need, supplied by the caller."""

    token_present: bool = False
    token_valid: bool | None = None
    feed_connected: bool = False
    ticks_per_sec: float = 0.0
    latency_ms: float = 0.0
    buffer_1m: dict[str, int] = field(default_factory=dict)
    buffer_5m: dict[str, int] = field(default_factory=dict)
    capacity: int = 512
    option_chain_ready: bool = False
    option_strikes: list[str] = field(default_factory=list)
    model_online: bool = False
    capital: float = 100_000.0


def authorize_url(client_id: str, redirect_uri: str) -> str:
    """Upstox login page the [Connect Upstox Live] button opens."""
    if not client_id:
        raise ValueError("UPSTOX_API_KEY missing")
    return (
        f"{AUTH_URL}?response_type=code&client_id={client_id}"
        f"&redirect_uri={requests.utils.quote(redirect_uri, safe='')}"
    )


def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Swap the authorization code for an access token."""
    if not code:
        raise ValueError("authorization code missing")
    response = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    payload: dict[str, Any] = {"ok": False, "status": response.status_code}
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:200]}
    payload["body"] = {k: v for k, v in body.items() if k != "access_token"}
    token = (body or {}).get("access_token")
    if response.status_code == 200 and token:
        payload.update({"ok": True, "access_token": token,
                        "user": body.get("user_name") or body.get("user_id")})
    return payload


def verify_token(access_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """Cheap authenticated call that proves the token still works."""
    if not access_token:
        return {"ok": False, "reason": "no token"}
    try:
        response = requests.get(
            PROFILE_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=timeout,
        )
        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "user": (response.json() or {}).get("data", {}).get("user_name")
            if response.status_code == 200 else None,
        }
    except requests.RequestException as exc:
        return {"ok": False, "reason": str(exc)}


def _upsert_env(key: str, value: str, env_file: Path = ENV_FILE) -> None:
    lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines() if env_file.is_file() else []
    seen = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def save_access_token(
    access_token: str,
    token_file: Path = TOKEN_FILE,
    env_file: Path = ENV_FILE,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the token (never logged) and return only metadata."""
    if not access_token:
        raise ValueError("access_token missing")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "access_token": access_token,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **(extra or {}),
    }
    token_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        _upsert_env("UPSTOX_ACCESS_TOKEN", access_token, env_file)
    except OSError:
        pass
    return {"saved": str(token_file), "length": len(access_token),
            "saved_at": record["saved_at"]}


def load_access_token(token_file: Path = TOKEN_FILE) -> str:
    if token_file.is_file():
        try:
            token = (json.loads(token_file.read_text(encoding="utf-8")) or {}).get("access_token")
            if token:
                return str(token)
        except ValueError:
            pass
    return (os.environ.get("UPSTOX_ACCESS_TOKEN") or "").strip()


def five_checks(inputs: HealthInputs) -> dict[str, Any]:
    """Build the five indicators the top bar renders."""
    checks: list[HealthCheck] = []

    if inputs.token_valid:
        checks.append(HealthCheck("auth", "Auth Token", GREEN, "VALID"))
    elif inputs.token_present:
        checks.append(HealthCheck("auth", "Auth Token", YELLOW, "FALLBACK REPLAY MODE"))
    else:
        checks.append(HealthCheck("auth", "Auth Token", RED, "NO TOKEN - REPLAY ONLY"))

    if inputs.feed_connected and inputs.ticks_per_sec > 0:
        checks.append(
            HealthCheck(
                "feed",
                "V3 WebSocket Feed",
                GREEN,
                f"STREAMING {inputs.ticks_per_sec:.1f} ticks/s \u00b7 {inputs.latency_ms:.0f} ms",
            )
        )
    elif inputs.feed_connected:
        checks.append(HealthCheck("feed", "V3 WebSocket Feed", YELLOW, "CONNECTED - NO TICKS"))
    else:
        checks.append(HealthCheck("feed", "V3 WebSocket Feed", RED, "OFFLINE"))

    cap = max(inputs.capacity, 1)
    full = {
        name: count
        for name, count in inputs.buffer_5m.items()
    }
    if full and all(count >= cap for count in full.values()):
        detail = " | ".join(f"{name} {count}/{cap}" for name, count in full.items())
        checks.append(HealthCheck("buffers", "Spot 512-Bar Buffer", GREEN, f"SYNCED ({detail})"))
    elif full:
        detail = " | ".join(f"{name} {count}/{cap}" for name, count in full.items())
        status = YELLOW if max(full.values()) > 32 else RED
        checks.append(HealthCheck("buffers", "Spot 512-Bar Buffer", status, f"WARMING ({detail})"))
    else:
        checks.append(HealthCheck("buffers", "Spot 512-Bar Buffer", RED, "EMPTY"))

    if inputs.option_chain_ready and inputs.option_strikes:
        checks.append(
            HealthCheck(
                "chain",
                "Weekly Option Chain (CE/PE + OI + VWAP)",
                GREEN,
                f"SYNCED ({len(inputs.option_strikes)} strikes, ATM ready)",
            )
        )
    elif inputs.option_chain_ready:
        checks.append(HealthCheck("chain", "Weekly Option Chain", YELLOW, "PARTIAL"))
    else:
        checks.append(HealthCheck("chain", "Weekly Option Chain", RED, "NOT LOADED"))

    if inputs.model_online:
        checks.append(
            HealthCheck(
                "model",
                "KRONOS 3-Model Engine",
                GREEN,
                f"ONLINE (Paper Capital: \u20b9{inputs.capital:,.0f})",
            )
        )
    else:
        checks.append(HealthCheck("model", "KRONOS 3-Model Engine", YELLOW, "CLOUD ONLY"))

    order = {GREEN: 0, YELLOW: 1, RED: 2}
    overall = max(checks, key=lambda check: order.get(check.status, 3)).status
    return {
        "checks": [check.as_dict() for check in checks],
        "overall": overall,
        "overall_dot": DOT.get(overall, "\u26aa"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
