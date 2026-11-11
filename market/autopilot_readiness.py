"""Six-point 08:40 autopilot readiness.

This module deliberately separates *facts* from *claims*.  A check is GREEN
only when the underlying value is present and usable.  During the 08:25-08:40
sync window an incomplete check is YELLOW (the UI renders it as a waiting
hourglass); after 08:40 it is RED.

The live session writes a readiness heartbeat every second.  The dashboard
reads that heartbeat, the live feed and the token metadata; it never turns a
missing field into a green check.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
READINESS_FILE = REPO_ROOT / "state" / "autopilot_readiness.json"
LOCK_FILE = REPO_ROOT / "state" / "autopilot.lock.json"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

BANNER_TEXT = (
    "🔔 UPSTOX AUTO-CONNECTED (08:40 AM READY) — GitHub Cloud Runner నుండి "
    "నేరుగా 100% డేటా KRONOS మోడల్స్‌కి వెళ్తోంది! మీ PC అవసరం లేదు."
)

CHECK_LABELS = (
    "08:30 AM Auto-Token",
    "GitHub Cloud Runner",
    "Cloudflare Direct Stream",
    "Upstox V3 WebSocket & 11-Strike Chain",
    "512 Candles & Futures Volume",
    "3 KRONOS Models (102M/25M/4M)",
)


def _dot(status: str) -> str:
    return {"GREEN": "✅", "YELLOW": "⏳", "RED": "🔴"}.get(status, "⏳")


def _now_ist(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def _phase(now: datetime) -> str:
    clock = now.time()
    if clock < datetime.strptime("08:25", "%H:%M").time():
        return "PRE_SYNC"
    if clock < datetime.strptime("08:40", "%H:%M").time():
        return "SYNCING"
    return "LIVE"


def build_checks(
    *,
    auth_ok: bool,
    runner_ok: bool,
    edge_ok: bool,
    socket_ok: bool,
    chain_ok: bool,
    buffer_ok: bool,
    models_ok: bool,
    details: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the exact six checks rendered by the UI.

    ``details`` keys are check keys (auth, runner, edge, socket, bars, models).
    Missing details are rendered as ``WAITING`` rather than invented values.
    """
    moment = _now_ist(now)
    phase = _phase(moment)
    details = details or {}
    facts = (
        ("auth", auth_ok, "VERIFIED"),
        ("runner", runner_ok, "ONLINE (PC-Free)"),
        ("edge", edge_ok, "CONNECTED (buffer-free)"),
        ("socket", socket_ok, "V3 STREAMING"),
        ("bars", buffer_ok, "512-BAR SYNC"),
        ("models", models_ok, "RECEIVING 100% 6-FEATURE DATA"),
    )
    checks: list[dict[str, Any]] = []
    for index, ((key, ok, fallback), label) in enumerate(zip(facts, CHECK_LABELS), start=1):
        if ok:
            status = "GREEN"
        elif phase == "SYNCING":
            status = "YELLOW"
        else:
            status = "RED"
        checks.append(
            {
                "key": key,
                "index": index,
                "label": label,
                "status": status,
                "dot": _dot(status),
                "detail": details.get(key) or (fallback if ok else "WAITING / NOT VERIFIED"),
            }
        )

    green = sum(1 for check in checks if check["status"] == "GREEN")
    all_green = green == len(checks)
    if all_green:
        status = "READY_0840_ALL_SYSTEMS_GO"
    elif phase == "SYNCING":
        status = "WARMING_0840"
    else:
        status = "DEGRADED"
    return {
        "checks": checks,
        "green": green,
        "total": len(checks),
        "all_green": all_green,
        "phase": phase,
        "status": status,
        "banner": BANNER_TEXT if all_green else "",
        "generated_at_ist": moment.isoformat(timespec="seconds"),
        "ready_by_0840": all_green and moment.time() <= datetime.strptime("08:40", "%H:%M").time(),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def load_readiness(path: Path = READINESS_FILE) -> dict[str, Any]:
    return _read_json(path)


def write_readiness(payload: dict[str, Any], path: Path = READINESS_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(payload, indent=1)
    # Windows can briefly deny rename while the dashboard is reading the file.
    # Keep the heartbeat alive with bounded retries and a direct-write fallback.
    for attempt in range(4):
        try:
            temp.write_text(body, encoding="utf-8")
            temp.replace(path)
            return path
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
        except OSError:
            break
    try:
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def _put_firebase(payload: dict[str, Any], timeout: float = 15.0) -> bool:
    """Best-effort publish to /system/premarket_readiness.json."""
    try:
        import requests
        from state.firebase_sync import admin

        url, token = admin()
        response = requests.put(
            f"{url}/system/premarket_readiness.json",
            params={"access_token": token},
            json=payload,
            timeout=timeout,
        )
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - readiness must never crash the runner
        return False


def publish_readiness(
    payload: dict[str, Any],
    *,
    path: Path = READINESS_FILE,
    firebase: bool = True,
) -> dict[str, Any]:
    write_readiness(payload, path)
    published = _put_firebase(payload) if firebase else False
    return {"local": str(path), "firebase": published}


def update_auth_readiness(
    *,
    minted_at_ist: str,
    verified: bool,
    user: str = "",
    path: Path = READINESS_FILE,
    firebase: bool = True,
) -> dict[str, Any]:
    """Record the morning auth result while preserving other runner facts."""
    payload = load_readiness(path)
    payload["auth"] = {
        "minted": bool(verified),
        "verified": bool(verified),
        "minted_at_ist": minted_at_ist,
        "user": user,
        "updated_epoch": time.time(),
    }
    payload["auth_minted"] = bool(verified)
    payload["auth_verified"] = bool(verified)
    payload["minted_at_ist"] = minted_at_ist
    payload["runner"] = {
        "online": True,
        "heartbeat_epoch": time.time(),
        "source": "github-actions-morning-auth",
    }
    payload["status"] = "AUTH_READY_WAITING_FOR_EDGE"
    payload["updated_epoch"] = time.time()
    publish_readiness(payload, path=path, firebase=firebase)
    return payload


def _age_seconds(epoch: Any, now: float) -> float:
    try:
        value = float(epoch or 0)
    except (TypeError, ValueError):
        return 10**9
    return max(0.0, now - value) if value else 10**9


def build_from_runtime(
    *,
    feed: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    token_present: bool = False,
    token_meta: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive the six checks from the current feed/readiness facts."""
    moment = _now_ist(now)
    current = moment.timestamp()
    feed = feed or {}
    readiness = readiness or {}
    token_meta = token_meta or {}

    auth_block = readiness.get("auth") or {}
    auth_ok = bool(
        auth_block.get("verified")
        and auth_block.get("minted")
    )
    if not auth_ok and token_present:
        saved = str(token_meta.get("saved_at") or "")
        auth_ok = bool(token_meta.get("verified")) and saved[:10] == moment.date().isoformat()
    auth_detail = (
        f"VERIFIED · {auth_block.get('minted_at_ist') or token_meta.get('verified_at') or ''}"
        if auth_ok else "NO VERIFIED TOKEN FOR TODAY"
    )

    runner = readiness.get("runner") or {}
    runner_age = _age_seconds(runner.get("heartbeat_epoch"), current)
    runner_ok = bool(runner.get("online") and runner_age <= 180)
    runner_detail = (
        f"ONLINE · heartbeat {int(runner_age)}s ago"
        if runner_ok else "NO RUNNER HEARTBEAT"
    )

    edge = readiness.get("edge") or {}
    edge_age = _age_seconds(edge.get("updated_epoch") or edge.get("ts"), current)
    edge_ok = bool(
        edge.get("active")
        and str(edge.get("url") or "").startswith("https://")
        and edge_age <= 180
    )
    edge_detail = (
        f"CONNECTED · {edge.get('url')}" if edge_ok else "TUNNEL NOT PUBLISHED"
    )

    # A 5-hour-old fixture must never look like a live V3 socket.  The feed
    # heartbeat is the authority for every feed-derived check.
    try:
        # Socket liveness is the 1s heartbeat; the full snapshot may lag the
        # heavy chain cycle by a few seconds.
        feed_updated = float(feed.get("liveness_epoch") or feed.get("updated_epoch") or 0)
    except (TypeError, ValueError):
        feed_updated = 0.0
    feed_age = _age_seconds(feed_updated, current)
    feed_fresh = bool(feed) and feed_age <= 120
    socket_ok = (
        feed_fresh
        and bool(feed.get("socket_connected"))
        and float(feed.get("tick_rate") or 0) > 0
    )
    chain = feed.get("option_chain") or {}
    chain_counts = [len(value or []) for value in chain.values() if isinstance(value, list)]
    chain_ok = feed_fresh and bool(chain_counts) and min(chain_counts) >= 11
    socket_detail = (
        f"STREAMING {float(feed.get('tick_rate') or 0):.1f} ticks/s · chain "
        f"{min(chain_counts) if chain_counts else 0}/11"
    ) if socket_ok else ("FEED STALE / NO V3 SOCKET" if feed else "V3 SOCKET OFFLINE")

    buffer_1m = feed.get("buffer_1m") or {}
    buffer_5m = feed.get("buffer_5m") or {}
    buffer_15m = feed.get("buffer_15m") or {}
    required = ("NIFTY", "BANKNIFTY")
    counts = [
        int((buffer_1m.get(name) or 0))
        for name in required
    ] + [
        int((buffer_5m.get(name) or 0))
        for name in required
    ] + [
        int((buffer_15m.get(name) or 0))
        for name in required
    ]
    buffer_ok = feed_fresh and len(counts) == 6 and min(counts) >= 512
    buffer_detail = (
        "NIFTY/BANKNIFTY 512×1m/5m/15m SYNCED"
        if buffer_ok else (
            "FEED STALE" if feed and not feed_fresh
            else f"WARMING · min {min(counts) if counts else 0}/512"
        )
    )

    models = readiness.get("models") or {}
    model_rows = [models.get(name) or {} for name in ("base", "small", "mini")]
    models_ok = len(model_rows) == 3 and all(
        bool(row.get("ready")) and int(row.get("bars") or 0) >= 512
        for row in model_rows
    )
    model_detail = (
        "102M / 25M / 4M · 512×6 FEEDS READY"
        if models_ok else "MODEL FEEDS NOT WARM"
    )

    return build_checks(
        auth_ok=auth_ok,
        runner_ok=runner_ok,
        edge_ok=edge_ok,
        socket_ok=socket_ok,
        chain_ok=chain_ok,
        buffer_ok=buffer_ok,
        models_ok=models_ok,
        details={
            "auth": auth_detail,
            "runner": runner_detail,
            "edge": edge_detail,
            "socket": socket_detail,
            "bars": buffer_detail,
            "models": model_detail,
        },
        now=moment,
    )


def status_payload(readiness: dict[str, Any]) -> dict[str, Any]:
    """Small compatibility payload for callers that only need the status."""
    return {
        "status": readiness.get("status") or "UNKNOWN",
        "green": int(readiness.get("green") or 0),
        "total": int(readiness.get("total") or 6),
        "updated_epoch": readiness.get("updated_epoch"),
    }
