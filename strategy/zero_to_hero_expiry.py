"""Expiry-day gamma blast scanner ("zero to hero").

On expiry Thursdays, cheap out-of-the-money options occasionally reprice in
minutes when open interest and premium velocity spike together. This module
ranks that behaviour and hands the top candidates to the execution layer.

It is deliberately conservative: nothing is returned outside the expiry day or
after the expiry entry cutoff, and every candidate must clear BOTH a premium
velocity and an OI velocity floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from config.settings import entry_allowed, to_ist
from execution.trading_cutoff import new_entries_allowed
from market.upstox_expired_options import is_expiry_day

PREMIUM_VELOCITY_FLOOR = 25.0  # percent move in premium
OI_VELOCITY_FLOOR = 10.0  # percent change in open interest
MIN_PREMIUM = 5.0
MAX_SLOTS = 5


@dataclass(frozen=True)
class GammaCandidate:
    symbol: str
    strike: int
    option_type: str
    premium: float
    premium_velocity_pct: float
    oi_velocity_pct: float
    volume: float
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strike": self.strike,
            "type": self.option_type,
            "premium": round(self.premium, 2),
            "prem_vel": round(self.premium_velocity_pct, 1),
            "oi_vel": round(self.oi_velocity_pct, 1),
            "score": round(self.score, 1),
        }


def _velocity(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous * 100.0


def _score(premium_velocity: float, oi_velocity: float, volume: float) -> float:
    volume_factor = min(volume / 100_000.0, 1.0) * 100.0
    return (premium_velocity * 0.5) + (oi_velocity * 0.3) + (volume_factor * 0.2)


def scan(
    chain: Iterable[dict[str, Any]],
    now: datetime | None = None,
    expiry_today: bool | None = None,
    top_n: int = MAX_SLOTS,
) -> list[GammaCandidate]:
    """Rank gamma-blast candidates, or return [] when the setup is not valid."""
    moment = to_ist(now)
    expiry = is_expiry_day(moment.date()) if expiry_today is None else expiry_today
    if (not expiry or not entry_allowed(moment, expiry_day=True)
            or not new_entries_allowed(moment)):
        return []

    candidates: list[GammaCandidate] = []
    for row in chain:
        premium = float(row.get("ltp", 0) or 0)
        previous = float(row.get("prev_ltp", 0) or 0)
        if premium < MIN_PREMIUM or previous <= 0:
            continue

        premium_velocity = _velocity(premium, previous)
        oi_velocity = _velocity(float(row.get("oi", 0) or 0), float(row.get("prev_oi", 0) or 0))
        if premium_velocity < PREMIUM_VELOCITY_FLOOR or oi_velocity < OI_VELOCITY_FLOOR:
            continue

        volume = float(row.get("volume", 0) or 0)
        candidates.append(
            GammaCandidate(
                symbol=str(row.get("symbol", "")),
                strike=int(row.get("strike", 0) or 0),
                option_type=str(row.get("option_type", "CE")).upper(),
                premium=premium,
                premium_velocity_pct=premium_velocity,
                oi_velocity_pct=oi_velocity,
                volume=volume,
                score=_score(premium_velocity, oi_velocity, volume),
            )
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[: max(int(top_n), 1)]


def describe(candidates: list[GammaCandidate]) -> str:
    if not candidates:
        return "no gamma blast setup"
    best = candidates[0]
    return (
        f"{len(candidates)} candidates - best {best.symbol} "
        f"premium +{best.premium_velocity_pct:.0f}% OI +{best.oi_velocity_pct:.0f}%"
    )
