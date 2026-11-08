"""Four-gear dynamic trailing stop with an 0.8R breakeven lock.

Gear 1 (<0.8R)   : keep the original stop, give the trade room.
Gear 2 (0.8R-1.5R): move to breakeven - the trade can no longer lose money.
Gear 3 (1.5R-2.5R): lock +0.8R.
Gear 4 (>2.5R)   : trail 0.5R behind the peak, never backwards.

The stop only ever moves in the profitable direction; that invariant is what
makes this safe to call on every tick.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from config.settings import BREAKEVEN_R

GEAR_LABELS = {
    1: "GEAR_1_INITIAL",
    2: "GEAR_2_BREAKEVEN",
    3: "GEAR_3_LOCK_0.8R",
    4: "GEAR_4_TRAIL_0.5R",
}


@dataclass(frozen=True)
class TrailingState:
    direction: int  # +1 long, -1 short
    entry: float
    initial_sl: float
    stop: float
    peak: float
    gear: int = 1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.initial_sl)

    @property
    def breakeven_locked(self) -> bool:
        if self.risk == 0:
            return True
        return (self.stop - self.entry) * self.direction >= 0 and self.gear >= 2

    def r_multiple(self, price: float) -> float:
        if self.risk == 0:
            return 0.0
        return (price - self.entry) * self.direction / self.risk

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entry": round(self.entry, 2),
            "stop": round(self.stop, 2),
            "peak": round(self.peak, 2),
            "gear": self.gear,
            "gear_label": GEAR_LABELS.get(self.gear, "?"),
            "risk": round(self.risk, 2),
            "breakeven_locked": self.breakeven_locked,
        }


def create(entry: float, initial_sl: float, direction: int) -> TrailingState:
    if direction not in (-1, 1):
        raise ValueError("direction must be +1 (long) or -1 (short)")
    if (direction > 0 and initial_sl >= entry) or (direction < 0 and initial_sl <= entry):
        raise ValueError("initial stop must sit on the losing side of the entry")
    return TrailingState(
        direction=direction,
        entry=entry,
        initial_sl=initial_sl,
        stop=initial_sl,
        peak=entry,
        gear=1,
    )


def _tighter(current: float, candidate: float, direction: int) -> float:
    """Pick whichever stop is more protective for the trade direction."""
    if direction > 0:
        return max(current, candidate)
    return min(current, candidate)


def update(state: TrailingState, price: float) -> TrailingState:
    """Advance the trail with the latest price. Never loosens the stop."""
    if price <= 0 or state.risk == 0:
        return state

    peak = max(state.peak, price) if state.direction > 0 else min(state.peak, price)
    r_value = (price - state.entry) * state.direction / state.risk
    gain = (peak - state.entry) * state.direction / state.risk

    if gain < BREAKEVEN_R:
        gear, candidate = 1, state.initial_sl
    elif gain < 1.5:
        gear, candidate = 2, state.entry
    elif gain < 2.5:
        gear, candidate = 3, state.entry + state.direction * BREAKEVEN_R * state.risk
    else:
        gear, candidate = 4, state.peak - state.direction * 0.5 * state.risk

    # ``_tighter`` already guarantees the stop only ever moves in the
    # protective direction. Clamping it to the live price here used to drag the
    # stop down on every falling tick, turning a 9.5-point risk into a 30-point
    # loss - the stop must stay put until it is hit.
    stop = _tighter(state.stop, candidate, state.direction)
    return replace(state, stop=stop, peak=peak, gear=gear)
