"""Three-model KRONOS trio ensemble.

The trio votes; the ensemble converts raw votes into one decision with an
explicit veto. Pure Python on purpose — it must be unit-testable without torch
and it runs on every tick, so there is no allocation-heavy work here.

Veto rules (any one trips):
  * no directional agreement (weighted agreement below 0.5)
  * blended confidence below the entry floor (78.0)
  * one model strongly disagrees (>= 85 confidence against the majority)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import KRONOS_MODELS, MIN_ENTRY_CONFIDENCE

HARD_DISAGREE_CONFIDENCE = 85.0
AGREEMENT_FLOOR = 0.5


@dataclass(frozen=True)
class KronosSignal:
    """One model's opinion for the current horizon."""

    model: str
    direction: int  # +1 long, -1 short, 0 flat
    confidence: float  # 0..100
    expected_return_pct: float = 0.0
    horizon_min: int = 5

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"direction must be -1, 0 or 1 (got {self.direction})")
        if not 0.0 <= self.confidence <= 100.0:
            raise ValueError(f"confidence must be within 0..100 (got {self.confidence})")

    @property
    def is_directional(self) -> bool:
        return self.direction != 0


@dataclass(frozen=True)
class EnsembleDecision:
    direction: int
    confidence: float
    agreement: float
    vetoed: bool
    reason: str
    weights: dict[str, float] = field(default_factory=dict)
    per_model: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return (not self.vetoed) and self.direction != 0

    @property
    def label(self) -> str:
        return {-1: "SHORT", 0: "FLAT", 1: "LONG"}[self.direction]


def model_weight(name: str) -> float:
    return float(KRONOS_MODELS.get(name, {}).get("weight", 0.0))


def _veto(reason: str, agreement: float, confidence: float, per_model: dict) -> EnsembleDecision:
    return EnsembleDecision(
        direction=0,
        confidence=round(confidence, 2),
        agreement=round(agreement, 3),
        vetoed=True,
        reason=reason,
        weights={},
        per_model=per_model,
    )


def decide(
    signals: list[KronosSignal],
    min_confidence: float = MIN_ENTRY_CONFIDENCE,
) -> EnsembleDecision:
    """Blend the trio into a single veto-aware decision."""
    if not signals:
        return _veto("no model produced a signal", 0.0, 0.0, {})

    weights = {signal.model: model_weight(signal.model) for signal in signals}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        # Unknown model names: fall back to an equal-weight blend.
        weights = {signal.model: 1.0 for signal in signals}
        total_weight = float(len(signals))

    long_weight = sum(weights[s.model] for s in signals if s.direction > 0)
    short_weight = sum(weights[s.model] for s in signals if s.direction < 0)
    flat_weight = sum(weights[s.model] for s in signals if s.direction == 0)

    per_model = {
        s.model: {
            "direction": float(s.direction),
            "confidence": s.confidence,
            "weight": round(weights[s.model], 4),
        }
        for s in signals
    }

    dominant = max(long_weight, short_weight)
    if dominant == 0:
        return _veto("all models flat", 0.0, 0.0, per_model)

    direction = 1 if long_weight > short_weight else -1
    if long_weight == short_weight:
        return _veto("long/short weights tied", 0.5, 0.0, per_model)

    agreement = dominant / total_weight
    blended = sum(s.confidence * weights[s.model] for s in signals) / total_weight
    # Disagreement drags the published confidence down; unanimity keeps it.
    confidence = blended * (0.6 + 0.4 * agreement)

    against = [
        s for s in signals if s.direction == -direction and s.confidence >= HARD_DISAGREE_CONFIDENCE
    ]
    if against:
        names = ", ".join(s.model for s in against)
        return _veto(f"hard disagreement from {names}", agreement, confidence, per_model)

    if agreement < AGREEMENT_FLOOR:
        return _veto(f"agreement {agreement:.2f} below {AGREEMENT_FLOOR}", agreement, confidence, per_model)

    if confidence < min_confidence:
        return _veto(
            f"confidence {confidence:.2f} below floor {min_confidence}",
            agreement,
            confidence,
            per_model,
        )

    return EnsembleDecision(
        direction=direction,
        confidence=round(confidence, 2),
        agreement=round(agreement, 3),
        vetoed=False,
        reason="trio aligned",
        weights={name: round(weight, 3) for name, weight in weights.items()},
        per_model=per_model,
    )


def from_predictions(predictions: dict[str, dict[str, float]]) -> EnsembleDecision:
    """Convenience adapter for raw model output dicts."""
    signals = [
        KronosSignal(
            model=name,
            direction=int(round(values.get("direction", 0))),
            confidence=float(values.get("confidence", 0.0)),
            expected_return_pct=float(values.get("expected_return_pct", 0.0)),
        )
        for name, values in predictions.items()
    ]
    return decide(signals)


if __name__ == "__main__":  # pragma: no cover
    demo = [
        KronosSignal("base", 1, 84.0),
        KronosSignal("small", 1, 81.5),
        KronosSignal("mini", 1, 79.0),
    ]
    print(decide(demo))
