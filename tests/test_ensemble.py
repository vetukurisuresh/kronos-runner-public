"""Ensemble verification suite — designed to run on the cloud runner.

Groups
------
* **voting core**   — the original weighted vote/veto contract (no torch).
* **trio gates**    — the 78/22 path-vote confluence rule (no torch).
* **CHECK 1**       — all three weight dirs + tokenizer exist and load into RAM.
* **CHECK 2**       — 512-bar dummy Nifty OHLCV through all three, timed.
* **CHECK 3**       — conflicting vs aligned signals suppress/trigger correctly.

Every model-dependent test requires torch and the downloaded weights; they skip
cleanly on a laptop so the suite never tries to download anything locally.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config.settings import MIN_ENTRY_CONFIDENCE, WEIGHTS_DIR  # noqa: E402
from model.kronos_ensemble import (  # noqa: E402
    HARD_DISAGREE_CONFIDENCE,
    KronosSignal,
    decide,
    from_predictions,
)

from agent.kronos_ensemble import (  # noqa: E402
    BASE_BEAR_THRESHOLD,
    BASE_BULL_THRESHOLD,
    CONE_TIGHT_RATIO,
    KronosTrioEnsemble,
    ModelForecast,
    _collapse_path_batch,
    decide_trio,
    ensemble_available,
    quantiles,
)

HAS_TORCH = importlib.util.find_spec("torch") is not None


def test_forecast_batch_collapses_extra_leading_axes() -> None:
    import numpy as np

    batch = np.zeros((1, 2, 1, 3, 5), dtype=float)
    collapsed = _collapse_path_batch(batch)
    assert collapsed.shape == (2, 3, 5)


def test_forecast_batch_rejects_missing_ohlcv_features() -> None:
    import numpy as np

    with pytest.raises(ValueError, match="OHLCV"):
        _collapse_path_batch(np.zeros((2, 3, 4), dtype=float))
HAS_PANDAS = importlib.util.find_spec("pandas") is not None
ROLES = ("base", "small", "mini")
TOKENIZERS = ("Kronos-Tokenizer-base", "Kronos-Tokenizer-2k")


def _weights_present() -> bool:
    # Size-checked, not existence-only: a partial safetensors download must not
    # make the integration tests attempt to load a corrupt checkpoint.
    return ensemble_available(WEIGHTS_DIR)[0]


WEIGHTS_PRESENT = _weights_present()
requires_models = pytest.mark.skipif(
    not (HAS_TORCH and HAS_PANDAS and WEIGHTS_PRESENT),
    reason="requires torch, pandas and cloud-downloaded weights",
)


# --------------------------------------------------------------------------- #
# voting core (original contract)
# --------------------------------------------------------------------------- #


def test_unanimous_trio_is_actionable() -> None:
    decision = decide(
        [
            KronosSignal("base", 1, 84.0),
            KronosSignal("small", 1, 81.5),
            KronosSignal("mini", 1, 79.0),
        ]
    )
    assert decision.actionable is True
    assert decision.direction == 1
    assert decision.agreement == pytest.approx(1.0)
    assert decision.confidence == pytest.approx(82.3, abs=0.1)
    assert decision.confidence >= MIN_ENTRY_CONFIDENCE
    assert decision.label == "LONG"


def test_short_unanimity_flips_direction() -> None:
    decision = decide(
        [
            KronosSignal("base", -1, 88.0),
            KronosSignal("small", -1, 86.0),
            KronosSignal("mini", -1, 85.0),
        ]
    )
    assert decision.actionable is True
    assert decision.direction == -1
    assert decision.label == "SHORT"


def test_hard_disagreement_vetoes() -> None:
    decision = decide(
        [
            KronosSignal("base", 1, 85.0),
            KronosSignal("small", 1, 80.0),
            KronosSignal("mini", -1, HARD_DISAGREE_CONFIDENCE),
        ]
    )
    assert decision.vetoed is True
    assert "hard disagreement" in decision.reason
    assert decision.direction == 0


def test_mild_disagreement_fails_confidence_floor() -> None:
    decision = decide(
        [
            KronosSignal("base", 1, 90.0),
            KronosSignal("small", -1, 70.0),
            KronosSignal("mini", 0, 60.0),
        ]
    )
    assert decision.vetoed is True
    assert decision.actionable is False
    assert "confidence" in decision.reason


def test_empty_signal_set_vetoes() -> None:
    decision = decide([])
    assert decision.vetoed is True
    assert decision.direction == 0


def test_all_flat_vetoes() -> None:
    decision = decide([KronosSignal("base", 0, 90.0), KronosSignal("mini", 0, 88.0)])
    assert decision.vetoed is True
    assert "flat" in decision.reason


def test_unknown_models_fall_back_to_equal_weights() -> None:
    decision = decide([KronosSignal("x", 1, 80.0), KronosSignal("y", 1, 80.0)])
    assert decision.actionable is True
    assert decision.confidence == pytest.approx(80.0, abs=0.01)


def test_weights_are_published_for_known_models() -> None:
    decision = decide(
        [
            KronosSignal("base", 1, 84.0),
            KronosSignal("small", 1, 82.0),
            KronosSignal("mini", 1, 80.0),
        ]
    )
    assert decision.weights["base"] == pytest.approx(0.50)
    assert decision.weights["small"] == pytest.approx(0.30)
    assert decision.weights["mini"] == pytest.approx(0.20)


def test_signal_validation_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        KronosSignal("base", 2, 80.0)
    with pytest.raises(ValueError):
        KronosSignal("base", 1, 140.0)


def test_prediction_adapter() -> None:
    decision = from_predictions(
        {
            "base": {"direction": 1, "confidence": 86.0},
            "small": {"direction": 1, "confidence": 85.0},
            "mini": {"direction": 1, "confidence": 84.0},
        }
    )
    assert decision.actionable is True
    assert decision.confidence >= MIN_ENTRY_CONFIDENCE


# --------------------------------------------------------------------------- #
# quantile + path-vote maths
# --------------------------------------------------------------------------- #


def test_quantiles_interpolate() -> None:
    p10, p50, p90 = quantiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
    assert p10 == pytest.approx(2.0)
    assert p50 == pytest.approx(6.0)
    assert p90 == pytest.approx(10.0)


def test_quantiles_single_and_empty() -> None:
    assert quantiles([7.5]) == (7.5, 7.5, 7.5)
    with pytest.raises(ValueError):
        quantiles([])


# A 1.1% cone: comfortably inside CONE_TIGHT_RATIO (1.2%) unless a test says
# otherwise, so "aligned" fixtures are approved and only explicit wide-cone
# fixtures are rejected.
def _forecast(role: str, paths_up: int, samples: int = 100,
              last: float = 100.0, p10: float = 99.8,
              p50: float = 100.6, p90: float = 100.9) -> ModelForecast:
    return ModelForecast(
        model=role, last_close=last, p10=p10, p50=p50, p90=p90,
        paths_up=paths_up, samples=samples,
    )


def test_up_pct_and_direction() -> None:
    assert _forecast("base", 84, 100).up_pct == pytest.approx(84.0)
    assert _forecast("base", 84, 100).direction == 1
    assert _forecast("base", 16, 100).direction == -1
    assert _forecast("base", 50, 100).direction == 0
    assert _forecast("mini", 6, 8).up_pct == pytest.approx(75.0)


def test_cone_tightness() -> None:
    # 1.1%, deliberately inside the gate: an exact 1.2% boundary lands at
    # 0.012000000000000028 in binary floating point and would trip the >.
    tight = _forecast("base", 84, p10=99.8, p90=100.9)
    assert tight.cone_tight is True
    assert tight.dispersion < CONE_TIGHT_RATIO
    wide = _forecast("base", 84, p10=96.0, p90=104.0)
    assert wide.cone_tight is False
    assert wide.dispersion > CONE_TIGHT_RATIO


# --------------------------------------------------------------------------- #
# trio confluence gates
# --------------------------------------------------------------------------- #


def test_trio_approves_buy_ce_when_all_gates_pass() -> None:
    decision = decide_trio(
        base=_forecast("base", 84),
        small=_forecast("small", 19, 24),
        mini=_forecast("mini", 21, 24),
    )
    assert decision.approved is True
    assert decision.action == "BUY_CE"
    assert decision.confidence >= BASE_BULL_THRESHOLD
    assert set(decision.forecasts) == {"base", "small", "mini"}


def test_trio_approves_buy_pe_on_unanimous_downside() -> None:
    decision = decide_trio(
        base=_forecast("base", 15),
        small=_forecast("small", 4, 24),
        mini=_forecast("mini", 3, 24),
    )
    assert decision.approved is True
    assert decision.action == "BUY_PE"


def test_trio_suppresses_when_base_is_in_the_dead_zone() -> None:
    decision = decide_trio(
        base=_forecast("base", 62),
        small=_forecast("small", 20, 24),
        mini=_forecast("mini", 21, 24),
    )
    assert decision.approved is False
    assert decision.action == "NO_TRADE"
    assert any("dead zone" in reason for reason in decision.reasons)


def test_trio_suppresses_when_small_disagrees() -> None:
    decision = decide_trio(
        base=_forecast("base", 84),
        small=_forecast("small", 3, 24),
        mini=_forecast("mini", 21, 24),
    )
    assert decision.approved is False
    assert any("small disagrees" in reason for reason in decision.reasons)


def test_trio_suppresses_when_mini_does_not_confirm() -> None:
    decision = decide_trio(
        base=_forecast("base", 84),
        small=_forecast("small", 19, 24),
        mini=_forecast("mini", 2, 24),
    )
    assert decision.approved is False
    assert any("mini does not confirm" in reason for reason in decision.reasons)


def test_trio_suppresses_wide_cone_chop() -> None:
    decision = decide_trio(
        base=_forecast("base", 84, p10=94.0, p90=108.0),
        small=_forecast("small", 19, 24),
        mini=_forecast("mini", 21, 24),
    )
    assert decision.approved is False
    assert any("cone too wide" in reason for reason in decision.reasons)


def test_threshold_constants_are_the_documented_ones() -> None:
    assert BASE_BULL_THRESHOLD == 78.0
    assert BASE_BEAR_THRESHOLD == 22.0


def test_decision_serialises_for_the_dashboard() -> None:
    decision = decide_trio(
        base=_forecast("base", 84),
        small=_forecast("small", 19, 24),
        mini=_forecast("mini", 21, 24),
    )
    payload = decision.as_dict()
    assert set(payload) == {"action", "approved", "confidence", "reasons", "forecasts"}
    assert set(payload["forecasts"]["base"]) == {
        "model", "direction", "up_pct", "paths_up", "samples",
        "p10", "p50", "p90", "dispersion",
    }


# --------------------------------------------------------------------------- #
# CHECK 1 - artefacts exist, load, and are the three distinct checkpoints
# --------------------------------------------------------------------------- #


def test_weight_directories_and_tokenizers_exist() -> None:
    if not WEIGHTS_PRESENT:
        pytest.skip("weights are downloaded on the cloud runner only")
    for role in ROLES:
        weight = WEIGHTS_DIR / role / "model.safetensors"
        assert weight.is_file(), f"missing {weight}"
        assert weight.stat().st_size > 1_000_000, f"{role} weights suspiciously small"
        assert (WEIGHTS_DIR / role / "config.json").is_file()
    for tokenizer in TOKENIZERS:
        assert (WEIGHTS_DIR / "tokenizers" / tokenizer / "model.safetensors").is_file()


@requires_models
def test_all_three_models_load_without_corruption() -> None:
    import torch

    from agent._vendor.kronos_upstream import Kronos

    counts: dict[str, int] = {}
    for role in ROLES:
        model = Kronos.from_pretrained(str(WEIGHTS_DIR / role))
        assert isinstance(model, torch.nn.Module)
        counts[role] = sum(parameter.numel() for parameter in model.parameters())
        model.eval()
    assert counts["mini"] < counts["small"] < counts["base"]
    assert counts["base"] > 50_000_000
    print(f"\nCHECK 1 parameter counts: {counts}")


@requires_models
def test_ensemble_loads_all_three_predictors_with_four_threads() -> None:
    trio = KronosTrioEnsemble(samples=2).load()
    assert trio.loaded is True
    assert sorted(trio.predictors) == ["base", "mini", "small"]
    assert trio.torch.get_num_threads() == 4


# --------------------------------------------------------------------------- #
# CHECK 2 - 512-bar dummy Nifty OHLCV through all three, latency measured
# --------------------------------------------------------------------------- #


def _dummy_nifty_frame(rows: int = 512):
    import pandas as pd

    index = pd.date_range("2026-11-05 09:15", periods=rows, freq="5min")
    closes = [24_800 + ((step * 7) % 90) - 45 + step * 0.05 for step in range(rows)]
    opens = [value - 4 for value in closes]
    highs = [max(o, c) + 6 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 6 for o, c in zip(opens, closes)]
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [12_000 + i * 3 for i in range(rows)],
            "amount": [c * 12_000 for c in closes],
        },
        index=index,
    )


@requires_models
def test_dummy_ohlcv_through_all_three_models_is_timed() -> None:
    frame = _dummy_nifty_frame(512)
    trio = KronosTrioEnsemble(samples=2).load()

    started = time.perf_counter()
    decision = trio.evaluate({"base": frame, "small": frame, "mini": frame})
    elapsed = time.perf_counter() - started

    assert decision.action in {"BUY_CE", "BUY_PE", "NO_TRADE"}
    assert set(decision.forecasts) == {"base", "small", "mini"}
    for role in ROLES:
        payload = decision.forecasts[role]
        assert payload["p10"] <= payload["p50"] <= payload["p90"]
        # The neural checkpoint may run two draws for speed, but the
        # production forecast contract always publishes the vectorized
        # 100-way conditional projection.
        assert payload["samples"] == 100
        assert 0 <= payload["paths_up"] <= 100
    assert elapsed < 900, f"inference too slow: {elapsed:.1f}s"
    print(f"\nCHECK 2 inference latency (512 bars, 100-way projection x 3 models): {elapsed:.2f}s")
    print(f"CHECK 2 decision: {decision.action} ({decision.confidence})")


# --------------------------------------------------------------------------- #
# CHECK 3 - aligned triggers, conflicting suppresses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base_up", "small_up", "mini_up", "expected"),
    [
        (88, 20, 22, "BUY_CE"),
        (80, 19, 18, "BUY_CE"),
        (12, 4, 5, "BUY_PE"),
        (88, 4, 22, "NO_TRADE"),
        (88, 20, 2, "NO_TRADE"),
        (55, 20, 22, "NO_TRADE"),
        (12, 19, 5, "NO_TRADE"),
    ],
)
def test_false_signal_suppression_matrix(base_up, small_up, mini_up, expected) -> None:
    decision = decide_trio(
        base=_forecast("base", base_up),
        small=_forecast("small", small_up, 24),
        mini=_forecast("mini", mini_up, 24),
    )
    assert decision.action == expected
    assert decision.approved is (expected != "NO_TRADE")
