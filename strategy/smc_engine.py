"""Pure Smart Money Concepts engine fused with KRONOS price levels.

The engine is deliberately deterministic and broker-agnostic.  It consumes
already-sanitized Upstox candles and answers one question: *where is the exact
price at which the odds become good enough to fire?*

Definitions
-----------
* swing: a fractal high/low with ``lookback`` bars on both sides
* BOS: close beyond the most recent swing in the direction of the prior leg
* CHoCH: close beyond the most recent swing against the prior leg
* order block: the last opposing candle before a high-volume impulsive leg
* FVG: a three-candle imbalance that has not been traded back through
* liquidity sweep: a wick through a prior swing that closes back inside with
  order-book absorption

No random values, no "best effort" price, no trade while the feed is stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

IMPULSE_RANGE_FACTOR = 1.5
FLOW_MULTIPLE = 2.0
OBI_IGNITION = 1.35
SWEEP_ABSORPTION = 1.10


def _value(row: Any, name: str, default: float = 0.0) -> float:
    if isinstance(row, dict):
        raw = row.get(name, default)
    else:
        raw = getattr(row, name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _time(row: Any) -> float:
    for name in ("time", "t", "timestamp", "ts", "start"):
        raw = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _rows(candles: Iterable[Any] | None) -> list[Any]:
    return [row for row in (candles or []) if row is not None]


@dataclass(frozen=True)
class Swing:
    kind: str
    index: int
    price: float
    time: float = 0.0


@dataclass(frozen=True)
class Structure:
    bias: int
    bos: bool
    choch: bool
    active_bos: str
    active_choch: str
    last_swing_high: float
    last_swing_low: float
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "bias": self.bias,
            "bos": self.bos,
            "choch": self.choch,
            "active_bos": self.active_bos,
            "active_choch": self.active_choch,
            "swing_high": round(self.last_swing_high, 2),
            "swing_low": round(self.last_swing_low, 2),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class OrderBlock:
    direction: int
    low: float
    high: float
    index: int
    volume_multiple: float
    mitigated: bool = False

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": "BULLISH_OB" if self.direction > 0 else "BEARISH_OB",
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "index": self.index,
            "volume_multiple": round(self.volume_multiple, 2),
            "mitigated": self.mitigated,
        }


@dataclass(frozen=True)
class FairValueGap:
    direction: int
    low: float
    high: float
    index: int
    mitigated: bool = False

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": "BULLISH_FVG" if self.direction > 0 else "BEARISH_FVG",
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "index": self.index,
            "mitigated": self.mitigated,
        }


@dataclass(frozen=True)
class LiquiditySweep:
    direction: int
    swept_level: float
    close: float
    index: int
    absorbed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": "SELL_SIDE_SWEEP" if self.direction > 0 else "BUY_SIDE_SWEEP",
            "swept_level": round(self.swept_level, 2),
            "close": round(self.close, 2),
            "index": self.index,
            "absorbed": self.absorbed,
        }


@dataclass(frozen=True)
class WaitZone:
    spot_low: float
    spot_high: float
    option_target: float | None
    confluence: tuple[str, ...] = ()

    @property
    def mid(self) -> float:
        return (self.spot_low + self.spot_high) / 2.0

    def contains(self, price: float) -> bool:
        return self.spot_low <= float(price) <= self.spot_high

    def as_dict(self) -> dict[str, Any]:
        return {
            "spot_low": round(self.spot_low, 2),
            "spot_high": round(self.spot_high, 2),
            "spot_mid": round(self.mid, 2),
            "option_target": round(self.option_target, 2) if self.option_target is not None else None,
            "confluence": list(self.confluence),
            "label": (f"Target Spot: ₹{self.mid:,.2f} / Option: "
                      f"₹{self.option_target:,.2f}" if self.option_target is not None
                      else f"Target Spot: ₹{self.mid:,.2f}"),
        }


@dataclass
class SmcSnapshot:
    structure: Structure
    structures: dict[str, Structure] = field(default_factory=dict)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    fvgs: list[FairValueGap] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)
    wait_zone: WaitZone | None = None
    state: str = "WAITING_FOR_STRUCTURE"
    reason: str = ""
    triggered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "structure": self.structure.as_dict(),
            "structures": {name: item.as_dict() for name, item in self.structures.items()},
            "order_blocks": [block.as_dict() for block in self.order_blocks],
            "fvgs": [gap.as_dict() for gap in self.fvgs],
            "sweeps": [sweep.as_dict() for sweep in self.sweeps],
            "wait_zone": self.wait_zone.as_dict() if self.wait_zone else None,
            "state": self.state,
            "reason": self.reason,
            "triggered": self.triggered,
        }


def find_swings(candles: Iterable[Any] | None, lookback: int = 2) -> list[Swing]:
    rows = _rows(candles)
    if lookback < 1 or len(rows) < 2 * lookback + 1:
        return []
    highs = [_value(row, "high") for row in rows]
    lows = [_value(row, "low") for row in rows]
    swings: list[Swing] = []
    for index in range(lookback, len(rows) - lookback):
        window_high = highs[index - lookback:index + lookback + 1]
        window_low = lows[index - lookback:index + lookback + 1]
        if highs[index] == max(window_high) and window_high.count(highs[index]) == 1:
            swings.append(Swing("high", index, highs[index], _time(rows[index])))
        if lows[index] == min(window_low) and window_low.count(lows[index]) == 1:
            swings.append(Swing("low", index, lows[index], _time(rows[index])))
    return sorted(swings, key=lambda swing: swing.index)


def analyze_structure(candles: Iterable[Any] | None, *, lookback: int = 2) -> Structure:
    rows = _rows(candles)
    if len(rows) < 4:
        return Structure(0, False, False, "", "", 0.0, 0.0, ("insufficient candles",))
    swings = find_swings(rows, lookback)
    highs = [swing for swing in swings if swing.kind == "high"]
    lows = [swing for swing in swings if swing.kind == "low"]
    closes = [_value(row, "close") for row in rows]
    if not highs or not lows:
        return Structure(0, False, False, "", "", 0.0, 0.0, ("no confirmed swings",))
    last_high = highs[-1].price
    last_low = lows[-1].price
    bias = 0
    reasons: list[str] = []
    if len(highs) >= 2 and len(lows) >= 2:
        higher = highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price
        lower = highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price
        if higher:
            bias = 1
            reasons.append("higher highs + higher lows")
        elif lower:
            bias = -1
            reasons.append("lower highs + lower lows")
        else:
            reasons.append("mixed swing sequence")
    else:
        bias = 1 if closes[-1] > closes[0] else -1 if closes[-1] < closes[0] else 0
        reasons.append("single swing pair - drift used")
    close = closes[-1]
    break_up = close > last_high
    break_down = close < last_low
    if break_up:
        active = "BULLISH_BOS"
        reasons.append("close above last swing high")
    elif break_down:
        active = "BEARISH_BOS"
        reasons.append("close below last swing low")
    else:
        active = ""
    choch = bool((break_up and bias < 0) or (break_down and bias > 0))
    if choch:
        reasons.append("break against prior leg - CHoCH")
    return Structure(
        bias=bias,
        bos=bool(active),
        choch=choch,
        active_bos=active,
        active_choch=("BULLISH" if break_up else "BEARISH") if choch else "",
        last_swing_high=last_high,
        last_swing_low=last_low,
        reasons=tuple(reasons),
    )


def _flow_map(futures: Iterable[Any] | None) -> tuple[dict[float, float], dict[int, float]]:
    rows = _rows(futures)
    by_time: dict[float, float] = {}
    by_index: dict[int, float] = {}
    previous: float | None = None
    for index, row in enumerate(rows):
        vtt = max(_value(row, "vtt", _value(row, "volume")), 0.0)
        delta = max(vtt - previous, 0.0) if previous is not None else 0.0
        previous = vtt if vtt else previous
        by_index[index] = delta
        stamp = _time(row)
        if stamp > 0:
            by_time[stamp] = delta
    return by_time, by_index


def detect_order_blocks(
    candles: Iterable[Any] | None,
    futures: Iterable[Any] | None = None,
    *,
    max_blocks: int = 5,
) -> list[OrderBlock]:
    rows = _rows(candles)
    flow_rows = _rows(futures)
    if len(rows) < 3 or len(flow_rows) < 2:
        return []
    by_time, by_index = _flow_map(flow_rows)
    deltas = [value for value in by_time.values() if value > 0]
    if not deltas:
        deltas = [value for value in by_index.values() if value > 0]
    if not deltas:
        return []
    mean_flow = sum(deltas) / len(deltas)
    if mean_flow <= 0:
        return []
    ranges = [max(_value(row, "high") - _value(row, "low"), 0.0) for row in rows]
    average_range = sum(ranges) / len(ranges)
    if average_range <= 0:
        return []
    blocks: list[OrderBlock] = []
    for index in range(1, len(rows)):
        impulse = ranges[index]
        if impulse < average_range * IMPULSE_RANGE_FACTOR:
            continue
        flow = by_time.get(_time(rows[index]), by_index.get(index, 0.0))
        if flow < mean_flow * FLOW_MULTIPLE:
            continue
        previous = rows[index - 1]
        prev_open, prev_close = _value(previous, "open"), _value(previous, "close")
        bullish = _value(rows[index], "close") >= _value(rows[index], "open")
        if bullish and prev_close < prev_open:
            block = OrderBlock(1, _value(previous, "low"), _value(previous, "high"),
                               index - 1, flow / mean_flow)
        elif not bullish and prev_close > prev_open:
            block = OrderBlock(-1, _value(previous, "low"), _value(previous, "high"),
                               index - 1, flow / mean_flow)
        else:
            continue
        later = rows[index + 1:]
        mitigated = (
            any(_value(row, "low") < block.low for row in later)
            if block.direction > 0
            else any(_value(row, "high") > block.high for row in later)
        )
        if not mitigated:
            blocks.append(block)
    return blocks[-max_blocks:][::-1]


def detect_fvgs(candles: Iterable[Any] | None, *, max_gaps: int = 5) -> list[FairValueGap]:
    rows = _rows(candles)
    gaps: list[FairValueGap] = []
    for index in range(2, len(rows)):
        first, third = rows[index - 2], rows[index]
        if _value(first, "high") < _value(third, "low"):
            gap = FairValueGap(1, _value(first, "high"), _value(third, "low"), index - 1)
        elif _value(first, "low") > _value(third, "high"):
            gap = FairValueGap(-1, _value(third, "high"), _value(first, "low"), index - 1)
        else:
            continue
        later = rows[index + 1:]
        mitigated = (
            any(_value(row, "low") <= gap.low for row in later)
            if gap.direction > 0
            else any(_value(row, "high") >= gap.high for row in later)
        )
        if not mitigated:
            gaps.append(gap)
    return gaps[-max_gaps:][::-1]


def detect_liquidity_sweeps(
    candles: Iterable[Any] | None,
    *,
    obi_5: float = 0.0,
    lookback: int = 2,
) -> list[LiquiditySweep]:
    rows = _rows(candles)
    if len(rows) < 2 * lookback + 3:
        return []
    swings = find_swings(rows, lookback)
    last = len(rows) - 1
    lows = [swing for swing in swings if swing.kind == "low" and swing.index < last]
    highs = [swing for swing in swings if swing.kind == "high" and swing.index < last]
    sweeps: list[LiquiditySweep] = []
    candle = rows[last]
    close = _value(candle, "close")
    if lows:
        level = lows[-1].price
        if _value(candle, "low") < level and close > level and obi_5 >= SWEEP_ABSORPTION:
            sweeps.append(LiquiditySweep(1, level, close, last, True))
    if highs:
        level = highs[-1].price
        if _value(candle, "high") > level and close < level and obi_5 <= (1.0 / SWEEP_ABSORPTION):
            sweeps.append(LiquiditySweep(-1, level, close, last, True))
    return sweeps


def build_wait_zone(
    *,
    spot: float,
    p10: float | None,
    p25: float | None,
    order_blocks: Sequence[OrderBlock] | None = None,
    fvgs: Sequence[FairValueGap] | None = None,
    option_atp: float | None = None,
) -> WaitZone | None:
    """Confluence of SMC demand, p10/p25 and option ATP.

    For a long setup the zone is the overlap between the macro dip and the
    nearest unmitigated bullish SMC area.  If no structural area is available,
    the p10/p25 band is still a valid *model* zone; if neither exists, there is
    no zone and the state machine must keep waiting.
    """
    if spot <= 0 or p10 is None or p25 is None or p10 <= 0 or p25 <= 0:
        return None
    lower_candidates = [float(p10)]
    upper_candidates = [float(p25)]
    confluence: list[str] = ["Kronos p10/p25 dip"]
    blocks = [block for block in (order_blocks or []) if block.direction > 0 and not block.mitigated]
    gaps = [gap for gap in (fvgs or []) if gap.direction > 0 and not gap.mitigated]
    if blocks:
        lower_candidates.append(max(block.low for block in blocks))
        upper_candidates.append(min(block.high for block in blocks))
        confluence.append("Bullish Order Block")
    if gaps:
        lower_candidates.append(max(gap.low for gap in gaps))
        upper_candidates.append(min(gap.high for gap in gaps))
        confluence.append("Bullish FVG")
    low = max(lower_candidates)
    high = min(upper_candidates)
    if high <= low:
        width = max(float(spot) * 0.0005, 0.05)
        mid = (low + high) / 2.0 if high > 0 else low
        low, high = mid - width, mid + width
    option_target = float(option_atp) if option_atp is not None and option_atp > 0 else None
    if option_target is not None:
        confluence.append("Option ATP")
    return WaitZone(round(low, 2), round(high, 2), option_target, tuple(confluence))


def wait_state(
    zone: WaitZone | None,
    *,
    spot: float,
    obi_5: float,
    feed_live: bool = True,
    structure: Structure | None = None,
    macro_structure: Structure | None = None,
) -> tuple[str, str]:
    """Patient exact-price state machine used by both live and tests."""
    if not feed_live:
        return "BLOCKED_FEED_OFFLINE_OR_STALE", "Order generation blocked by the feed gate"
    if zone is None:
        return "WAITING_FOR_STRUCTURE", "No valid SMC/model confluence zone"
    if structure is not None and structure.active_choch.startswith("BEARISH"):
        return "WAITING_FOR_STRUCTURE", "Bearish CHoCH invalidated the long zone"
    if macro_structure is not None and (
        macro_structure.active_choch.startswith("BEARISH")
        or macro_structure.active_bos == "BEARISH_BOS"
    ):
        return "WAITING_FOR_STRUCTURE", "15m macro structure is bearish; long zone vetoed"
    if not zone.contains(spot):
        side = "above" if spot > zone.spot_high else "below"
        return "WAITING_FOR_PRICE_ZONE", f"Spot ₹{spot:,.2f} is {side} the exact zone"
    if obi_5 < OBI_IGNITION:
        return "WAITING_FOR_IGNITION", f"Price in zone but OBI_5 {obi_5:.2f} < {OBI_IGNITION:.2f}"
    return "TRIGGER_READY", f"Price in zone and OBI_5 {obi_5:.2f} confirms ignition"


class SmcStateMachine:
    """Live 1m/5m/15m detector with a sticky patient entry state."""

    def __init__(self) -> None:
        self.last: SmcSnapshot | None = None
        self.triggered = False

    def update(
        self,
        *,
        candles_1m: Iterable[Any] | None,
        candles_5m: Iterable[Any] | None,
        candles_15m: Iterable[Any] | None = None,
        futures_1m: Iterable[Any] | None = None,
        p10: float | None = None,
        p25: float | None = None,
        spot: float = 0.0,
        option_atp: float | None = None,
        obi_5: float = 0.0,
        feed_live: bool = True,
    ) -> SmcSnapshot:
        one = _rows(candles_1m)
        five = _rows(candles_5m)
        fifteen = _rows(candles_15m)
        structures: dict[str, Structure] = {}
        for label, rows in (("1m", one), ("5m", five), ("15m", fifteen)):
            if len(rows) >= 4:
                structures[label] = analyze_structure(rows)
        structure = structures.get("5m") or structures.get("1m") or Structure(
            0, False, False, "", "", 0.0, 0.0, ("insufficient candles",)
        )
        macro = structures.get("15m")
        structure_rows = five if len(five) >= 3 else one
        blocks = detect_order_blocks(structure_rows, futures_1m)
        gaps = detect_fvgs(structure_rows)
        sweeps = detect_liquidity_sweeps(one, obi_5=obi_5)
        zone = build_wait_zone(
            spot=spot, p10=p10, p25=p25,
            order_blocks=blocks, fvgs=gaps, option_atp=option_atp,
        )
        state, reason = wait_state(
            zone, spot=spot, obi_5=obi_5, feed_live=feed_live,
            structure=structure, macro_structure=macro,
        )
        self.triggered = self.triggered or state == "TRIGGER_READY"
        snapshot = SmcSnapshot(
            structure=structure, structures=structures,
            order_blocks=blocks, fvgs=gaps, sweeps=sweeps,
            wait_zone=zone, state=state, reason=reason, triggered=self.triggered,
        )
        self.last = snapshot
        return snapshot
