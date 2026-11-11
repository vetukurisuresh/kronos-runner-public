"""Expiry-day Zero-to-Hero / Gamma Blast engine.

The module is deliberately pure: callers pass the current IST clock, the
11-strike option chain and the live microstructure.  It never reads a clock or
places an order, so every decision can be replayed in tests.

The desk has four IST windows on an index expiry day:

* 09:30-10:35  morning writer-trap reversal (max one setup)
* 10:36-13:20  theta-decay blackout, only a >=90% + 3x-volume blast bypasses it
* 13:20-14:15  afternoon Gamma Blast wave 1 (cheap premiums)
* 14:15-15:10  closing short-covering squeeze wave 2 (ultra-cheap premiums)

``rank_hero_candidates`` scores all 11 CE and 11 PE strikes with

    HeroScore = Gamma * dS^2 / max(LTP, 5) * OI_Unwind * OrderFlow

and rejects far-OTM, out-of-band or fuel-less lottery tickets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

IST = timezone(timedelta(hours=5, minutes=30))

EXPIRY_MODES = ("AUTO", "FORCE_ON", "OFF")
MAX_HERO_TRADES_PER_DAY = 3

WINDOW_1 = "W1"
WINDOW_2 = "W2"
WINDOW_3 = "W3"
WINDOW_4 = "W4"
WINDOW_CLOSED = "CLOSED"

WINDOW_LABELS = {
    WINDOW_1: "Window 1 · 09:30–10:35 Writer-Trap Reversal",
    WINDOW_2: "Window 2 · 10:36–13:20 Theta Decay Blackout",
    WINDOW_3: "Window 3 · 1:20–2:15 PM Gamma Blast Wave 1",
    WINDOW_4: "Window 4 · 2:15–3:10 PM Closing Squeeze Wave 2",
    WINDOW_CLOSED: "Outside expiry Hero windows",
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_ist(moment: datetime | str | None) -> datetime:
    if moment is None:
        return datetime.now(tz=IST)
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _as_ist(value).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def normalize_mode(mode: Any = "AUTO") -> str:
    """Normalise UI/configuration spellings to ``AUTO``/``FORCE_ON``/``OFF``."""
    text = str(mode or "AUTO").strip().upper().replace("-", "_").replace(" ", "_")
    if text in {"ON", "FORCE", "FORCED", "FORCEON", "FORCE_ON", "TRUE", "1"}:
        return "FORCE_ON"
    if text in {"OFF", "FALSE", "0", "DISABLED", "NONE"}:
        return "OFF"
    return "AUTO"


def expiry_day_status(
    nearest_expiry: Any,
    today: Any = None,
    mode: Any = "AUTO",
) -> dict[str, Any]:
    """Resolve the actual expiry date and the manual AUTO/ON/OFF override."""
    today_d = _as_date(today) or datetime.now(tz=IST).date()
    expiry_d = _as_date(nearest_expiry)
    actual = bool(expiry_d and expiry_d == today_d)
    resolved = normalize_mode(mode)
    if resolved == "FORCE_ON":
        enabled, source = True, "MANUAL_FORCE_ON"
    elif resolved == "OFF":
        enabled, source = False, "MANUAL_OFF"
    else:
        enabled, source = actual, "AUTO_CONTRACT_EXPIRY"
    days_to = (expiry_d - today_d).days if expiry_d else None
    return {
        "symbol": "",
        "today": today_d.isoformat(),
        "nearest_expiry": expiry_d.isoformat() if expiry_d else "",
        "is_expiry_day": actual,
        "enabled": enabled,
        "mode": resolved,
        "source": source,
        "days_to_expiry": days_to,
        "is_weekly_expiry": actual,
    }


# Short alias used by callers and tests.
detect_expiry_day = expiry_day_status


def expiry_context(
    symbol: str,
    nearest_expiry: Any,
    today: Any = None,
    mode: Any = "AUTO",
) -> dict[str, Any]:
    ctx = expiry_day_status(nearest_expiry, today=today, mode=mode)
    ctx["symbol"] = str(symbol or "").upper()
    return ctx


def expiry_window(now: datetime | str | None = None) -> dict[str, Any]:
    """Return the active expiry window and its entry permissions."""
    moment = _as_ist(now)
    minutes = moment.hour * 60 + moment.minute + moment.second / 60.0

    if 9 * 60 + 30 <= minutes < 10 * 60 + 36:
        key = WINDOW_1
        allowed = True
        max_setups = 1
        premium_min, premium_max = 25.0, 65.0
        status = "🟢 MORNING WRITER-TRAP REVERSAL — TRAP FUEL ONLY"
    elif 10 * 60 + 36 <= minutes < 13 * 60 + 20:
        key = WINDOW_2
        allowed = False
        max_setups = 1
        premium_min, premium_max = 8.0, 32.0
        status = "🛡️ THETA SHIELD ACTIVE — WAITING FOR 1:25 PM GAMMA WINDOW"
    elif 13 * 60 + 20 <= minutes < 14 * 60 + 15:
        key = WINDOW_3
        allowed = True
        max_setups = 2
        premium_min, premium_max = 8.0, 32.0
        status = "🟢 Window 3: 1:20–2:15 PM Gamma Wave"
    elif 14 * 60 + 15 <= minutes <= 15 * 60 + 10:
        key = WINDOW_4
        allowed = True
        max_setups = 2
        premium_min, premium_max = 7.0, 20.0
        status = "🟢 Window 4: 2:15–3:10 PM Closing Squeeze"
    else:
        key = WINDOW_CLOSED
        allowed = False
        max_setups = 0
        premium_min, premium_max = 7.0, 65.0
        status = "⏸ OUTSIDE EXPIRY HERO WINDOWS"

    return {
        "window": key,
        "window_id": key,
        "label": WINDOW_LABELS[key],
        "status": status,
        "allowed": allowed,
        "blocked": not allowed,
        "theta_shield": key == WINDOW_2,
        "extreme_only": key == WINDOW_2,
        "max_setups": max_setups,
        "premium_min": premium_min,
        "premium_max": premium_max,
        "minutes": round(minutes, 2),
        "now_ist": moment.isoformat(timespec="seconds"),
    }


def window_for_time(now: datetime | str | None = None) -> dict[str, Any]:
    return expiry_window(now)


def premium_band(now_or_window: datetime | str | dict[str, Any] | None = None) -> tuple[float, float]:
    window = now_or_window if isinstance(now_or_window, dict) else expiry_window(now_or_window)
    return (_f(window.get("premium_min"), 7.0), _f(window.get("premium_max"), 32.0))


def premium_allowed(ltp: Any, window: dict[str, Any] | None = None) -> bool:
    low, high = premium_band(window or expiry_window())
    price = _f(ltp)
    return low - 1e-9 <= price <= high + 1e-9


def strike_step(symbol: str) -> float:
    return 100.0 if str(symbol or "").upper() == "BANKNIFTY" else 50.0


def atm_strike(spot: Any, step: float | None = None) -> float:
    px = _f(spot)
    unit = float(step or 50.0)
    if px <= 0 or unit <= 0:
        return 0.0
    return round(px / unit) * unit


def strike_distance_ok(
    symbol: str,
    side: str,
    strike: Any,
    spot: Any,
    step: float | None = None,
) -> tuple[bool, float, float]:
    """ATM-to-one-strike-OTM filter (55 NIFTY / 150 BANKNIFTY points).

    The ATM strike may be a few points ITM because strikes are 50/100 points
    apart.  Anything more than half a step ITM, or beyond the OTM cap, is junk.
    """
    px, k = _f(spot), _f(strike)
    unit = float(step or strike_step(symbol))
    atm = atm_strike(px, unit)
    if px <= 0 or k <= 0:
        return False, 0.0, atm
    limit = 150.0 if str(symbol or "").upper() == "BANKNIFTY" else 55.0
    distance = (k - px) if str(side or "").upper() == "CE" else (px - k)
    ok = (-unit * 0.5 - 1e-9) <= distance <= limit + 1e-9
    return ok, round(distance, 2), atm


def _oi_change_pct(market: dict[str, Any]) -> float:
    explicit = market.get("oi_change_pct")
    if explicit is not None:
        return _f(explicit)
    delta = _f(market.get("oi_change"))
    oi = _f(market.get("oi"))
    base = max(oi - delta, 1.0)
    return round(delta / base * 100.0, 4) if delta else 0.0


def writer_fuel(
    oi_change_pct: Any,
    opposite_oi_change_pct: Any = 0.0,
) -> dict[str, Any]:
    """Detect short covering or fresh opposite-side writer pressure."""
    own = _f(oi_change_pct)
    opposite = _f(opposite_oi_change_pct)
    unwinding = own <= -4.0
    opposite_fresh = opposite >= 6.0
    if unwinding:
        factor = 1.25 + min(0.35, (abs(own) - 4.0) * 0.02)
        reason = f"5m ΔOI {own:+.1f}% Panic Covering 🔥"
    elif opposite_fresh:
        factor = 1.15 + min(0.20, (opposite - 6.0) * 0.01)
        reason = f"Opposite fresh OI {opposite:+.1f}% 🔥"
    else:
        factor = 0.0
        reason = "No trapped-writer fuel"
    return {
        "ok": bool(unwinding or opposite_fresh),
        "factor": round(factor, 4),
        "oi_change_pct": round(own, 2),
        "opposite_oi_change_pct": round(opposite, 2),
        "unwinding": bool(unwinding),
        "opposite_fresh": bool(opposite_fresh),
        "reason": reason,
    }


def order_flow_factor(obi_5: Any, tbq: Any = 0.0, tsq: Any = 0.0) -> float:
    obi = max(_f(obi_5), 0.0)
    bid_qty, ask_qty = max(_f(tbq), 0.0), max(_f(tsq), 0.0)
    ratio = bid_qty / ask_qty if ask_qty > 0 and bid_qty > 0 else 0.0
    if obi >= 1.65 and ratio >= 1.40:
        return 1.15
    if obi >= 1.20 and ratio >= 1.10:
        return 1.0
    return 0.85


def hero_score(
    gamma: Any,
    kronos_move: Any,
    ltp: Any,
    oi_unwind_factor: Any,
    orderflow_factor: Any,
) -> float:
    """The raw institutional gamma-convexity score (unnormalised)."""
    gamma_f = max(_f(gamma), 0.0)
    move = max(abs(_f(kronos_move)), 0.0)
    premium = max(_f(ltp), 5.0)
    if gamma_f <= 0 or move <= 0:
        return 0.0
    raw = (gamma_f * move * move / premium) * max(_f(oi_unwind_factor), 0.0) * max(
        _f(orderflow_factor), 0.0
    )
    return round(raw, 6)


def hero_score_100(raw_score: Any) -> float:
    """Map the raw score onto the dashboard's 0-100 badge."""
    return round(min(100.0, max(0.0, _f(raw_score) * 200.0)), 1)


def gamma_wave_target(
    entry: Any,
    delta: Any,
    gamma: Any,
    kronos_move: Any,
    *,
    floor_multiple: float = 0.0,
) -> float:
    """Second-order Taylor target used for the p90/Stage-2 take-profit."""
    premium = max(_f(entry), 0.0)
    move = max(abs(_f(kronos_move)), 0.0)
    target = premium + max(_f(delta), 0.0) * move + 0.5 * max(_f(gamma), 0.0) * move * move
    if floor_multiple > 0:
        target = max(target, premium * float(floor_multiple))
    return round(target, 2)


def _leg(row: dict[str, Any], side: str) -> dict[str, Any]:
    key = "call_options" if str(side).upper() == "CE" else "put_options"
    raw = row.get(key) or {}
    leg = raw if isinstance(raw, dict) else {}
    market = leg.get("market_data") or {}
    greeks = leg.get("option_greeks") or {}
    market = market if isinstance(market, dict) else {}
    greeks = greeks if isinstance(greeks, dict) else {}
    return {
        "ltp": _f(market.get("ltp") if market.get("ltp") is not None else leg.get("ltp")),
        "oi": _f(market.get("oi") if market.get("oi") is not None else leg.get("oi")),
        "oi_change_pct": _oi_change_pct(market),
        "delta": _f(greeks.get("delta") if greeks.get("delta") is not None else leg.get("delta")),
        "gamma": abs(_f(greeks.get("gamma") if greeks.get("gamma") is not None else leg.get("gamma"))),
        "iv": _f(greeks.get("iv") if greeks.get("iv") is not None else leg.get("iv")),
        "obi_5": _f(market.get("obi_5") if market.get("obi_5") is not None else leg.get("obi_5")),
        "tbq": _f(market.get("tbq") if market.get("tbq") is not None else leg.get("tbq")),
        "tsq": _f(market.get("tsq") if market.get("tsq") is not None else leg.get("tsq")),
        "instrument_key": str(leg.get("instrument_key") or ""),
    }


@dataclass
class HeroCandidate:
    symbol: str
    strike: float
    side: str
    ltp: float
    delta: float
    gamma: float
    oi: float
    oi_change_pct: float
    opposite_oi_change_pct: float
    distance_points: float
    obi_5: float
    tbq: float
    tsq: float
    hero_score: float
    score_100: float
    eligible: bool
    reason: str
    fuel: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.symbol} {int(self.strike)} {self.side} @ ₹{self.ltp:.2f}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label
        data["strike"] = int(self.strike) if float(self.strike).is_integer() else self.strike
        return data


def _candidate_from_row(
    row: dict[str, Any],
    side: str,
    *,
    symbol: str,
    spot: float,
    window: dict[str, Any],
    kronos_move: float,
) -> HeroCandidate | None:
    strike = _f(row.get("strike_price") if row.get("strike_price") is not None else row.get("strike"))
    if strike <= 0:
        return None
    own = _leg(row, side)
    opposite = _leg(row, "PE" if side == "CE" else "CE")
    if own["ltp"] <= 0:
        return None

    distance_ok, distance, _atm = strike_distance_ok(symbol, side, strike, spot)
    band_ok = premium_allowed(own["ltp"], window)
    fuel = writer_fuel(own["oi_change_pct"], opposite["oi_change_pct"])
    flow = order_flow_factor(own["obi_5"], own["tbq"], own["tsq"])
    raw = hero_score(own["gamma"], kronos_move, own["ltp"], fuel["factor"], flow)
    score_100 = hero_score_100(raw)

    reasons: list[str] = []
    if not distance_ok:
        reasons.append("FAR_OTM_JUNK")
    if not band_ok:
        reasons.append(
            f"PREMIUM_OUT_OF_BAND_{window.get('premium_min')}-{window.get('premium_max')}"
        )
    if own["gamma"] <= 0:
        reasons.append("NO_GAMMA")
    if not fuel["ok"]:
        reasons.append("NO_TRAPPED_WRITER_FUEL")
    eligible = not reasons
    return HeroCandidate(
        symbol=symbol,
        strike=strike,
        side=side,
        ltp=round(own["ltp"], 2),
        delta=round(own["delta"], 4),
        gamma=round(own["gamma"], 6),
        oi=round(own["oi"], 2),
        oi_change_pct=round(own["oi_change_pct"], 2),
        opposite_oi_change_pct=round(opposite["oi_change_pct"], 2),
        distance_points=distance,
        obi_5=round(own["obi_5"], 3),
        tbq=round(own["tbq"], 2),
        tsq=round(own["tsq"], 2),
        hero_score=raw,
        score_100=score_100,
        eligible=eligible,
        reason="ELIGIBLE" if eligible else " · ".join(reasons),
        fuel=fuel,
    )


def rank_hero_candidates(
    chain: Iterable[dict[str, Any]] | None,
    spot: Any,
    symbol: str = "NIFTY",
    now: datetime | str | None = None,
    *,
    window: dict[str, Any] | None = None,
    kronos_move: Any = 45.0,
    paths_up: Any = 0,
    samples: Any = 0,
    max_display: int = 11,
) -> dict[str, Any]:
    """Rank the ATM +/- 5 chain and return the #1 eligible Hero strike."""
    rows = [row for row in (chain or []) if isinstance(row, dict)]
    px = _f(spot)
    symbol = str(symbol or "NIFTY").upper()
    win = window or expiry_window(now)
    move = abs(_f(kronos_move)) or 45.0
    candidates: list[HeroCandidate] = []
    for row in rows:
        for side in ("CE", "PE"):
            cand = _candidate_from_row(
                row, side, symbol=symbol, spot=px, window=win, kronos_move=move
            )
            if cand is not None:
                candidates.append(cand)

    candidates.sort(key=lambda item: (item.eligible, item.score_100, item.hero_score, -item.ltp), reverse=True)
    eligible = [item for item in candidates if item.eligible]
    hero = eligible[0] if eligible else None
    watch = candidates[0] if candidates else None
    paths_pct = (
        round(_f(paths_up) / _f(samples) * 100.0, 2) if _f(samples) > 0 else 0.0
    )
    fuel_count = sum(1 for item in candidates if item.fuel.get("ok"))
    return {
        "ok": bool(rows and px > 0),
        "symbol": symbol,
        "spot": round(px, 2),
        "atm_strike": atm_strike(px, strike_step(symbol)),
        "window": win,
        "paths_pct": paths_pct,
        "scanned": len(candidates),
        "eligible": len(eligible),
        "fuel_count": fuel_count,
        "hero": hero.to_dict() if hero else None,
        "watch": watch.to_dict() if watch else None,
        "candidates": [item.to_dict() for item in candidates[: max(0, int(max_display))]],
        "rejected": {
            "far_otm": sum(1 for item in candidates if "FAR_OTM_JUNK" in item.reason),
            "premium": sum(1 for item in candidates if "PREMIUM_OUT_OF_BAND" in item.reason),
            "no_fuel": sum(1 for item in candidates if "NO_TRAPPED_WRITER_FUEL" in item.reason),
        },
    }


def pick_hero_strike(
    chain: Iterable[dict[str, Any]] | None,
    spot: Any,
    symbol: str = "NIFTY",
    now: datetime | str | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Convenience wrapper returning only the eligible Hero candidate."""
    return rank_hero_candidates(chain, spot, symbol, now, **kwargs).get("hero")


def _conditions(items: list[tuple[str, bool, str]]) -> list[dict[str, Any]]:
    return [
        {"key": key, "pass": bool(ok), "value": value}
        for key, ok, value in items
    ]


def preemptive_trigger(
    candidate: HeroCandidate | dict[str, Any] | None,
    *,
    paths_up: Any = 0,
    samples: Any = 0,
    mini_acceleration: Any = 0.0,
    atp: Any = 0.0,
    rolling_15m_high: Any = 0.0,
    volume_ratio_1m: Any = 0.0,
    obi_5: Any = 0.0,
    tbq: Any = 0.0,
    tsq: Any = 0.0,
    futures_volume_ratio: Any = 0.0,
    basis: Any = 0.0,
    previous_basis: Any = 0.0,
    ignition_low: Any = 0.0,
    window: dict[str, Any] | None = None,
    now: datetime | str | None = None,
    entry: Any = None,
    p90_target: Any = None,
    kronos_move: Any = 45.0,
) -> dict[str, Any]:
    """The five-signature millisecond pre-emptive entry gate.

    A theta-blackout entry is refused unless the blast is extreme: >=90% of
    Kronos paths and >=3x futures volume.  The option stop is always just below
    the 1-minute ignition candle low and must stay inside Rs 3-Rs 6.
    """
    if isinstance(candidate, HeroCandidate):
        cand = candidate.to_dict()
    else:
        cand = dict(candidate or {})
    win = window or expiry_window(now)
    price = _f(entry) or _f(cand.get("ltp"))
    side = str(cand.get("side") or "CE").upper()
    symbol = str(cand.get("symbol") or "NIFTY").upper()
    directional_paths = (
        _f(samples) - _f(paths_up) if side == "PE" else _f(paths_up)
    )
    paths_pct = (
        directional_paths / _f(samples) * 100.0 if _f(samples) > 0 else 0.0
    )
    acceleration = _f(mini_acceleration)
    basis_delta = _f(basis) - _f(previous_basis)
    basis_ok = basis_delta > 0 if side == "CE" else basis_delta < 0 if side == "PE" else False
    extreme = paths_pct >= 90.0 and _f(futures_volume_ratio) >= 3.0

    breakout = price > 0 and (
        (_f(rolling_15m_high) > 0 and price >= _f(rolling_15m_high))
        or (_f(atp) > 0 and price >= _f(atp))
    )
    volume_ok = _f(volume_ratio_1m) >= 2.2
    depth_ok = _f(obi_5) >= 1.65 and _f(tbq) >= 1.40 * _f(tsq) and _f(tsq) > 0
    futures_ok = _f(futures_volume_ratio) >= 2.5 and basis_ok

    raw_sl = price - _f(ignition_low) if _f(ignition_low) > 0 else 0.0
    sl_ok = 3.0 - 1e-9 <= raw_sl <= 6.0 + 1e-9
    sl_points = round(min(6.0, max(3.0, raw_sl)), 2) if sl_ok else 0.0

    conditions = _conditions([
        (
            "kronos_5m_1m_surge",
            paths_pct >= 78.0 and acceleration > 0,
            f"{paths_pct:.0f}% paths · 1m accel {acceleration:+.2f}",
        ),
        (
            "atp_or_15m_reclaim",
            breakout and volume_ok,
            f"LTP ₹{price:.2f} vs ATP ₹{_f(atp):.2f} / 15m high ₹{_f(rolling_15m_high):.2f} · vol {_f(volume_ratio_1m):.2f}x",
        ),
        (
            "obi_ignition",
            depth_ok,
            f"OBI {_f(obi_5):.2f} · TBQ/TSQ {(_f(tbq) / _f(tsq)) if _f(tsq) else 0:.2f}",
        ),
        (
            "futures_velocity",
            futures_ok,
            f"Fut vol {_f(futures_volume_ratio):.2f}x · basis Δ {basis_delta:+.2f}",
        ),
        (
            "tight_expiry_sl",
            sl_ok,
            f"SL ₹{sl_points:.2f} pts (₹3–₹6) below ignition low",
        ),
    ])
    signature_count = sum(1 for item in conditions if item["pass"])
    blocked_reason = ""
    if win.get("extreme_only") and not extreme:
        blocked_reason = "THETA_SHIELD_ACTIVE"
    elif not win.get("allowed") and not win.get("extreme_only"):
        blocked_reason = "OUTSIDE_HERO_WINDOW"
    elif not _f(cand.get("ltp")):
        blocked_reason = "NO_HERO_CANDIDATE"
    elif not cand.get("eligible", True):
        blocked_reason = str(cand.get("reason") or "HERO_NOT_ELIGIBLE")
    elif not sl_ok:
        blocked_reason = "HERO_SL_OUT_OF_RANGE"
    elif signature_count < 5:
        blocked_reason = "HERO_SIGNATURES_INCOMPLETE"

    trigger = signature_count == 5 and not blocked_reason
    target_1 = round(price * 2.0, 2) if price else 0.0
    taylor = (
        _f(p90_target)
        if _f(p90_target) > 0
        else gamma_wave_target(
            price,
            cand.get("delta"),
            cand.get("gamma"),
            kronos_move,
            floor_multiple=2.5,
        )
    )
    return {
        "trigger": bool(trigger),
        "signal": "ZERO_TO_HERO_BUY" if trigger else "WAIT",
        "symbol": symbol,
        "side": side,
        "strike": cand.get("strike"),
        "entry": round(price, 2),
        "stop": round(price - sl_points, 2) if trigger else 0.0,
        "sl_points": sl_points if trigger else 0.0,
        "target_1": target_1,
        "target_2_p90": round(taylor, 2),
        "signature_count": signature_count,
        "signatures": conditions,
        "conditions": conditions,
        "theta_shield": bool(win.get("theta_shield")),
        "extreme_breakout": bool(extreme),
        "reason": "ZERO_TO_HERO_READY" if trigger else blocked_reason,
        "window": win,
    }


def detect_peak_exhaustion(
    entry: Any,
    ltp: Any,
    peak_ltp: Any,
    p90_target: Any = 0.0,
    *,
    obi_5: Any = 1.0,
    tbq: Any = 0.0,
    tsq: Any = 0.0,
    iv_velocity_1m: Any = 0.0,
) -> dict[str, Any]:
    """Book-the-top signatures, valid only once the trade is in profit zone."""
    entry_f, price = _f(entry), _f(ltp)
    peak = max(_f(peak_ltp), price)
    p90 = _f(p90_target)
    in_profit = entry_f > 0 and price >= 1.8 * entry_f
    drawdown_pct = ((peak - price) / peak * 100.0) if peak > 0 else 0.0

    obi_flip = _f(obi_5) < 0.68
    seller_depth = _f(tsq) > 1.55 * _f(tbq) and _f(tsq) > 0
    iv_crush = _f(iv_velocity_1m) < -1.5
    peak_drop = p90 > 0 and peak >= p90 and price <= peak * 0.88

    reasons: list[str] = []
    if obi_flip:
        reasons.append("OBI_FLIP")
    if seller_depth:
        reasons.append("SELLER_DEPTH_SURGE")
    if iv_crush:
        reasons.append("IV_CRUSH")
    if peak_drop:
        reasons.append("PEAK_DRAWDOWN_12PCT")
    peak_exit = bool(in_profit and reasons)
    return {
        "peak_exit": peak_exit,
        "in_profit_zone": bool(in_profit),
        "reasons": reasons,
        "reason": "+".join(reasons) if reasons else "HOLD",
        "peak_ltp": round(peak, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "profit_pct": round((price / entry_f - 1.0) * 100.0, 2) if entry_f > 0 else 0.0,
        "obi_5": round(_f(obi_5), 3),
        "tbq_tsq": round(_f(tbq) / _f(tsq), 3) if _f(tsq) > 0 else 0.0,
        "p90_target": round(p90, 2),
    }


@dataclass
class HeroPeakDetector:
    """Stateful peak hunter for one Hero position."""

    entry: float
    p90_target: float = 0.0
    peak_ltp: float = 0.0
    stage1_done: bool = False
    stage2_done: bool = False
    exited: bool = False
    exit_reason: str = ""

    def __post_init__(self) -> None:
        self.entry = _f(self.entry)
        self.p90_target = _f(self.p90_target)
        if self.peak_ltp <= 0:
            self.peak_ltp = self.entry

    @property
    def peak_gain_pct(self) -> float:
        return round((self.peak_ltp / self.entry - 1.0) * 100.0, 2) if self.entry > 0 else 0.0

    def update(
        self,
        ltp: Any,
        *,
        obi_5: Any = 1.0,
        tbq: Any = 0.0,
        tsq: Any = 0.0,
        iv_velocity_1m: Any = 0.0,
        p90_target: Any = None,
    ) -> dict[str, Any]:
        price = _f(ltp)
        if _f(p90_target) > 0:
            self.p90_target = _f(p90_target)
        self.peak_ltp = max(self.peak_ltp, price)
        detection = detect_peak_exhaustion(
            self.entry,
            price,
            self.peak_ltp,
            self.p90_target,
            obi_5=obi_5,
            tbq=tbq,
            tsq=tsq,
            iv_velocity_1m=iv_velocity_1m,
        )
        stage1 = bool(not self.stage1_done and self.entry > 0 and price >= 2.0 * self.entry)
        stage2 = bool(
            not self.stage2_done
            and self.p90_target > 0
            and price >= self.p90_target
        )
        if stage1:
            self.stage1_done = True
        if stage2:
            self.stage2_done = True
        if detection["peak_exit"] and not self.exited:
            self.exited = True
            self.exit_reason = detection["reason"]
        return {
            **detection,
            "stage1": stage1,
            "stage2": stage2,
            "peak_exit": bool(detection["peak_exit"] and not self.exited),
            "already_exited": self.exited and not detection["peak_exit"],
            "stage1_done": self.stage1_done,
            "stage2_done": self.stage2_done,
            "exit_reason": self.exit_reason,
            "peak_gain_pct": self.peak_gain_pct,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "entry": round(self.entry, 2),
            "p90_target": round(self.p90_target, 2),
            "peak_ltp": round(self.peak_ltp, 2),
            "peak_gain_pct": self.peak_gain_pct,
            "stage1_done": self.stage1_done,
            "stage2_done": self.stage2_done,
            "exited": self.exited,
            "exit_reason": self.exit_reason,
        }


def hero_scan_snapshot(scan: dict[str, Any] | None) -> dict[str, Any]:
    """Compact scan object suitable for the live payload."""
    data = dict(scan or {})
    hero = data.get("hero") or {}
    watch = data.get("watch") or {}
    return {
        "ok": bool(data.get("ok")),
        "window": data.get("window") or {},
        "spot": data.get("spot"),
        "atm_strike": data.get("atm_strike"),
        "scanned": int(data.get("scanned") or 0),
        "eligible": int(data.get("eligible") or 0),
        "fuel_count": int(data.get("fuel_count") or 0),
        "hero": hero or None,
        "watch": watch or None,
        "rejected": data.get("rejected") or {},
        "paths_pct": data.get("paths_pct") or 0.0,
    }
