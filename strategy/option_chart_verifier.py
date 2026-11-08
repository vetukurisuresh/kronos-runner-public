"""Option-chart verification for entries.

The index forecast only says *direction*. This module checks whether the option
contract itself is tradeable right now:

* premium above VWAP (``atp``) for calls, below it for puts
* option swing-low stop within the rupee budget (``<= Rs 9-10``, ``<= Rs 8`` on
  an A+ recovery setup)
* delta at least ``0.45`` so the contract actually tracks the index
* 5-minute OI unwinding velocity (writers covering = fuel for the move)

Pure functions: no clock, no network, no broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_SL_POINTS = 10.0
MICRO_SL_POINTS = 8.0
MIN_DELTA = 0.45
MIN_OI_UNWIND_PCT = 6.0


@dataclass(frozen=True)
class OptionSnapshot:
    ltp: float
    atp: float = 0.0  # option VWAP from eFeedDetails.atp
    swing_low: float = 0.0
    delta: float = 0.0
    iv: float = 0.0
    oi: float = 0.0
    prev_oi: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0

    @property
    def sl_points(self) -> float:
        """Distance from premium to the option swing low (0 when unknown)."""
        if self.swing_low <= 0 or self.swing_low >= self.ltp:
            return 0.0
        return round(self.ltp - self.swing_low, 2)

    @property
    def oi_unwind_pct(self) -> float:
        if self.prev_oi <= 0:
            return 0.0
        return round((self.prev_oi - self.oi) / self.prev_oi * 100.0, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ltp": round(self.ltp, 2),
            "atp": round(self.atp, 2),
            "swing_low": round(self.swing_low, 2),
            "sl_points": self.sl_points,
            "delta": round(self.delta, 3),
            "iv": round(self.iv, 2),
            "oi": int(self.oi),
            "oi_unwind_pct": self.oi_unwind_pct,
            "best_bid": round(self.best_bid, 2),
            "best_ask": round(self.best_ask, 2),
        }


@dataclass
class OptionVerdict:
    ok: bool
    side: str
    score: float
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    sl_points: float = 0.0
    telugu: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "side": self.side,
            "score": round(self.score, 1),
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "sl_points": round(self.sl_points, 2),
            "telugu": self.telugu,
        }


def vwap_aligned(snapshot: OptionSnapshot, side: str) -> bool:
    """Calls must hold above ATP, puts below it."""
    if snapshot.atp <= 0:
        return True  # no VWAP published yet (opening minutes) - do not block
    return snapshot.ltp >= snapshot.atp if side.upper() == "CE" else snapshot.ltp <= snapshot.atp


def verify_option_chart(
    snapshot: OptionSnapshot,
    side: str = "CE",
    *,
    recovery: bool = False,
    require_oi_unwind: bool = False,
) -> OptionVerdict:
    """Decide whether the option chart permits an entry right now."""
    side = side.upper()
    if side not in {"CE", "PE"}:
        raise ValueError("side must be CE or PE")

    budget = MICRO_SL_POINTS if recovery else MAX_SL_POINTS
    reasons: list[str] = []
    blockers: list[str] = []
    score = 50.0

    if snapshot.ltp <= 0:
        blockers.append("no premium")
    if snapshot.atp > 0:
        if vwap_aligned(snapshot, side):
            reasons.append(f"premium {'above' if side == 'CE' else 'below'} VWAP {snapshot.atp:.2f}")
            score += 12.0
        else:
            blockers.append(f"premium on the wrong side of VWAP {snapshot.atp:.2f}")

    sl = snapshot.sl_points
    if sl <= 0:
        blockers.append("option swing low unknown")
    elif sl <= budget:
        reasons.append(f"swing-low SL {sl:.2f} <= Rs {budget:.0f}")
        score += 14.0
    else:
        blockers.append(f"swing-low SL {sl:.2f} exceeds Rs {budget:.0f}")

    if snapshot.delta >= MIN_DELTA:
        reasons.append(f"delta {snapshot.delta:.2f} >= {MIN_DELTA}")
        score += 12.0
    elif snapshot.delta > 0:
        blockers.append(f"delta {snapshot.delta:.2f} < {MIN_DELTA}")

    unwind = snapshot.oi_unwind_pct
    if unwind >= MIN_OI_UNWIND_PCT:
        reasons.append(f"5m OI unwinding {unwind:.1f}% >= {MIN_OI_UNWIND_PCT:.0f}%")
        score += 12.0
    elif unwind < 0:
        reasons.append(f"OI building {abs(unwind):.1f}% (writers defending)")
    elif require_oi_unwind:
        blockers.append(f"OI unwinding {unwind:.1f}% below {MIN_OI_UNWIND_PCT:.0f}%")

    ok = not blockers
    telugu = (
        f"\u2705 {side} \u0c1a\u0c3e\u0c30\u0c4d\u0c1f\u0c4d \u0c15\u0c4d\u0c32\u0c40\u0c28\u0c4d \u2014 SL {sl:.1f}, "
        f"delta {snapshot.delta:.2f}. \u0c0e\u0c02\u0c1f\u0c4d\u0c30\u0c40 \u0c05\u0c02\u0c26\u0c41\u0c2c\u0c3e\u0c1f\u0c41\u0c32\u0c4b \u0c09\u0c02\u0c26\u0c3f."
        if ok
        else f"\u26a0\ufe0f {side} \u0c38\u0c46\u0c1f\u0c4d\u0c05\u0c2a\u0c4d \u0c30\u0c47\u0c26\u0c41 \u0c15\u0c3e\u0c26\u0c41 \u2014 {blockers[0]}."
    )
    return OptionVerdict(
        ok=ok,
        side=side,
        score=min(100.0, score),
        reasons=reasons,
        blockers=blockers,
        sl_points=sl,
        telugu=telugu,
    )


def oi_unwinding_pct(current_oi: float, previous_oi: float) -> float:
    """Percent of open interest unwound over a window (negative = building)."""
    if previous_oi <= 0:
        return 0.0
    return round((previous_oi - current_oi) / previous_oi * 100.0, 2)


def straddle_decay(ce_atp: float, ce_ltp: float, pe_atp: float, pe_ltp: float) -> bool:
    """True when both legs trade below their VWAP - premium sellers in control."""
    if min(ce_atp, pe_atp) <= 0:
        return False
    return ce_ltp < ce_atp and pe_ltp < pe_atp
