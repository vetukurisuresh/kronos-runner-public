"""Price-action / Smart-Money-Concepts analyzer.

Detects market structure (swings, BOS, CHoCH), order blocks and VWAP bias from a
list of candles. Pure Python so it runs on every tick and is unit-testable
without pandas or numpy.

Candles may be dicts (``open/high/low/close/volume``) or objects exposing the
same attributes, which lets the aggregator's :class:`market.upstox_stream.Candle`
flow straight in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

SWING_LOOKBACK = 2
IMPULSE_RANGE_FACTOR = 1.5


@dataclass(frozen=True)
class OrderBlock:
    """Last opposing candle before an impulsive move — the reaction zone."""

    direction: int  # +1 demand (bullish OB), -1 supply (bearish OB)
    low: float
    high: float
    index: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "index": self.index,
        }


@dataclass
class SmcAnalysis:
    bias: int
    bos: bool
    choch: bool
    vwap: float
    vwap_side: str
    last_swing_high: float
    last_swing_low: float
    order_blocks: list[OrderBlock] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def near_order_block(self) -> OrderBlock | None:
        return self.order_blocks[0] if self.order_blocks else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias,
            "bos": self.bos,
            "choch": self.choch,
            "vwap": round(self.vwap, 2),
            "vwap_side": self.vwap_side,
            "swing_high": round(self.last_swing_high, 2),
            "swing_low": round(self.last_swing_low, 2),
        }


def _get(candle: Any, key: str, default: float = 0.0) -> float:
    if isinstance(candle, dict):
        return float(candle.get(key, default) or default)
    return float(getattr(candle, key, default) or default)


def find_swings(
    highs: Sequence[float], lows: Sequence[float], lookback: int = SWING_LOOKBACK
) -> tuple[list[int], list[int]]:
    """Return indices of swing highs and swing lows."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    swing_highs: list[int] = []
    swing_lows: list[int] = []
    for index in range(lookback, len(highs) - lookback):
        window_high = highs[index - lookback : index + lookback + 1]
        window_low = lows[index - lookback : index + lookback + 1]
        if highs[index] == max(window_high) and window_high.count(highs[index]) == 1:
            swing_highs.append(index)
        if lows[index] == min(window_low) and window_low.count(lows[index]) == 1:
            swing_lows.append(index)
    return swing_highs, swing_lows


def compute_vwap(candles: Sequence[Any]) -> float:
    """Volume-weighted average price over the supplied candles."""
    numerator = 0.0
    denominator = 0.0
    for candle in candles:
        high, low, close = _get(candle, "high"), _get(candle, "low"), _get(candle, "close")
        volume = _get(candle, "volume", 1.0) or 1.0
        typical = (high + low + close) / 3.0
        numerator += typical * volume
        denominator += volume
    return numerator / denominator if denominator else 0.0


def detect_order_blocks(candles: Sequence[Any], max_blocks: int = 3) -> list[OrderBlock]:
    """Find the last opposing candle before each impulsive displacement."""
    if len(candles) < 3:
        return []
    ranges = [_get(c, "high") - _get(c, "low") for c in candles]
    average_range = sum(ranges) / len(ranges)
    if average_range <= 0:
        return []

    blocks: list[OrderBlock] = []
    for index in range(1, len(candles)):
        impulse = ranges[index]
        if impulse < average_range * IMPULSE_RANGE_FACTOR:
            continue
        previous = candles[index - 1]
        prev_open, prev_close = _get(previous, "open"), _get(previous, "close")
        impulse_direction = 1 if _get(candles[index], "close") >= _get(candles[index], "open") else -1
        # Bullish displacement follows a bearish candle (and vice versa).
        if impulse_direction > 0 and prev_close < prev_open:
            blocks.append(
                OrderBlock(1, _get(previous, "low"), _get(previous, "high"), index - 1)
            )
        elif impulse_direction < 0 and prev_close > prev_open:
            blocks.append(
                OrderBlock(-1, _get(previous, "low"), _get(previous, "high"), index - 1)
            )
    return blocks[-max_blocks:][::-1]


def analyze(candles: Sequence[Any], lookback: int = SWING_LOOKBACK) -> SmcAnalysis:
    """Full structure read on the supplied candle series."""
    if len(candles) < (2 * lookback + 2):
        raise ValueError(f"need at least {2 * lookback + 2} candles")

    highs = [_get(c, "high") for c in candles]
    lows = [_get(c, "low") for c in candles]
    closes = [_get(c, "close") for c in candles]

    swing_highs, swing_lows = find_swings(highs, lows, lookback)
    # A smoothly trending series has no pivot. Fall back to the series extremes
    # rather than raising: this function runs on the hot path, and an exception
    # there would silently stop the engine mid-session.
    if not swing_highs:
        swing_highs = [max(range(len(highs)), key=highs.__getitem__)]
    if not swing_lows:
        swing_lows = [min(range(len(lows)), key=lows.__getitem__)]

    last_high = highs[swing_highs[-1]]
    last_low = lows[swing_lows[-1]]
    reasons: list[str] = []

    bias = 0
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        higher_high = highs[swing_highs[-1]] > highs[swing_highs[-2]]
        higher_low = lows[swing_lows[-1]] > lows[swing_lows[-2]]
        lower_high = highs[swing_highs[-1]] < highs[swing_highs[-2]]
        lower_low = lows[swing_lows[-1]] < lows[swing_lows[-2]]
        if higher_high and higher_low:
            bias = 1
            reasons.append("higher highs + higher lows")
        elif lower_high and lower_low:
            bias = -1
            reasons.append("lower highs + lower lows")
        else:
            reasons.append("range / mixed structure")
    else:
        # Only one pivot of each kind: use the overall drift as the bias.
        if closes[-1] > closes[0]:
            bias = 1
        elif closes[-1] < closes[0]:
            bias = -1
        reasons.append("no pivot pair - series drift fallback")

    last_close = closes[-1]
    bos = last_close > last_high or last_close < last_low
    if bos:
        reasons.append("break of structure")

    # CHoCH = the newest break opposes the prior leg bias.
    choch = False
    if len(closes) >= 3:
        prior = closes[-3] - closes[-2]
        latest = closes[-2] - closes[-1]
        choch = prior * latest < 0 and bos
        if choch:
            reasons.append("change of character")

    vwap = compute_vwap(candles)
    return SmcAnalysis(
        bias=bias,
        bos=bos,
        choch=choch,
        vwap=vwap,
        vwap_side="above" if last_close >= vwap else "below",
        last_swing_high=last_high,
        last_swing_low=last_low,
        order_blocks=detect_order_blocks(candles),
        reasons=reasons,
    )
