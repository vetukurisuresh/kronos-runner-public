"""Intra-candle sub-second scalping engine (KRONOS-4M captain, 25M cache).

Everything here is deterministic and driven by the ticks/books it is given:

* ``arm_strike_cache``  pre-arms the 1-strike ITM CE/PE and the OI walls.
* ``detect_mode``       picks SHOCKWAVE / PING-PONG / WICK-SNAPBACK inside the
  forming candle, without ever waiting for a candle close.
* ``PeakTickRatchet``   books profit tick-by-tick on the peak, so a +₹18 spike
  cannot turn into a loss when the wick snaps back.

No order is emitted from this module; callers decide what to do with the
``action`` it returns.  Missing inputs are reported as ``None`` and never
invented.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

ITM_DELTA_BAND = (0.54, 0.65)
SHOCKWAVE_VELOCITY = 5.0          # spot points per second
SHOCKWAVE_LOOKBACK_SEC = 1.5
SHOCKWAVE_OBI = 1.55
SHOCKWAVE_TBQ = 1.50
MAX_SPREAD_PCT = 0.5
PINGPONG_OBI = 1.45
PINGPONG_PROXIMITY_PTS = 3.0
WICK_MIN_PTS = 18.0
WICK_OBI_COLLAPSE = 0.45
#: Peak-tick ratchet constants.
RISK_FREE_GAIN = 4.0
RISK_FREE_LOCK = 1.20
T1_GAIN = 7.5
T1_BOOK_FRACTION = 0.60
RATCHET_FLOOR = 1.50
RATCHET_FRACTION = 0.18
EXHAUSTION_OBI = 0.55
EXHAUSTION_MIN_GAIN = 5.0
STAGNATION_SEC = 20.0
STAGNATION_MIN_GAIN = 2.0
SCRATCH_OBI = 0.60


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _row(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _leg(row: Mapping[str, Any], side: str) -> dict[str, Any]:
    leg = _row(row.get("call_options" if side == "CE" else "put_options"))
    market = _row(leg.get("market_data"))
    return {**leg, **market}


def _strike(row: Mapping[str, Any]) -> float:
    return _f(row.get("strike_price") or row.get("strike"))


def arm_strike_cache(
    feed: Mapping[str, Any], symbol: str = "NIFTY", spot: float = 0.0,
    gate: Mapping[str, Any] | None = None, step: float = 0.0,
) -> dict[str, Any]:
    """KRONOS-25M: pre-arm the hot CE/PE contracts and the OI walls."""
    feed = feed or {}
    symbol = str(symbol or "NIFTY").upper()
    rows = [
        _row(row)
        for row in ((feed.get("option_chain") or {}).get(symbol) or [])
        if _row(row)
    ]
    spot = _f(spot) or _f((_row((feed.get("spot") or {}).get(symbol))).get("ltp"))
    calls, puts = [], []
    for row in rows:
        strike = _strike(row)
        if strike <= 0:
            continue
        calls.append((strike, _leg(row, "CE")))
        puts.append((strike, _leg(row, "PE")))

    def hot(legs: Sequence[tuple[float, dict[str, Any]]], want_call: bool) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any]] | None = None
        for strike, leg in legs:
            if spot and (strike >= spot if want_call else strike <= spot):
                continue  # must already carry intrinsic value (1-strike ITM)
            delta = abs(_f(leg.get("delta")))
            if not (ITM_DELTA_BAND[0] <= delta <= ITM_DELTA_BAND[1]):
                continue
            target = abs(delta - 0.60)
            if best is None or target < abs(abs(_f(best[1].get("delta"))) - 0.60):
                best = (strike, leg)
        if best is None:
            return None
        strike, leg = best
        return {
            "symbol": symbol,
            "side": "CE" if want_call else "PE",
            "strike": strike,
            "delta": abs(_f(leg.get("delta"))),
            "ltp": _f(leg.get("ltp")) or None,
            "obi_5": _f(leg.get("obi_5")) or None,
            "spread_pct": round(
                100.0 * max(_f(leg.get("ask")) - _f(leg.get("bid")), 0.0) / max(_f(leg.get("ltp")), 1e-9), 3
            ) if _f(leg.get("ltp")) else None,
        }

    def wall(legs: Sequence[tuple[float, dict[str, Any]]]) -> float | None:
        ranked = [(strike, _f(leg.get("oi"))) for strike, leg in legs if _f(leg.get("oi")) > 0]
        if not ranked:
            return None
        return max(ranked, key=lambda item: item[1])[0]

    ce, pe = hot(calls, True), hot(puts, False)
    gate = _row(gate)
    delta = abs(_f(gate.get("delta")))
    level = _f(gate.get("max_pain")) or None
    fallback = ""
    if not (ce and pe) and ITM_DELTA_BAND[0] <= delta <= ITM_DELTA_BAND[1]:
        # The raw 11-strike window can be empty between chain refreshes while the
        # 25M gate still carries a verified live delta. Arm on that real delta;
        # the strike stays None until the chain repopulates (never invented).
        import math

        step = _f(step)
        ce_strike = math.floor(spot / step) * step if step > 0 and spot else None
        pe_strike = math.ceil(spot / step) * step if step > 0 and spot else None
        stub = {"symbol": symbol, "strike": None, "delta": delta, "ltp": None,
                "obi_5": _f(gate.get("obi_5")) or None, "spread_pct": None,
                "level": level, "source": "gate_25m_delta_only"}
        ce = ce or {**stub, "side": "CE", "strike": ce_strike}
        pe = pe or {**stub, "side": "PE", "strike": pe_strike}
        fallback = "gate_25m"
    return {
        "symbol": symbol,
        "spot": spot or None,
        "hot_ce_contract": ce,
        "hot_pe_contract": pe,
        "call_wall_spot": wall(calls) or level,
        "put_wall_spot": wall(puts) or level,
        "armed": bool(ce and pe),
        "armed_source": "option_chain" if not fallback else fallback,
        "strike_pending": bool(ce and ce.get("strike") is None),
    }


def tick_velocity(ticks: Sequence[tuple[float, float]], lookback: float = SHOCKWAVE_LOOKBACK_SEC) -> float:
    """Spot points per second over the most recent ``lookback`` window."""
    if len(ticks) < 2:
        return 0.0
    end_t, end_p = ticks[-1]
    start_t, start_p = ticks[0]
    for stamp, price in reversed(ticks):
        if end_t - stamp <= lookback:
            start_t, start_p = stamp, price
    span = end_t - start_t
    if span <= 0:
        return 0.0
    return round((end_p - start_p) / span, 3)


def _box(bars: Sequence[Mapping[str, Any]]) -> tuple[float | None, float | None]:
    closes = sorted(_f(bar.get("c", bar.get("close"))) for bar in bars if _f(bar.get("c", bar.get("close"))) > 0)
    if len(closes) < 5:
        return None, None
    low_index = max(0, int(round(0.15 * (len(closes) - 1))))
    high_index = min(len(closes) - 1, int(round(0.85 * (len(closes) - 1))))
    return closes[low_index], closes[high_index]


def detect_mode(
    *,
    spot_ticks: Sequence[tuple[float, float]],
    option: Mapping[str, Any],
    bars_5m: Sequence[Mapping[str, Any]] = (),
    call_wall: float | None = None,
    put_wall: float | None = None,
    opposite_obi: float | None = None,
) -> dict[str, Any]:
    """Read the forming candle and return the active intra-candle mode."""
    option = option or {}
    ltp = _f(option.get("ltp"))
    vwap = _f(option.get("atp_vwap") or option.get("vwap"))
    obi = _f(option.get("obi_5"))
    tbq = _f(option.get("tbq_tsq"))
    bid, ask = _f(option.get("bid")), _f(option.get("ask"))
    spread_pct = (100.0 * (ask - bid) / ltp) if (ltp and ask >= bid > 0) else None
    velocity = tick_velocity(spot_ticks)
    spot = _f(spot_ticks[-1][1]) if spot_ticks else 0.0
    candle_open = _f((bars_5m[-1] or {}).get("o", (bars_5m[-1] or {}).get("open"))) if bars_5m else 0.0

    # MODE A - shockwave velocity.
    if (
        abs(velocity) >= SHOCKWAVE_VELOCITY
        and obi >= SHOCKWAVE_OBI
        and tbq >= SHOCKWAVE_TBQ
        and ltp > 0
        and (not vwap or ltp > vwap)
        and (spread_pct is None or spread_pct <= MAX_SPREAD_PCT)
    ):
        return {
            "mode": "SHOCKWAVE_VELOCITY_SCALP",
            "side": "CE" if velocity > 0 else "PE",
            "velocity": velocity,
            "obi_5": obi or None,
            "tbq_tsq": tbq or None,
            "spread_pct": spread_pct,
            "reason": f"{velocity:+.1f} pts/s · OBI {obi:.2f}x · TBQ {tbq:.2f}x",
        }

    # MODE B - sideways ping-pong at the micro box edges.
    floor, ceiling = _box(bars_5m)
    if spot and floor and ceiling and ceiling > floor and obi >= PINGPONG_OBI:
        if abs(spot - floor) <= PINGPONG_PROXIMITY_PTS and abs(velocity) < 1.0:
            return {"mode": "SIDEWAYS_PING_PONG_ABSORPTION", "side": "CE", "velocity": velocity,
                    "box_floor": floor, "box_ceiling": ceiling, "obi_5": obi,
                    "reason": f"floor {floor:.2f} · absorption OBI {obi:.2f}x"}
        if abs(spot - ceiling) <= PINGPONG_PROXIMITY_PTS and abs(velocity) < 1.0:
            return {"mode": "SIDEWAYS_PING_PONG_ABSORPTION", "side": "PE", "velocity": velocity,
                    "box_floor": floor, "box_ceiling": ceiling, "obi_5": obi,
                    "reason": f"ceiling {ceiling:.2f} · absorption OBI {obi:.2f}x"}

    # MODE C - wick exhaustion into an OI wall, order book flips.
    if spot and candle_open and obi and opposite_obi is not None:
        stretch = spot - candle_open
        into_call_wall = bool(call_wall and stretch >= WICK_MIN_PTS and spot >= call_wall - 5.0)
        into_put_wall = bool(put_wall and -stretch >= WICK_MIN_PTS and spot <= put_wall + 5.0)
        if (into_call_wall or into_put_wall) and obi < WICK_OBI_COLLAPSE and _f(opposite_obi) >= 1.50:
            return {
                "mode": "WICK_EXHAUSTION_SNAPBACK",
                "side": "PE" if into_call_wall else "CE",
                "velocity": velocity,
                "stretch_pts": round(stretch, 2),
                "obi_5": obi,
                "opposite_obi": opposite_obi,
                "reason": f"wick {stretch:+.1f} pts into wall · OBI {obi:.2f}x → {opposite_obi:.2f}x",
            }
    return {"mode": "SCANNING", "side": None, "velocity": velocity, "reason": "no intra-candle setup"}


@dataclass
class PeakTickRatchet:
    """Tick-by-tick profit protection that never waits for a candle close."""

    entry: float
    qty: int
    mode: str = "SHOCKWAVE_VELOCITY_SCALP"
    sl_pct: float = 0.055
    opened_at: float = 0.0
    sl: float = field(init=False)
    peak: float = field(init=False)
    booked_qty: int = field(default=0, init=False)
    realized: float = field(default=0.0, init=False)
    risk_free: bool = field(default=False, init=False)
    t1_booked: bool = field(default=False, init=False)
    closed: bool = field(default=False, init=False)
    peak_lock: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.entry = round(float(self.entry), 2)
        self.sl = round(self.entry * (1.0 - self.sl_pct), 2)
        self.peak = self.entry

    @property
    def open_qty(self) -> int:
        return max(int(self.qty) - self.booked_qty, 0)

    def ratchet_level(self) -> float:
        trail = max(RATCHET_FLOOR, RATCHET_FRACTION * (self.peak - self.entry))
        return round(self.peak - trail, 2)

    def _book(self, price: float, qty: int, reason: str) -> dict[str, Any]:
        self.booked_qty += qty
        self.realized = round(self.realized + (price - self.entry) * qty, 2)
        return {
            "action": reason,
            "price": round(price, 2),
            "qty": qty,
            "realized": self.realized,
            "open_qty": self.open_qty,
            "peak": round(self.peak, 2),
            "sl": self.sl,
            "ratchet": self.ratchet_level(),
        }

    def step(self, ltp: float, *, obi: float | None = None, now: float | None = None) -> dict[str, Any]:
        return self.on_tick(ltp, obi=obi, now=now)

    def on_tick(self, ltp: float, *, obi: float | None = None, now: float | None = None) -> dict[str, Any]:
        """Manage one tick; returns the action the caller must execute."""
        if self.closed:
            return {"action": "CLOSED", "realized": self.realized, "open_qty": 0}
        price = round(float(ltp), 2)
        self.peak = max(self.peak, price)
        gain = price - self.entry

        if not self.risk_free and self.peak - self.entry >= RISK_FREE_GAIN:
            self.risk_free = True
            self.sl = max(self.sl, round(self.entry + RISK_FREE_LOCK, 2))
            self.peak_lock = round(self.peak - 1.50, 2)

        if not self.t1_booked and price - self.entry >= T1_GAIN:
            qty = max(int(round(self.qty * T1_BOOK_FRACTION)), 1)
            self.t1_booked = True
            self.peak_lock = self.ratchet_level()
            return self._book(price, min(qty, self.open_qty), "T1_BOOK_PEAK_RATCHET")

        if self.t1_booked:
            self.peak_lock = self.ratchet_level()
            if gain >= EXHAUSTION_MIN_GAIN and (
                (obi is not None and obi < EXHAUSTION_OBI) or price <= self.peak_lock
            ):
                return self._book(price, self.open_qty, "EXIT_PEAK_EXHAUSTION")
        elif price <= self.sl:
            self.closed = True
            return self._book(price, self.open_qty, "EXIT_SL")

        if (
            self.mode.startswith("SHOCKWAVE")
            and now is not None
            and self.opened_at is not None
            and now - self.opened_at >= STAGNATION_SEC
            and self.peak - self.entry < STAGNATION_MIN_GAIN
        ):
            self.closed = True
            return self._book(price, self.open_qty, "SCRATCH_STAGNATION")
        if obi is not None and obi < SCRATCH_OBI and gain < STAGNATION_MIN_GAIN:
            self.closed = True
            return self._book(price, self.open_qty, "SCRATCH_OBI_COLLAPSE")
        return {
            "action": "HOLD",
            "price": price,
            "qty": 0,
            "realized": self.realized,
            "open_qty": self.open_qty,
            "peak": round(self.peak, 2),
            "sl": self.sl,
            "ratchet": self.ratchet_level(),
            "peak_lock": self.peak_lock,
        }


class TickWindow:
    """Rolling 1.5s per-symbol tick window for intra-candle velocity."""

    def __init__(self, maxlen: int = 32) -> None:
        self.rows: deque[tuple[float, float, float, float]] = deque(maxlen=maxlen)

    def add(self, ts: float, spot: float, option_ltp: float = 0.0, obi: float = 0.0) -> None:
        if _f(spot) <= 0:
            return
        self.rows.append((float(ts), float(spot), _f(option_ltp), _f(obi)))

    def _pair(self, index: int) -> tuple[float, float]:
        if len(self.rows) < 2:
            return 0.0, 0.0
        end = self.rows[-1]
        start = self.rows[0]
        for row in reversed(self.rows):
            if end[0] - row[0] <= SHOCKWAVE_LOOKBACK_SEC:
                start = row
        span = end[0] - start[0]
        if span <= 0:
            return 0.0, 0.0
        return end[index] - start[index], span

    def spot_velocity(self) -> float:
        delta, span = self._pair(1)
        return round(delta / span, 3) if span else 0.0

    def option_velocity(self) -> float:
        delta, span = self._pair(2)
        return round(delta / span, 2) if span else 0.0

    def spot_series(self) -> list[tuple[float, float]]:
        return [(row[0], row[1]) for row in self.rows]

    def last_obi(self) -> float:
        return self.rows[-1][3] if self.rows else 0.0


def plan_scalp_order(
    mode: Mapping[str, Any], contract: Mapping[str, Any], entry: float, qty: int
) -> dict[str, Any] | None:
    """Turn a mode trigger into the exact paper order the executor may place."""
    mode = _row(mode)
    contract = _row(contract)
    entry = _f(entry)
    if not mode.get("mode") or mode.get("mode") == "SCANNING" or entry <= 0 or int(qty) <= 0:
        return None
    side = mode.get("side") or contract.get("side")
    if side not in ("CE", "PE") or not contract.get("strike"):
        return None          # no strike means nothing real to execute
    return {
        "mode": mode["mode"], "side": side, "symbol": contract.get("symbol"),
        "strike": contract.get("strike"), "instrument_key": contract.get("instrument_key"),
        "entry": round(entry, 2), "qty": int(qty),
        "sl": round(entry * 0.945, 2), "t1": round(entry + 7.50, 2),
        "t2": round(entry + 15.00, 2), "sl_pct": -5.5,
        "ratchet": "peak - max(1.50, 0.18 * gain)", "executor": "paper",
    }


__all__ = [
    "PeakTickRatchet",
    "TickWindow",
    "arm_strike_cache",
    "detect_mode",
    "plan_scalp_order",
    "tick_velocity",
]
