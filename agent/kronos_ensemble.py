"""Three-model KRONOS trio ensemble (macro judge / trend validator / micro trigger).

Voting rule
-----------
Each model draws ``N`` Monte Carlo futures and reports how many close above the
last price (``paths_up``). The trio is approved only when **all** of the
following hold:

1. ``Kronos-base`` is decisive: ``paths_up >= 78%`` (BUY_CE) or ``<= 22%`` (BUY_PE)
2. ``Kronos-small`` leans the same way
3. ``Kronos-mini`` confirms the same micro-momentum
4. the cone is tight: ``(p90 - p10) / spot <= CONE_TIGHT_RATIO``

Anything else is ``NO_TRADE`` — the gate is designed to suppress, not to guess.

Torch, pandas and the vendored architecture are imported lazily so that
``import agent.kronos_ensemble`` stays cheap and works with no weights present.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from config.settings import WEIGHTS_DIR

BASE_BULL_THRESHOLD = 78.0
BASE_BEAR_THRESHOLD = 22.0
CONE_TIGHT_RATIO = 0.012  # (p90 - p10) / last_close
DEFAULT_THREADS = 4

#: Production Monte Carlo budget. ``KRONOS_MC_SAMPLES`` overrides every role at
#: once, which is how CI keeps a single verification run inside a few minutes.
DEFAULT_SAMPLES: dict[str, int] = {"base": 100, "small": 24, "mini": 24}

ROLE_CONFIG: dict[str, dict[str, Any]] = {
    "base": {"interval": "5m", "lookback": 480, "pred_len": 24, "max_context": 512},
    "small": {"interval": "5m", "lookback": 256, "pred_len": 12, "max_context": 512},
    "mini": {"interval": "1m", "lookback": 120, "pred_len": 6, "max_context": 2048},
}

TOKENIZER_FOR_ROLE = {
    "base": "Kronos-Tokenizer-base",
    "small": "Kronos-Tokenizer-base",
    "mini": "Kronos-Tokenizer-2k",
}


class EnsembleUnavailable(RuntimeError):
    """Raised when torch, pandas or the weights are missing."""


# --------------------------------------------------------------------------- #
# value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelForecast:
    """One model's Monte Carlo distribution over the next ``pred_len`` bars."""

    model: str
    last_close: float
    p10: float
    p50: float
    p90: float
    paths_up: int
    samples: int
    #: Final close of every Monte Carlo draw, in draw order (the UI fan chart).
    paths: tuple[float, ...] = ()

    @property
    def up_pct(self) -> float:
        return round(100.0 * self.paths_up / max(self.samples, 1), 2)

    @property
    def direction(self) -> int:
        if self.up_pct > 50.0:
            return 1
        if self.up_pct < 50.0:
            return -1
        return 0

    @property
    def dispersion(self) -> float:
        if self.last_close <= 0:
            return 1.0
        return max(self.p90 - self.p10, 0.0) / self.last_close

    @property
    def cone_tight(self) -> bool:
        return self.dispersion <= CONE_TIGHT_RATIO

    @property
    def conviction(self) -> float:
        return round(max(self.up_pct, 100.0 - self.up_pct), 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "direction": self.direction,
            "up_pct": self.up_pct,
            "paths_up": self.paths_up,
            "samples": self.samples,
            "p10": round(self.p10, 2),
            "p50": round(self.p50, 2),
            "p90": round(self.p90, 2),
            "dispersion": round(self.dispersion, 5),
        }


@dataclass
class TrioDecision:
    action: str  # BUY_CE | BUY_PE | NO_TRADE
    approved: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    forecasts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "approved": self.approved,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "forecasts": self.forecasts,
        }


# --------------------------------------------------------------------------- #
# pure decision core
# --------------------------------------------------------------------------- #


def base_direction(forecast: ModelForecast) -> int:
    """The 78/22 rule — the macro judge must be decisive, not merely leaning."""
    if forecast.up_pct >= BASE_BULL_THRESHOLD:
        return 1
    if forecast.up_pct <= BASE_BEAR_THRESHOLD:
        return -1
    return 0


def decide_trio(
    base: ModelForecast,
    small: ModelForecast,
    mini: ModelForecast,
    *,
    cone_limit: float = CONE_TIGHT_RATIO,
) -> TrioDecision:
    """Apply the four-gate confluence rule; any failed gate suppresses the trade."""
    reasons: list[str] = []
    forecasts = {role: f.as_dict() for role, f in (("base", base), ("small", small), ("mini", mini))}

    direction = base_direction(base)
    if direction == 0:
        reasons.append(
            f"base inside the dead zone (paths_up {base.up_pct:.1f}% not >=78% or <=22%)"
        )
    else:
        reasons.append(f"base decisive {base.up_pct:.1f}% paths_up")

    small_ok = small.direction == direction and direction != 0
    if not small_ok:
        reasons.append(f"small disagrees (small={small.up_pct:.1f}%, base_dir={direction})")

    mini_ok = mini.direction == direction and direction != 0
    if not mini_ok:
        reasons.append(f"mini does not confirm (mini={mini.up_pct:.1f}%, base_dir={direction})")

    widest = max(base.dispersion, small.dispersion, mini.dispersion)
    cone_ok = widest <= cone_limit
    if not cone_ok:
        reasons.append(f"cone too wide ({widest:.4f} > {cone_limit:.4f}) - chopping risk")

    approved = direction != 0 and small_ok and mini_ok and cone_ok
    action = "NO_TRADE"
    confidence = 0.0
    if approved:
        action = "BUY_CE" if direction > 0 else "BUY_PE"
        confidence = round(min(base.conviction, small.conviction, mini.conviction), 2)
        reasons.append(f"trio aligned on {'upside' if direction > 0 else 'downside'} edge")

    return TrioDecision(
        action=action,
        approved=approved,
        confidence=confidence,
        reasons=reasons,
        forecasts=forecasts,
    )


def quantiles(samples: Iterable[float]) -> tuple[float, float, float]:
    """p10 / p50 / p90 with linear interpolation (numpy-free)."""
    ordered = sorted(float(value) for value in samples)
    if not ordered:
        raise ValueError("no samples")
    if len(ordered) == 1:
        return ordered[0], ordered[0], ordered[0]

    def at(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return at(0.10), at(0.50), at(0.90)


# --------------------------------------------------------------------------- #
# torch-backed ensemble
# --------------------------------------------------------------------------- #


class KronosTrioEnsemble:
    """Loads the trio once and produces confluence-gated decisions."""

    def __init__(
        self,
        weights_root: Path | str = WEIGHTS_DIR,
        threads: int = DEFAULT_THREADS,
        device: str = "cpu",
        samples: int | None = None,
        verbose: bool = False,
        collect_frames: bool = False,
    ) -> None:
        self.collect_frames = collect_frames
        self.frames: dict[str, list] = {}
        self.weights_root = Path(weights_root)
        self.threads = max(int(threads), 1)
        self.device = device
        self.verbose = verbose
        override = samples if samples is not None else os.environ.get("KRONOS_MC_SAMPLES", "")
        try:
            self.samples = {
                role: max(int(override), 1) if str(override).strip() else DEFAULT_SAMPLES[role]
                for role in ROLE_CONFIG
            }
        except (TypeError, ValueError):
            self.samples = dict(DEFAULT_SAMPLES)
        self.torch = None
        self.predictors: dict[str, Any] = {}
        self.loaded = False

    # -- loading -----------------------------------------------------------
    def _import_torch(self):
        try:
            import torch
        except ImportError as exc:  # pragma: no cover
            raise EnsembleUnavailable("torch is not installed on this runner") from exc
        return torch

    def load(self) -> "KronosTrioEnsemble":
        torch = self._import_torch()
        self.torch = torch
        torch.set_num_threads(self.threads)
        torch.set_grad_enabled(False)

        try:
            from agent._vendor.kronos_upstream import Kronos, KronosPredictor, KronosTokenizer
        except ImportError as exc:  # pragma: no cover
            raise EnsembleUnavailable(f"vendored Kronos source unavailable: {exc}") from exc

        missing = [
            role for role in ROLE_CONFIG
            if not (self.weights_root / role / "model.safetensors").is_file()
        ]
        if missing:
            raise EnsembleUnavailable(
                f"weights missing for {missing}; run python model/download_all_kronos.py"
            )

        for role, config in ROLE_CONFIG.items():
            model_dir = self.weights_root / role
            tokenizer_dir = self.weights_root / "tokenizers" / TOKENIZER_FOR_ROLE[role]
            if not (tokenizer_dir / "model.safetensors").is_file():
                raise EnsembleUnavailable(f"tokenizer missing for {role}: {tokenizer_dir}")
            model = Kronos.from_pretrained(str(model_dir))
            tokenizer = KronosTokenizer.from_pretrained(str(tokenizer_dir))
            model.eval()
            tokenizer.eval()
            self.predictors[role] = KronosPredictor(
                model, tokenizer, device=self.device, max_context=int(config["max_context"])
            )
            if self.verbose:
                print(
                    f"  loaded {role:<6} interval={config['interval']:<3} "
                    f"max_context={config['max_context']} samples={self.samples[role]}"
                )
        self.loaded = True
        return self

    # -- inference ---------------------------------------------------------
    def _frame(self, candles: Any, lookback: int):
        import pandas as pd

        if isinstance(candles, pd.DataFrame):
            tail = candles.tail(lookback)
            frame = tail.reset_index(drop=True)
            if isinstance(tail.index, pd.DatetimeIndex):
                stamps = pd.Series(pd.to_datetime(tail.index))
            else:
                stamps = pd.Series(
                    pd.date_range("2026-11-05 09:15", periods=len(frame), freq="5min")
                )
        else:
            rows = list(candles)[-lookback:]
            if not rows:
                raise ValueError("no candles supplied")
            frame = pd.DataFrame(rows)
            if "timestamp" in frame.columns:
                stamps = pd.Series(pd.to_datetime(frame["timestamp"]))
            else:
                stamps = pd.Series(
                    pd.date_range("2026-11-05 09:15", periods=len(frame), freq="5min")
                )

        for column in ("open", "high", "low", "close"):
            if column not in frame.columns:
                raise ValueError(f"candle frame missing column: {column}")
        if "volume" not in frame.columns:
            frame["volume"] = 0.0
        return frame, stamps

    def forecast(self, role: str, candles: Any) -> ModelForecast:
        """Draw the Monte Carlo cone for one role and count upward paths."""
        if not self.loaded:
            raise EnsembleUnavailable("call load() before forecast()")
        import pandas as pd

        config = ROLE_CONFIG[role]
        frame, timestamps = self._frame(candles, int(config["lookback"]))
        last_close = float(frame["close"].iloc[-1])
        pred_len = int(config["pred_len"])
        draws = self.samples[role]

        future = pd.date_range(
            start=pd.Timestamp(timestamps.iloc[-1]) + pd.Timedelta(minutes=5),
            periods=pred_len,
            freq="5min",
        )

        finals: list[float] = []
        draws_frames: list = []
        with self.torch.inference_mode():
            for seed in range(draws):
                self.torch.manual_seed(seed)
                predicted = self.predictors[role].predict(
                    df=frame,
                    x_timestamp=timestamps,
                    y_timestamp=pd.Series(future),
                    pred_len=pred_len,
                    T=1.0,
                    top_p=0.9,
                    sample_count=1,
                    verbose=False,
                )
                finals.append(float(predicted["close"].iloc[-1]))
                if self.collect_frames:
                    draws_frames.append(predicted)

        if self.collect_frames:
            self.frames[role] = draws_frames

        p10, p50, p90 = quantiles(finals)
        return ModelForecast(
            model=role,
            last_close=last_close,
            p10=p10,
            p50=p50,
            p90=p90,
            paths_up=sum(1 for value in finals if value > last_close),
            samples=len(finals),
            paths=tuple(finals),
        )

    def evaluate(self, frames: dict[str, Any], **gates: Any) -> TrioDecision:
        """Run all three models concurrently, then apply the confluence rule."""
        if not self.loaded:
            raise EnsembleUnavailable("call load() before evaluate()")
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="kronos") as pool:
            futures = {role: pool.submit(self.forecast, role, frames[role]) for role in ROLE_CONFIG}
            results = {role: future.result() for role, future in futures.items()}
        return decide_trio(results["base"], results["small"], results["mini"], **gates)


def ensemble_available(weights_root: Path | str = WEIGHTS_DIR) -> tuple[bool, str]:
    """Cheap readiness probe for the runner preflight and the tests."""
    root = Path(weights_root)
    missing = [role for role in ROLE_CONFIG if not (root / role / "model.safetensors").is_file()]
    if missing:
        return False, f"weights missing: {missing}"
    missing_tok = [
        name for name in set(TOKENIZER_FOR_ROLE.values())
        if not (root / "tokenizers" / name / "model.safetensors").is_file()
    ]
    if missing_tok:
        return False, f"tokenizers missing: {missing_tok}"
    try:
        import torch  # noqa: F401
    except ImportError:
        return False, "torch not installed"
    return True, "weights, tokenizers and torch present"
