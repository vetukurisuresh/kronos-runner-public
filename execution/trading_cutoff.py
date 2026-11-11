"""The one IST trading-time policy shared by every entry path.

The desk stops opening new positions at exactly ``15:00:00`` IST.  Existing
positions may still be managed, but only by protective exits; anything left
open is force-closed at ``15:18:00`` IST.  Keeping this policy in one module
means the paper router, the live broker router, the expiry scanner, the order
manager and the session loop cannot drift apart.

All functions accept an optional ``now`` so the behaviour is deterministic in
tests and safe on a UTC GitHub runner.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

ENTRY_CUTOFF = time(15, 0, 0)
SQUARE_OFF_AT = time(15, 18, 0)
SESSION_END = time(15, 30, 0)

AFTER_3PM_ENTRY_CUTOFF = (
    "🚫 03:00 PM CUTOFF ACTIVE — NO NEW ORDERS ALLOWED AFTER 3:00 PM"
)
AFTER_3PM_UI_BADGE = (
    "🛑 3:00 PM ORDER CUTOFF ACTIVE — కొత్త ఆర్డర్లు నిలిపివేయబడ్డాయి "
    "(Today's Net P&L Locked)"
)
SQUARE_OFF_REASON = "AFTER_3PM_SQUARE_OFF"


class NewEntryBlocked(RuntimeError):
    """Raised when a new BUY/entry is attempted at or after 15:00 IST."""

    code = "AFTER_3PM_ENTRY_CUTOFF"


def to_ist(moment: datetime | None = None) -> datetime:
    """Coerce a datetime to IST; a naive value is treated as IST."""
    if moment is None:
        return datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def cutoff_state(now: datetime | None = None) -> dict[str, Any]:
    """Return the exact policy state for the current instant."""
    moment = to_ist(now)
    clock = moment.time()
    entries = clock < ENTRY_CUTOFF
    square_off = clock >= SQUARE_OFF_AT
    return {
        "now_ist": moment.isoformat(timespec="seconds"),
        "entry_cutoff_ist": ENTRY_CUTOFF.strftime("%H:%M:%S"),
        "square_off_ist": SQUARE_OFF_AT.strftime("%H:%M:%S"),
        "new_entries_allowed": entries,
        "exit_only": not entries,
        "square_off_due": square_off,
        "reason": "" if entries else AFTER_3PM_ENTRY_CUTOFF,
        "badge": "" if entries else AFTER_3PM_UI_BADGE,
    }


def require_new_entry(now: datetime | None = None) -> None:
    """Raise the canonical cutoff error for a new entry at/after 15:00."""
    if not cutoff_state(now)["new_entries_allowed"]:
        raise NewEntryBlocked(AFTER_3PM_ENTRY_CUTOFF)


def new_entries_allowed(now: datetime | None = None) -> bool:
    """Convenience predicate used by strategy scanners."""
    return bool(cutoff_state(now)["new_entries_allowed"])


def square_off_due(now: datetime | None = None) -> bool:
    """True from 15:18 IST onward, when every intraday position must be flat."""
    return bool(cutoff_state(now)["square_off_due"])
