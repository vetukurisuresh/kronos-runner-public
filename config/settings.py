"""KRONOS central configuration.

Loads environment variables (optionally through python-dotenv), exposes trading
constants, and provides the session-window guards every strategy module relies
on. Importing this module must never touch the network or require a heavy
dependency: it stays importable on a bare runner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

try:  # optional: the system still works with plain exported env vars
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:  # pragma: no cover - depends on local .env
    load_dotenv(REPO_ROOT / ".env")

# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

SESSION_START = time(9, 15)
SESSION_END = time(15, 30)
ENTRY_CUTOFF = time(14, 45)
EXPIRY_ENTRY_CUTOFF = time(14, 0)

# --------------------------------------------------------------------------- #
# Market universe
# --------------------------------------------------------------------------- #

INDEX_SYMBOLS = {"NIFTY": "NIFTY 50", "BANKNIFTY": "BANK NIFTY"}
HEAVY_WEIGHT_SYMBOLS = ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS")

# --------------------------------------------------------------------------- #
# Model trio
# --------------------------------------------------------------------------- #

KRONOS_MODELS = {
    "base": {"repo_id": "NeoQuasar/Kronos-base", "params_m": 102.3, "weight": 0.50},
    "small": {"repo_id": "NeoQuasar/Kronos-small", "params_m": 24.7, "weight": 0.30},
    "mini": {"repo_id": "NeoQuasar/Kronos-mini", "params_m": 4.1, "weight": 0.20},
}

MIN_ENTRY_CONFIDENCE = 78.0
CONFLUENCE_THRESHOLD = 80.0
BREAKEVEN_R = 0.8

# --------------------------------------------------------------------------- #
# Money management
# --------------------------------------------------------------------------- #

DEPLOYMENT_PCT = 0.35
RECOVERY_MULTIPLIER = 1.25
HOUSE_VAULT_PCT = 0.70
MAX_TRADES_BY_LIGHT = {"RED": 0, "YELLOW": 2, "GREEN": 5}

DATA_DIR = REPO_ROOT / "state" / "data"
LOG_DIR = REPO_ROOT / "state" / "logs"
WEIGHTS_DIR = REPO_ROOT / "model" / "weights"
KILL_SWITCH_FILE = REPO_ROOT / "state" / "KILL_SWITCH"


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Session guards
# --------------------------------------------------------------------------- #


def to_ist(moment: datetime | None = None) -> datetime:
    """Coerce a naive or aware datetime into IST (naive is assumed to be IST)."""
    if moment is None:
        return datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def in_session_window(moment: datetime | None = None) -> bool:
    """True only Mon-Fri between 09:15 and 15:30 IST."""
    now = to_ist(moment)
    if now.weekday() >= 5:
        return False
    return SESSION_START <= now.time() <= SESSION_END


def entry_allowed(moment: datetime | None = None, expiry_day: bool = False) -> bool:
    """Entries stop before the close so positions are never trapped overnight."""
    now = to_ist(moment)
    if not in_session_window(now):
        return False
    cutoff = EXPIRY_ENTRY_CUTOFF if expiry_day else ENTRY_CUTOFF
    return now.time() <= cutoff


def session_phase(moment: datetime | None = None) -> str:
    """Coarse phase label used by logs and the regime classifier."""
    now = to_ist(moment)
    if not in_session_window(now):
        return "CLOSED"
    clock = now.time()
    if clock <= time(10, 0):
        return "OPENING_DRIVE"
    if clock <= time(12, 0):
        return "PRIMARY_TREND"
    if clock <= time(14, 0):
        return "AFTERNOON"
    return "POWER_HOUR"


def kill_switch_enabled() -> bool:
    """Master stop. Env var wins; a local flag file also trips the switch."""
    if env_flag("KRONOS_KILL_SWITCH"):
        return True
    return KILL_SWITCH_FILE.exists()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings. Every field is env-overridable."""

    mode: str = "paper"
    live_trading: bool = False
    capital: float = 100_000.0
    max_daily_loss_pct: float = 0.03
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_redirect_uri: str = "http://localhost:8501"
    upstox_access_token: str = ""
    firebase_db_url: str = ""
    firebase_credentials: str = ""
    dashboard_port: int = 8501

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("KRONOS_MODE", "paper").strip().lower() or "paper"
        live = env_flag("KRONOS_LIVE") and mode == "live"
        return cls(
            mode="live" if live else mode,
            live_trading=live,
            capital=env_float("KRONOS_CAPITAL", 100_000.0),
            max_daily_loss_pct=env_float("KRONOS_MAX_DAILY_LOSS_PCT", 0.03),
            upstox_api_key=os.environ.get("UPSTOX_API_KEY", ""),
            upstox_api_secret=os.environ.get("UPSTOX_API_SECRET", ""),
            upstox_redirect_uri=os.environ.get("UPSTOX_REDIRECT_URI", "http://localhost:8501"),
            upstox_access_token=os.environ.get("UPSTOX_ACCESS_TOKEN", ""),
            firebase_db_url=os.environ.get("FIREBASE_DB_URL", ""),
            firebase_credentials=os.environ.get("FIREBASE_CREDENTIALS", ""),
            dashboard_port=env_int("KRONOS_DASHBOARD_PORT", 8501),
        )

    def redacted(self) -> dict[str, object]:
        """Safe representation for logs — secrets never leave the process."""
        payload = {field: getattr(self, field) for field in self.__dataclass_fields__}
        for key in list(payload):
            if any(token in key for token in ("secret", "token", "key", "credentials")):
                payload[key] = "***" if payload[key] else ""
        return payload


def get_settings() -> Settings:
    return Settings.from_env()
