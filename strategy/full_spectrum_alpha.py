"""Institutional alpha overlay for the full Upstox V3 data stream.

The module is pure: the live session supplies chain, depth, Greeks, futures and
VIX snapshots, while this layer produces deterministic gates and the concise
``full_spectrum`` telemetry object rendered by the dashboard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from config.settings import in_session_window

#: The inspector and PCR are defined over an ATM +/- 5 window (11 strikes).
CHAIN_STRIKES = 11
STALE_AFTER_MS = 5_000
#: Exact fail-closed reasons consumed by the order gate and the dashboard.
MISSING_GREEKS = "MISSING_GREEKS"
MISSING_CHAIN = "MISSING_CHAIN"
MISSING_DEPTH = "MISSING_DEPTH"
STALE_FEED = "STALE_FEED"
BLOCK_REASONS = frozenset({MISSING_GREEKS, MISSING_CHAIN, MISSING_DEPTH, STALE_FEED})
#: Backwards-compatible alias for callers written before the exact reasons.
MISSING_OR_STALE_REASON = STALE_FEED


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row(row: Any) -> dict[str, Any]:
    return row if isinstance(row, dict) else {}


def _strike(row: Any) -> float:
    row = _row(row)
    return _f(row.get("strike_price") if row.get("strike_price") is not None else row.get("strike"))


def _leg_oi(row: dict[str, Any], side: str, key: str) -> float:
    leg = _row(row.get("call_options" if side == "CE" else "put_options"))
    market = _row(leg.get("market_data"))
    return max(_f(market.get(key) if market.get(key) is not None else leg.get(key)), 0.0)


def _leg_change(row: dict[str, Any], side: str) -> float:
    leg = _row(row.get("call_options" if side == "CE" else "put_options"))
    market = _row(leg.get("market_data"))
    return _f(market.get("oi_change") if market.get("oi_change") is not None else leg.get("oi_change"))


def _leg_ltp(row: dict[str, Any], side: str) -> float:
    leg = _row(row.get("call_options" if side == "CE" else "put_options"))
    market = _row(leg.get("market_data"))
    if _f(market.get("ltp")) > 0:
        return _f(market.get("ltp"))
    return _f(leg.get("ltp"))


def chain_option_ltp(rows: Iterable[Any], spot: float, side: str = "CE") -> float:
    """Nearest-ATM option LTP used as the explicit P0-1 price input."""
    candidates = [row for row in (_row(item) for item in rows) if _strike(row) > 0]
    candidates.sort(key=lambda row: abs(_strike(row) - _f(spot)))
    for row in candidates:
        ltp = _leg_ltp(row, str(side).upper())
        if ltp > 0:
            return ltp
    return 0.0


def chain_metrics(rows: Iterable[Any]) -> dict[str, Any]:
    """Aggregate the 11 CE + 11 PE window into PCR, OI and max pain."""
    strikes: list[dict[str, float]] = []
    for raw in rows:
        row = _row(raw)
        strike = _strike(row)
        if strike <= 0:
            continue
        strikes.append({
            "strike": strike,
            "ce_oi": _leg_oi(row, "CE", "oi"),
            "pe_oi": _leg_oi(row, "PE", "oi"),
            "ce_doi": _leg_change(row, "CE"),
            "pe_doi": _leg_change(row, "PE"),
        })
    ce_oi = sum(row["ce_oi"] for row in strikes)
    pe_oi = sum(row["pe_oi"] for row in strikes)
    ce_doi = sum(row["ce_doi"] for row in strikes)
    pe_doi = sum(row["pe_doi"] for row in strikes)
    valid_strikes = sum(1 for row in strikes if row["ce_oi"] > 0 and row["pe_oi"] > 0)
    pain = max_pain(strikes)
    return {
        "strikes": strikes,
        "strike_count": len(strikes),
        "valid_strikes": valid_strikes,
        "ce_oi": round(ce_oi, 2),
        "pe_oi": round(pe_oi, 2),
        "pcr": round(pe_oi / ce_oi, 4) if ce_oi else 0.0,
        "ce_doi": round(ce_doi, 2),
        "pe_doi": round(pe_doi, 2),
        "doi_pcr": round(pe_doi / ce_doi, 4) if ce_doi else 0.0,
        "max_pain": pain,
    }


def max_pain(strikes: Iterable[Any]) -> float:
    """Strike with the lowest aggregate option-writer payout."""
    rows = [row for row in (_row(item) for item in strikes) if _strike(row) > 0]
    if not rows:
        return 0.0
    best_strike = 0.0
    best_pain = float("inf")
    for candidate in rows:
        level = _strike(candidate)
        pain = 0.0
        for row in rows:
            strike = _strike(row)
            pain += max(level - strike, 0.0) * _f(row.get("ce_oi") or row.get("ceOI"))
            pain += max(strike - level, 0.0) * _f(row.get("pe_oi") or row.get("peOI"))
        if pain < best_pain:
            best_pain, best_strike = pain, level
    return round(best_strike, 2)


def _leg_usable(leg: Any) -> bool:
    """A CE/PE leg is structurally valid when it has an identity or a quote."""
    leg = _row(leg)
    if not leg:
        return False
    market = _row(leg.get("market_data"))
    if _f(market.get("ltp")) > 0 or _f(market.get("oi")) > 0:
        return True
    if _f(leg.get("ltp")) > 0 or _f(leg.get("oi")) > 0:
        return True
    # An instrument key without a quote is not a usable chain row.
    return False


def _level_price(row: dict[str, Any]) -> float:
    for name in ("price", "bidP", "askP", "bid", "ask"):
        if row.get(name) is not None:
            return _f(row.get(name))
    return 0.0


def _level_qty(row: dict[str, Any]) -> float:
    for name in ("qty", "bidQ", "askQ", "bid_qty", "ask_qty", "quantity"):
        if row.get(name) is not None:
            return _f(row.get(name))
    return 0.0


def _levels_count(rows: Any) -> int:
    if not isinstance(rows, (list, tuple)):
        return 0
    return sum(1 for raw in rows
               if _level_price(_row(raw)) > 0 and _level_qty(_row(raw)) > 0)


def _levels_usable(rows: Any, minimum: int = 1) -> bool:
    """True when a depth side has at least ``minimum`` priced quantity levels."""
    return _levels_count(rows) >= max(int(minimum), 1)


def valid_chain_strikes(chain: Iterable[Any]) -> int:
    """Strikes that carry both CE and PE open interest."""
    return sum(
        1 for raw in chain
        if _strike(_row(raw)) > 0
        and _leg_oi(_row(raw), "CE", "oi") > 0
        and _leg_oi(_row(raw), "PE", "oi") > 0
    )


def structure_gaps(
    *, chain: Iterable[Any], greeks: dict[str, Any], depth: dict[str, Any]
) -> list[str]:
    """Critical market structures that make every entry unsafe when absent.

    This is deliberately fail-closed: missing/zero Greeks, fewer than eleven
    two-sided chain strikes or fewer than five depth levels are blocking
    conditions, not neutral defaults.
    """
    chain = list(chain)
    gaps: list[str] = []
    greeks = _row(greeks)
    if (_f(greeks.get("iv")) <= 0
            or any(_f(greeks.get(name)) == 0
                   for name in ("delta", "gamma", "theta", "vega"))):
        gaps.append("greeks")
    if valid_chain_strikes(chain) < CHAIN_STRIKES:
        gaps.append("option_chain")
    depth = _row(depth)
    bid_ok = _levels_usable(depth.get("bids"), minimum=5)
    ask_ok = _levels_usable(depth.get("asks"), minimum=5)
    if not (bid_ok and ask_ok):
        gaps.append("depth")
    return gaps


def greeks_gate(
    *,
    delta: float,
    theta: float,
    iv: float,
    iv_velocity_5m: float,
    gamma_velocity_5m: float = 0.0,
    gamma: float = 0.0,
    vega: float = 0.0,
    kronos_move: float = 0.0,
    obi_5: float = 1.0,
    tbq_tsq: float = 1.0,
    theta_horizon_min: float = 15.0,
) -> dict[str, Any]:
    """Block theta/IV-crush buys and grade the expansion setup."""
    missing = [
        name for name, value in (
            ("iv", _f(iv)), ("delta", _f(delta)), ("theta", _f(theta)),
            ("gamma", _f(gamma)), ("vega", _f(vega)),
        )
        if (value <= 0 if name == "iv" else value == 0)
    ]
    if missing:
        return {
            "allow": False,
            "blocked": True,
            "iv_crush": False,
            "theta_pressure": False,
            "theta_15m": 0.0,
            "expected_move": 0.0,
            "iv": round(_f(iv), 2),
            "iv_velocity_5m": round(_f(iv_velocity_5m), 3),
            "gamma_velocity_5m": round(_f(gamma_velocity_5m), 4),
            "conviction": "BLOCKED",
            "reason": MISSING_GREEKS,
            "missing_greeks": missing,
        }
    expected_move = abs(_f(delta) * _f(kronos_move))
    theta_15m = abs(_f(theta)) * max(_f(theta_horizon_min), 1.0) / 375.0
    iv_crush = _f(iv_velocity_5m) < -3.5
    theta_pressure = theta_15m > expected_move > 0
    blocked = bool(iv_crush and theta_pressure)
    a_plus = bool(
        _f(iv_velocity_5m) > 0
        and _f(gamma_velocity_5m) > 0
        and _f(obi_5) > 1.35
        and _f(tbq_tsq) > 1.25
    )
    return {
        "allow": not blocked,
        "blocked": blocked,
        "iv_crush": iv_crush,
        "theta_pressure": theta_pressure,
        "theta_15m": round(theta_15m, 4),
        "expected_move": round(expected_move, 4),
        "iv": round(_f(iv), 2),
        "iv_velocity_5m": round(_f(iv_velocity_5m), 3),
        "gamma_velocity_5m": round(_f(gamma_velocity_5m), 4),
        "conviction": "A+" if a_plus and not blocked else "A" if not blocked else "BLOCKED",
        "reason": "IV crush + theta pressure" if blocked else "IV/theta gate clear",
    }


def zero_chop_gate(
    *,
    paths_up: float | None = None,
    samples: float = 0,
    neutral_pct: float | None = None,
    order_flow_divergence: bool = False,
    obi_5: float = 1.0,
    tbq_tsq: float = 1.0,
) -> dict[str, Any]:
    """Lock new entries when the 100-path box is pure chop with no flow edge."""
    if paths_up is None or _f(samples) <= 0:
        return {
            "locked": False, "status": "TRADE_ALLOWED", "paths_pct": 0.0,
            "neutral": False, "order_flow_divergence": bool(order_flow_divergence),
            "reason": "FORECAST_UNAVAILABLE",
        }
    pct = round(_f(paths_up) / _f(samples) * 100.0, 1)
    neutral = bool(float(neutral_pct) >= 70.0) if neutral_pct is not None else 31.0 <= pct <= 69.0
    divergence = bool(
        order_flow_divergence
        or ((_f(obi_5) >= 1.5 or _f(obi_5) <= 0.65)
            and (_f(tbq_tsq) >= 1.35 or _f(tbq_tsq) <= 0.74))
    )
    locked = bool(neutral and not divergence)
    return {
        "locked": locked,
        "status": "NO_TRADE_CAPITAL_PROTECTED" if locked else "TRADE_ALLOWED",
        "paths_pct": pct,
        "neutral": neutral,
        "order_flow_divergence": divergence,
        "reason": "NO_TRADE_CAPITAL_PROTECTED" if locked else "DIRECTIONAL_OR_FLOW_EDGE",
    }


def basis_filter(
    spot: float, futures_ltp: float, *, previous_basis: float | None = None, side: str = "CE"
) -> dict[str, Any]:
    """Futures basis must support the option side."""
    basis = _f(futures_ltp) - _f(spot)
    rising = previous_basis is None or basis >= _f(previous_basis)
    falling = previous_basis is not None and basis < _f(previous_basis)
    if str(side).upper() == "CE":
        healthy = basis > 0 and rising
    else:
        healthy = basis < 0 or falling
    return {
        "basis": round(basis, 2),
        "basis_change": round(basis - _f(previous_basis), 2) if previous_basis is not None else 0.0,
        "rising": rising,
        "healthy": healthy,
        "side": str(side).upper(),
    }


def vix_filter(vix: float, *, previous_vix: float | None = None, side: str = "CE") -> dict[str, Any]:
    """VIX is a risk-regime confirmation, not a standalone entry trigger."""
    change = _f(vix) - _f(previous_vix) if previous_vix is not None else 0.0
    pct = (change / _f(previous_vix) * 100.0) if _f(previous_vix) else 0.0
    if str(side).upper() == "CE":
        supportive = pct < 1.0
    else:
        supportive = pct > -1.0
    return {
        "ltp": round(_f(vix), 2),
        "change": round(change, 2),
        "change_pct": round(pct, 2),
        "direction": "RISING" if change > 0 else "FALLING" if change < 0 else "FLAT",
        "supportive": supportive,
    }


def evaluate(
    *,
    chain: Iterable[Any],
    spot: float,
    futures_ltp: float,
    previous_basis: float | None = None,
    vix: float = 0.0,
    previous_vix: float | None = None,
    delta: float = 0.0,
    theta: float = 0.0,
    gamma: float = 0.0,
    vega: float = 0.0,
    iv: float = 0.0,
    iv_velocity_5m: float = 0.0,
    gamma_velocity_5m: float = 0.0,
    obi_5: float = 1.0,
    tbq_tsq: float = 1.0,
    kronos_move: float = 0.0,
    side: str = "CE",
    depth: dict[str, Any] | None = None,
    futures_volume: float = 0.0,
    option_ltp: float | None = None,
    stale: bool = False,
    age_ms: int = 0,
    as_of: str = "",
    paths_up: float | None = None,
    samples: float = 0,
    neutral_pct: float | None = None,
    order_flow_divergence: bool = False,
) -> dict[str, Any]:
    chain_rows = list(chain)
    metrics = chain_metrics(chain_rows)
    if option_ltp is None:
        option_ltp = chain_option_ltp(chain_rows, spot, side)
    stale = bool(stale or (int(age_ms or 0) > STALE_AFTER_MS and in_session_window()))
    greeks = greeks_gate(
        delta=delta, theta=theta, gamma=gamma, vega=vega, iv=iv,
        iv_velocity_5m=iv_velocity_5m, gamma_velocity_5m=gamma_velocity_5m,
        kronos_move=kronos_move, obi_5=obi_5, tbq_tsq=tbq_tsq,
    )
    gaps = structure_gaps(
        chain=chain_rows,
        greeks={"iv": iv, "delta": delta, "theta": theta,
                "gamma": gamma, "vega": vega},
        depth=depth or {},
    )
    basis = basis_filter(spot, futures_ltp, previous_basis=previous_basis, side=side)
    vix_state = vix_filter(vix, previous_vix=previous_vix, side=side)
    pcr_support = metrics["pcr"] >= 1.0 if str(side).upper() == "CE" else metrics["pcr"] <= 0.8
    chop = zero_chop_gate(
        paths_up=paths_up, samples=samples, neutral_pct=neutral_pct,
        order_flow_divergence=order_flow_divergence,
        obi_5=obi_5, tbq_tsq=tbq_tsq,
    )

    missing = list(gaps)
    if _f(spot) <= 0:
        missing.append("spot")
    if _f(option_ltp) <= 0:
        missing.append("option.ltp")
    if _f(futures_ltp) <= 0:
        missing.append("futures.ltp")
    if _f(futures_volume) <= 0:
        missing.append("futures.volume")
    if _f(vix) <= 0:
        missing.append("vix.ltp")
    if stale:
        missing.append("stale")

    # Exact, deterministic priority.  A zero quote is a dead feed, never a
    # neutral value; structural gaps keep their own machine-readable reason.
    if stale or _f(spot) <= 0 or _f(option_ltp) <= 0 or _f(futures_ltp) <= 0 \
            or _f(futures_volume) <= 0 or _f(vix) <= 0:
        blocking_reason = STALE_FEED
    elif "greeks" in gaps:
        blocking_reason = MISSING_GREEKS
    elif "option_chain" in gaps:
        blocking_reason = MISSING_CHAIN
    elif "depth" in gaps:
        blocking_reason = MISSING_DEPTH
    else:
        blocking_reason = ""
    data_ok = not missing
    allow = bool(data_ok and greeks["allow"] and not chop["locked"])
    reason = (
        blocking_reason if blocking_reason
        else "NO_TRADE_CAPITAL_PROTECTED" if chop["locked"]
        else greeks["reason"]
    )
    reasons: list[str] = []
    if blocking_reason:
        reasons.append(blocking_reason)
    if chop["locked"]:
        reasons.append("NO_TRADE_CAPITAL_PROTECTED")
    reasons.append(greeks["reason"])
    if not basis["healthy"]:
        reasons.append("futures basis not supporting " + str(side).upper())
    if not pcr_support:
        reasons.append("PCR not supporting " + str(side).upper())
    return {
        "allow_entry": allow,
        "conviction": greeks["conviction"] if allow else "BLOCKED",
        "block_reason": reason if not allow else "",
        "reason": reason,
        "status": "BLOCKED" if (not data_ok or not greeks["allow"]) else chop["status"],
        "zero_chop": chop,
        "data_ok": data_ok,
        "missing_fields": missing,
        "stale": bool(stale),
        "age_ms": int(age_ms or 0),
        "as_of": as_of,
        "option_ltp": round(_f(option_ltp), 2),
        "structure_ok": not gaps,
        "missing_structures": gaps,
        "chain_degraded": metrics["valid_strikes"] != CHAIN_STRIKES,
        "side": str(side).upper(),
        "greeks": greeks,
        "basis": basis,
        "vix": vix_state,
        "pcr_support": pcr_support,
        "basis_support": basis["healthy"],
        "reasons": reasons,
    }


COVERAGE_PATHS = (
    "spot", "option_ltp",
    "futures.ltp", "futures.atp", "futures.volume", "futures.amount",
    "futures.oi", "futures.basis", "depth.obi_5", "depth.bid_qty_5",
    "depth.ask_qty_5", "depth.bid_qty_top", "depth.ask_qty_top",
    "depth.tbq", "depth.tsq", "depth.bids", "depth.asks",
    "pcr.pcr", "pcr.doi_pcr", "pcr.max_pain", "pcr.ce_oi", "pcr.pe_oi",
    "pcr.strike_count", "pcr.valid_strikes",
    "greeks.delta", "greeks.gamma", "greeks.theta", "greeks.vega", "greeks.rho",
    "greeks.iv", "greeks.iv_velocity_5m", "vix.ltp", "breadth.aligned",
    "breadth.total",
    "market_ohlc.1m", "market_ohlc.30m", "market_ohlc.1d",
)
#: Streams that must carry a positive (usable) value, not merely a key.
POSITIVE_PATHS = frozenset({
    "spot", "option_ltp", "futures.ltp", "futures.volume", "futures.amount",
    "greeks.iv", "breadth.aligned", "depth.obi_5", "depth.bid_qty_5",
    "depth.ask_qty_5", "depth.bid_qty_top", "depth.ask_qty_top",
    "pcr.pcr", "pcr.max_pain", "pcr.ce_oi", "pcr.pe_oi", "vix.ltp",
    "breadth.total",
})
NONZERO_PATHS = frozenset({"greeks.delta", "greeks.theta", "greeks.gamma", "greeks.vega"})


def _path(snapshot: dict[str, Any], path: str) -> Any:
    value: Any = snapshot
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _path_present(snapshot: dict[str, Any], path: str) -> bool:
    """Presence is intentionally weaker than validity for audit telemetry."""
    value = _path(snapshot, path)
    return value is not None and value != ""


def _path_usable(snapshot: dict[str, Any], path: str) -> bool:
    """A field counts only when its value is usable, not when a key exists."""
    value = _path(snapshot, path)
    if value is None or value == "":
        return False
    if path.startswith("market_ohlc."):
        bar = _row(value)
        return bool(bar) and all(_f(bar.get(name)) > 0 for name in ("open", "high", "low", "close"))
    if path in {"depth.bids", "depth.asks"}:
        return _levels_usable(value, minimum=5)
    if path == "pcr.strike_count":
        return int(_f(value)) == CHAIN_STRIKES
    if path == "pcr.valid_strikes":
        return int(_f(value)) >= CHAIN_STRIKES
    if path == "breadth.total":
        return int(_f(value)) == 5
    if path in POSITIVE_PATHS:
        return _f(value) > 0
    if path in NONZERO_PATHS:
        return _f(value) != 0
    return True


def field_coverage(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Separate present fields from fields carrying usable live values.

    A full zero snapshot can therefore never advertise 100% coverage, and an
    offline fixture is labelled explicitly even if its values are populated.
    """
    present = [path for path in COVERAGE_PATHS if _path_present(snapshot, path)]
    valid = [path for path in COVERAGE_PATHS if _path_usable(snapshot, path)]
    total = len(COVERAGE_PATHS)
    pct = round(len(valid) / total * 100.0, 1) if total else 0.0
    source = str(snapshot.get("source") or "")
    fixture = bool(snapshot.get("fixture")) or source == "offline-fixture" \
        or bool(snapshot.get("fixture_blocked"))
    stale = bool(snapshot.get("stale"))
    degraded = len(valid) < total or stale
    if fixture:
        status = "OFFLINE_FIXTURE"
    elif degraded:
        status = "DEGRADED"
    else:
        status = "OK"
    label = f"{len(valid)}/{total} valid · {len(present)}/{total} present"
    if status != "OK":
        label += f" · {status}"
    return {
        "present": len(present),
        "valid": len(valid),
        "total": total,
        "pct": pct,
        "missing": [path for path in COVERAGE_PATHS if path not in valid],
        "present_fields": present,
        "valid_fields": valid,
        "degraded": degraded,
        "stale": stale,
        "fixture": fixture,
        "source": source or "upstox-v3",
        "status": status,
        "coverage_label": label,
    }


def build_snapshot(
    *,
    symbol: str,
    spot: float,
    futures: dict[str, Any],
    depth: dict[str, Any],
    chain: Iterable[Any],
    greeks: dict[str, Any],
    vix: dict[str, Any],
    breadth: dict[str, Any],
    market_ohlc: dict[str, Any],
    previous_basis: float | None = None,
    iv_velocity_5m: float = 0.0,
    gamma_velocity_5m: float = 0.0,
    kronos_move: float = 0.0,
    side: str = "CE",
    option_ltp: float | None = None,
    source: str = "upstox-v3",
    fixture: bool = False,
    as_of: str | None = None,
    generated_epoch: float | None = None,
    age_ms: int | None = None,
    stale: bool | None = None,
    now_epoch: float | None = None,
    paths_up: float | None = None,
    samples: float = 0,
    neutral_pct: float | None = None,
    order_flow_divergence: bool = False,
) -> dict[str, Any]:
    """Assemble the single object consumed by the Full-Spectrum Inspector."""
    chain_rows = list(chain)
    metrics = chain_metrics(chain_rows)
    futures_ltp = _f(futures.get("ltp"))
    if option_ltp is None:
        option_ltp = chain_option_ltp(chain_rows, spot, side)
    if age_ms is None:
        age_ms = (max(0.0, float(now_epoch or time.time()) - float(generated_epoch)) * 1000.0
                  if generated_epoch is not None else 0)
    age_ms = max(0, int(age_ms))
    if stale is None:
        stale = bool(age_ms > STALE_AFTER_MS and in_session_window())
    fixture = bool(fixture)
    stale = bool(stale or fixture)
    source = "offline-fixture" if fixture else str(source or "upstox-v3")
    # Heavyweight composite fallback: no synthetic futures volume is invented;
    # the five real heavyweight quotes are summed and the provenance is shown.
    breadth_rows = list(breadth.get("rows") or [])
    composite_volume = sum(
        _f(row.get("volume") if row.get("volume") is not None else row.get("vtt") or row.get("vol"))
        for row in breadth_rows if isinstance(row, dict)
    )
    composite_amount = sum(
        _f(row.get("amount") if row.get("amount") is not None else row.get("turnover"))
        for row in breadth_rows if isinstance(row, dict)
    )
    futures_volume = _f(futures.get("volume"))
    volume_source = "FUT"
    if futures_volume <= 0 and composite_volume > 0:
        futures_volume = composite_volume
        volume_source = "HEAVYWEIGHT_COMPOSITE"
    elif futures_volume <= 0:
        volume_source = "UNAVAILABLE"
    futures_amount = _f(futures.get("amount"))
    amount_source = "FUT"
    if futures_amount <= 0 and composite_amount > 0:
        futures_amount = composite_amount
        amount_source = "HEAVYWEIGHT_COMPOSITE"
    elif futures_amount <= 0:
        amount_source = "UNAVAILABLE"
    # No neutral defaults: an absent book must show as zero and fail the gate.
    obi = _f(depth.get("obi_5"))
    flow = _f(depth.get("tbq_tsq"))
    bid_qty = _f(depth.get("bid_qty_5") if depth.get("bid_qty_5") is not None else depth.get("bid_qty"))
    ask_qty = _f(depth.get("ask_qty_5") if depth.get("ask_qty_5") is not None else depth.get("ask_qty"))
    depth_bids = list(depth.get("bids") or [])
    depth_asks = list(depth.get("asks") or [])
    if bid_qty <= 0:
        bid_qty = sum(_level_qty(_row(row)) for row in depth_bids)
    if ask_qty <= 0:
        ask_qty = sum(_level_qty(_row(row)) for row in depth_asks)
    bid_top = _f(depth.get("bid_qty_top"))
    ask_top = _f(depth.get("ask_qty_top"))
    if bid_top <= 0 and depth_bids:
        bid_top = _level_qty(_row(depth_bids[0]))
    if ask_top <= 0 and depth_asks:
        ask_top = _level_qty(_row(depth_asks[0]))
    snapshot: dict[str, Any] = {
        "symbol": str(symbol).upper(),
        "spot": round(_f(spot), 2),
        "option_ltp": round(_f(option_ltp), 2),
        "as_of": as_of or "",
        "generated_epoch": generated_epoch,
        "updated_epoch": generated_epoch,
        "age_ms": age_ms,
        "stale": bool(stale),
        "fixture": fixture,
        "source": source,
        "chain_degraded": metrics["valid_strikes"] != CHAIN_STRIKES,
        "strike_window": CHAIN_STRIKES,
        "futures": {
            "ltp": round(futures_ltp, 2),
            "atp": round(_f(futures.get("atp")), 2),
            "volume": round(futures_volume, 2),
            "amount": round(futures_amount, 2),
            "oi": round(_f(futures.get("oi")), 2),
            "basis": round(futures_ltp - _f(spot), 2),
            "volume_source": volume_source,
            "amount_source": amount_source,
        },
        "depth": {
            "bids": depth_bids,
            "asks": depth_asks,
            "bid_qty": round(bid_qty, 2),
            "ask_qty": round(ask_qty, 2),
            "bid_qty_top": round(bid_top, 2),
            "ask_qty_top": round(ask_top, 2),
            "obi_5": round(obi, 4),
            "bid_qty_5": round(bid_qty, 2),
            "ask_qty_5": round(ask_qty, 2),
            "tbq": round(_f(depth.get("tbq")), 2),
            "tsq": round(_f(depth.get("tsq")), 2),
            "tbq_tsq": round(flow, 4),
        },
        "pcr": {
            "pcr": metrics["pcr"],
            "doi_pcr": metrics["doi_pcr"],
            "max_pain": metrics["max_pain"],
            "ce_oi": metrics["ce_oi"],
            "pe_oi": metrics["pe_oi"],
            "strike_count": metrics["strike_count"],
            "valid_strikes": metrics["valid_strikes"],
            "chain_degraded": metrics["valid_strikes"] != CHAIN_STRIKES,
        },
        "greeks": {
            "delta": round(_f(greeks.get("delta")), 4),
            "gamma": round(_f(greeks.get("gamma")), 6),
            "theta": round(_f(greeks.get("theta")), 4),
            "vega": round(_f(greeks.get("vega")), 4),
            "rho": round(_f(greeks.get("rho")), 4),
            "iv": round(_f(greeks.get("iv")), 2),
            "iv_velocity_5m": round(_f(iv_velocity_5m), 3),
            "gamma_velocity_5m": round(_f(gamma_velocity_5m), 4),
        },
        "vix": {
            "ltp": round(_f(vix.get("ltp")), 2),
            "change_pct": round(_f(vix.get("change_pct")), 2),
            "direction": str(vix.get("direction") or "FLAT"),
        },
        "breadth": breadth,
        "market_ohlc": market_ohlc,
    }
    snapshot["alpha"] = evaluate(
        chain=chain_rows, spot=spot, futures_ltp=futures_ltp,
        previous_basis=previous_basis, vix=_f(vix.get("ltp")),
        previous_vix=vix.get("previous"), delta=_f(greeks.get("delta")),
        theta=_f(greeks.get("theta")), gamma=_f(greeks.get("gamma")),
        vega=_f(greeks.get("vega")), iv=_f(greeks.get("iv")),
        iv_velocity_5m=iv_velocity_5m,
        gamma_velocity_5m=gamma_velocity_5m, obi_5=obi, tbq_tsq=flow,
        kronos_move=kronos_move, side=side, depth=depth,
        futures_volume=futures_volume, option_ltp=option_ltp,
        stale=bool(stale), age_ms=age_ms, as_of=as_of or "",
        paths_up=paths_up, samples=samples,
        neutral_pct=neutral_pct, order_flow_divergence=order_flow_divergence,
    )
    snapshot["status"] = snapshot["alpha"]["status"]
    snapshot["paths_pct"] = snapshot["alpha"]["zero_chop"]["paths_pct"]
    snapshot["coverage"] = field_coverage(snapshot)
    return snapshot