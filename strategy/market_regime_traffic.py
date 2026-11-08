"""Three-light market-regime traffic controller.

RED    -> 0 trades (capital preservation)
YELLOW -> at most 2 trades (selective)
GREEN  -> up to 5 trades (full deployment)

The classifier is a pure function of observable inputs so it can be replayed
against history and proven in tests. Every result carries the reasons for its
light, which is what the dashboard renders in Telugu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config.settings import MAX_TRADES_BY_LIGHT, in_session_window, to_ist

VIX_RED = 22.0
VIX_GREEN = 15.0
ATR_RED = 1.2
ATR_GREEN = 0.7
TREND_RED = 0.25
TREND_GREEN = 0.60
LATE_DAY_MINUTES = 20


@dataclass(frozen=True)
class RegimeInput:
    india_vix: float
    atr_pct: float
    trend_strength: float  # 0..1
    oi_shift_pct: float = 0.0
    minutes_to_close: int = 375
    phase: str = "PRIMARY_TREND"


@dataclass
class RegimeState:
    light: str
    max_trades: int
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def tradeable(self) -> bool:
        return self.max_trades > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "light": self.light,
            "max_trades": self.max_trades,
            "score": round(self.score, 1),
            "reasons": list(self.reasons),
        }


def classify(data: RegimeInput, now: datetime | None = None) -> RegimeState:
    """Map market conditions onto the three-light controller."""
    moment = to_ist(now)
    reasons: list[str] = []

    if not in_session_window(moment):
        return RegimeState("RED", 0, 0.0, ["outside 09:15-15:30 IST session"])

    red_hits = 0
    if data.india_vix >= VIX_RED:
        red_hits += 1
        reasons.append(f"India VIX {data.india_vix:.1f} >= {VIX_RED}")
    if data.atr_pct >= ATR_RED:
        red_hits += 1
        reasons.append(f"ATR {data.atr_pct:.2f}% >= {ATR_RED}%")
    if data.trend_strength < TREND_RED:
        red_hits += 1
        reasons.append(f"trend strength {data.trend_strength:.2f} < {TREND_RED}")
    if data.minutes_to_close <= LATE_DAY_MINUTES:
        red_hits += 1
        reasons.append(f"only {data.minutes_to_close} min to close")

    if red_hits:
        return RegimeState("RED", MAX_TRADES_BY_LIGHT["RED"], 0.0, reasons)

    green = (
        data.india_vix <= VIX_GREEN
        and data.atr_pct <= ATR_GREEN
        and data.trend_strength >= TREND_GREEN
        and abs(data.oi_shift_pct) >= 2.0
    )
    if green:
        reasons.append(
            f"calm VIX {data.india_vix:.1f}, ATR {data.atr_pct:.2f}%, "
            f"trend {data.trend_strength:.2f}"
        )
        score = min(100.0, 60.0 + data.trend_strength * 40.0)
        return RegimeState("GREEN", MAX_TRADES_BY_LIGHT["GREEN"], score, reasons)

    reasons.append(
        f"mixed tape: VIX {data.india_vix:.1f}, ATR {data.atr_pct:.2f}%, "
        f"trend {data.trend_strength:.2f}"
    )
    score = 30.0 + data.trend_strength * 30.0
    return RegimeState("YELLOW", MAX_TRADES_BY_LIGHT["YELLOW"], score, reasons)


def trades_remaining(light: str, executed: int) -> int:
    """How many more entries the current light permits."""
    cap = MAX_TRADES_BY_LIGHT.get(light.upper(), 0)
    return max(0, cap - max(int(executed), 0))


def can_enter(light: str, executed: int) -> bool:
    return trades_remaining(light, executed) > 0


# --------------------------------------------------------------------------- #
# KRONOS fan classifier - "daily market character"
# --------------------------------------------------------------------------- #

FAN_RED_BAND = (38.0, 62.0)
FAN_YELLOW = 78.0
FAN_GREEN = 83.0
GREEN_CONE_TIGHT_PCT = 1.2
GREEN_HEAVY_ALIGN = 4
GREEN_OI_UNWIND_PCT = 6.0
OPENING_RANGE_RATIO = 0.45
INSIDE_BAR_OVERLAP = 0.65

RED_TELUGU = (
    "\U0001F6AB \u0c08\u0c30\u0c4b\u0c1c\u0c41 \u0c38\u0c48\u0c21\u0c4d\u0c35\u0c47\u0c38\u0c4d / "
    "\u0c06\u0c2a\u0c30\u0c47\u0c1f\u0c30\u0c4d \u0c1f\u0c4d\u0c30\u0c3e\u0c2a\u0c4d \u0c2e\u0c3e\u0c30\u0c4d\u0c15\u0c46\u0c1f\u0c4d "
    "\u2014 \u0c15\u0c4d\u0c2f\u0c3e\u0c2a\u0c3f\u0c1f\u0c32\u0c4d \u0c30\u0c15\u0c4d\u0c37\u0c23 \u0c15\u0c4b\u0c38\u0c02 "
    "NO TRADE MODE (0 \u0c1f\u0c4d\u0c30\u0c47\u0c21\u0c4d\u0c38\u0c4d)."
)
YELLOW_TELUGU = (
    "\U0001F7E1 \u0c38\u0c4d\u0c28\u0c48\u0c2a\u0c30\u0c4d \u0c21\u0c47 \u2014 \u0c17\u0c30\u0c3f\u0c37\u0c4d\u0c1f\u0c02\u0c17\u0c3e 2 "
    "\u0c1f\u0c4d\u0c30\u0c47\u0c21\u0c4d\u0c38\u0c4d, 78+ \u0c15\u0c28\u0c4d\u0c35\u0c3f\u0c15\u0c4d\u0c37\u0c28\u0c4d \u0c09\u0c28\u0c4d\u0c28\u0c2a\u0c4d\u0c2a\u0c41\u0c21\u0c47 \u0c0e\u0c02\u0c1f\u0c4d\u0c30\u0c40."
)
GREEN_TELUGU = (
    "\U0001F7E2 \u0c38\u0c42\u0c2a\u0c30\u0c4d \u0c1f\u0c4d\u0c30\u0c46\u0c02\u0c21\u0c4d \u0c21\u0c47 \u2014 \u0c39\u0c4c\u0c38\u0c4d \u0c2e\u0c28\u0c40\u0c24\u0c4b "
    "\u0c17\u0c30\u0c3f\u0c37\u0c4d\u0c1f\u0c02\u0c17\u0c3e 5 \u0c1f\u0c4d\u0c30\u0c47\u0c21\u0c4d\u0c38\u0c4d."
)


@dataclass(frozen=True)
class FanRegimeInput:
    """Observables the 5-minute master scan feeds into the classifier."""

    paths_up: float
    dispersion_pct: float = 99.0  # (p90 - p10) / spot * 100
    heavyweights_aligned: int = 0  # 0..5 above/below VWAP
    oi_unwinding_pct: float = 0.0
    inside_bar_overlap: float = 0.0  # share of the last 8 bars inside the prior
    opening_range_ratio: float = 1.0  # opening 45m / 5-day average range
    straddle_below_vwap: bool = False
    minutes_to_close: int = 375


@dataclass
class FanRegimeState(RegimeState):
    telugu: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        payload["telugu"] = self.telugu
        return payload


def classify_fan(data: FanRegimeInput, now: datetime | None = None) -> FanRegimeState:
    """Map the KRONOS fan + market character onto the three lights."""
    moment = to_ist(now)
    if not in_session_window(moment):
        return FanRegimeState("RED", 0, 0.0, ["outside 09:15-15:30 IST session"], RED_TELUGU)

    paths = float(data.paths_up)
    if data.minutes_to_close <= LATE_DAY_MINUTES:
        return FanRegimeState(
            "RED", 0, 0.0, [f"only {data.minutes_to_close} min to close"], RED_TELUGU
        )

    reasons: list[str] = []
    low, high = FAN_RED_BAND
    entropy = low <= paths <= high
    trap = (
        data.inside_bar_overlap >= INSIDE_BAR_OVERLAP
        and data.opening_range_ratio < OPENING_RANGE_RATIO
        and data.straddle_below_vwap
    )
    if entropy:
        reasons.append(f"fan paths_up {paths:.0f} inside {low:.0f}-{high:.0f} (high entropy)")
    if trap:
        reasons.append(
            f"inside-bar overlap {data.inside_bar_overlap:.0%}, opening range "
            f"{data.opening_range_ratio:.2f}x, both ATM legs below VWAP"
        )
    if entropy or trap:
        return FanRegimeState("RED", MAX_TRADES_BY_LIGHT["RED"], 0.0, reasons, RED_TELUGU)

    directional = paths >= FAN_GREEN or paths <= 100.0 - FAN_GREEN
    cone_tight = data.dispersion_pct <= GREEN_CONE_TIGHT_PCT
    aligned = data.heavyweights_aligned >= GREEN_HEAVY_ALIGN
    unwinding = abs(data.oi_unwinding_pct) >= GREEN_OI_UNWIND_PCT
    if directional and cone_tight and aligned and unwinding:
        reasons.extend(
            [
                f"fan paths_up {paths:.0f} ">=" {FAN_GREEN:.0f}",
                f"cone {data.dispersion_pct:.2f}% <= {GREEN_CONE_TIGHT_PCT}%",
                f"{data.heavyweights_aligned}/5 heavyweights aligned",
                f"OI unwinding {data.oi_unwinding_pct:.1f}% >= {GREEN_OI_UNWIND_PCT:.0f}%",
            ]
        )
        score = min(100.0, 70.0 + (abs(paths - 50.0) - 33.0) * 0.9)
        return FanRegimeState("GREEN", MAX_TRADES_BY_LIGHT["GREEN"], score, reasons, GREEN_TELUGU)

    sniper = paths >= FAN_YELLOW or paths <= 100.0 - FAN_YELLOW
    if sniper:
        reasons.append(f"fan paths_up {paths:.0f} ">=" {FAN_YELLOW:.0f} with clean structure")
        return FanRegimeState("YELLOW", MAX_TRADES_BY_LIGHT["YELLOW"], 62.0, reasons, YELLOW_TELUGU)

    reasons.append(f"mixed fan {paths:.0f} - no A+ alignment, stay selective")
    return FanRegimeState("YELLOW", MAX_TRADES_BY_LIGHT["YELLOW"], 45.0, reasons, YELLOW_TELUGU)
