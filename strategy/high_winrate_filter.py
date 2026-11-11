"""80% confluence gate.

The engine only fires when at least four of five independent checks agree, i.e.
a score of 80/100. Each check is worth 20 points and is explicit, so a rejected
trade can always be explained in the narration log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.settings import CONFLUENCE_THRESHOLD, HEAVY_WEIGHT_SYMBOLS, MIN_ENTRY_CONFIDENCE

CHECK_WEIGHT = 20.0
OI_VELOCITY_FLOOR = 12.0  # percent change in open interest per 5m bucket


@dataclass(frozen=True)
class ConfluenceCheck:
    name: str
    passed: bool
    detail: str
    weight: float = CHECK_WEIGHT


@dataclass
class ConfluenceReport:
    score: float
    threshold: float
    checks: list[ConfluenceCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold

    @property
    def failed_checks(self) -> list[str]:
        return [check.name for check in self.checks if not check.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "threshold": self.threshold,
            "passed": self.passed,
            "failed": self.failed_checks,
        }


def evaluate(
    *,
    kronos_confidence: float,
    signal_direction: int,
    smc_bias: int,
    oi_velocity_pct: float,
    heavy_vwap_aligned: int,
    regime_light: str,
    threshold: float = CONFLUENCE_THRESHOLD,
) -> ConfluenceReport:
    """Score the five confluence pillars.

    ``heavy_vwap_aligned`` is the count (0-5) of heavy-weight constituents whose
    price sits on the same side of VWAP as the intended trade.
    """
    light = regime_light.upper()
    heavy_total = len(HEAVY_WEIGHT_SYMBOLS)
    checks = [
        ConfluenceCheck(
            "kronos_confidence",
            kronos_confidence >= MIN_ENTRY_CONFIDENCE,
            f"{kronos_confidence:.1f} vs floor {MIN_ENTRY_CONFIDENCE:.0f}",
        ),
        ConfluenceCheck(
            "smc_alignment",
            signal_direction != 0 and smc_bias == signal_direction,
            f"signal={signal_direction} smc_bias={smc_bias}",
        ),
        ConfluenceCheck(
            "oi_velocity",
            oi_velocity_pct >= OI_VELOCITY_FLOOR,
            f"{oi_velocity_pct:.1f}% vs floor {OI_VELOCITY_FLOOR:.0f}%",
        ),
        ConfluenceCheck(
            "heavy_vwap_sync",
            heavy_vwap_aligned >= (heavy_total + 1) // 2,
            f"{heavy_vwap_aligned}/{heavy_total} heavyweights aligned",
        ),
        ConfluenceCheck(
            "regime_not_red",
            light != "RED",
            f"regime={light}",
        ),
    ]

    score = sum(check.weight for check in checks if check.passed)
    return ConfluenceReport(score=score, threshold=threshold, checks=checks)


def explain(report: ConfluenceReport) -> str:
    """One-line narration string."""
    if report.passed:
        return f"confluence {report.score:.0f}/100 - all pillars aligned"
    return f"confluence {report.score:.0f}/100 - blocked by: {', '.join(report.failed_checks)}"
