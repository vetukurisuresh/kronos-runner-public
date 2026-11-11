"""Institutional smart pre-emptive order pricer.

The pricer avoids chasing a late spike. It combines:

* top-5 bid/ask depth imbalance,
* the option's running ATP/VWAP,
* the Kronos 1m p25 dip,
* the 1m + 5m + 15m confluence state,
* 5-minute OI unwinding velocity.

For a micro-pullback we rest at ``best_bid + 1 tick``. When writers are covering
with positive velocity we cross to ``best_ask`` so the fill is not missed.
"""

from __future__ import annotations

from typing import Any, Iterable

TICK_SIZE = 0.05
BULL = "BULL"
BEAR = "BEAR"


def _levels(rows: Iterable[Any] | None, side: str) -> list[tuple[float, float]]:
    """Normalize depth levels to ``[(price, quantity), ...]``."""
    out: list[tuple[float, float]] = []
    for row in rows or []:
        if isinstance(row, dict):
            price = row.get("price", row.get("bidP", row.get("askP", row.get("p"))))
            qty = row.get("quantity", row.get("qty", row.get("bidQ", row.get("askQ", row.get("q")))))
        else:
            try:
                price, qty = row[0], row[1]
            except (IndexError, TypeError):
                continue
        try:
            price_f, qty_f = float(price), float(qty)
        except (TypeError, ValueError):
            continue
        if price_f > 0 and qty_f > 0:
            out.append((price_f, qty_f))
    out.sort(key=lambda level: level[0], reverse=(side == "bid"))
    return out[:5]


def book_imbalance(
    bids: Iterable[Any] | None,
    asks: Iterable[Any] | None,
) -> dict[str, float]:
    """Return top-5 bid/ask quantities, ratio and normalized imbalance.

    ``ratio > 1`` means buyers dominate. The normalized value is in ``[-1, 1]``
    and is safe to display directly in the scanner.
    """
    bid_levels = _levels(bids, "bid")
    ask_levels = _levels(asks, "ask")
    bid_qty = sum(qty for _, qty in bid_levels)
    ask_qty = sum(qty for _, qty in ask_levels)
    ratio = bid_qty / ask_qty if ask_qty > 0 else (float("inf") if bid_qty > 0 else 1.0)
    total = bid_qty + ask_qty
    imbalance = (bid_qty - ask_qty) / total if total > 0 else 0.0
    return {
        "bid_qty": round(bid_qty, 2),
        "ask_qty": round(ask_qty, 2),
        "ratio": round(ratio, 4) if ratio != float("inf") else 999.0,
        "imbalance": round(imbalance, 4),
        "levels": float(min(len(bid_levels), len(ask_levels))),
    }


def _round_tick(price: float, tick: float = TICK_SIZE) -> float:
    tick = max(float(tick), 0.01)
    return round(round(float(price) / tick) * tick, 2)


def micro_dip_reference(atp_vwap: float | None, p25_dip: float | None) -> float:
    """The pre-emptive pullback floor; never below a valid reference."""
    values = [float(value) for value in (atp_vwap, p25_dip) if value is not None and float(value) > 0]
    return max(values) if values else 0.0


def mtf_is_aligned(mtf: Any, side: str = "CE") -> bool:
    """Accept the analyzer dict or its alignment string."""
    if isinstance(mtf, dict):
        alignment = str(mtf.get("alignment") or "").upper()
        if alignment in ("BULL", "BEAR"):
            return alignment == (BULL if str(side).upper() == "CE" else BEAR)
        return bool(mtf.get("aligned"))
    return str(mtf or "").upper() in (
        (BULL if str(side).upper() == "CE" else BEAR),
    )


def smart_limit_price(
    *,
    bid: float,
    ask: float,
    bids: Iterable[Any] | None = None,
    asks: Iterable[Any] | None = None,
    atp_vwap: float | None = None,
    p25_dip: float | None = None,
    mtf: Any = None,
    side: str = "CE",
    oi_change_5m_pct: float = 0.0,
    high_velocity: bool = False,
    tick: float = TICK_SIZE,
) -> dict[str, Any]:
    """Compute the smart entry limit and a human-readable reason.

    A returned ``ok=False`` means the confluence gate is not aligned or the
    broker quote is unusable; the caller must not place an order.
    """
    bid_f, ask_f = float(bid or 0.0), float(ask or 0.0)
    if bid_f <= 0 or ask_f <= 0:
        return {
            "ok": False,
            "reason": "Top-of-book bid/ask unavailable",
            "price": 0.0,
            "bid": bid_f,
            "ask": ask_f,
            "imbalance": book_imbalance(bids, asks),
        }
    if not mtf_is_aligned(mtf, side):
        return {
            "ok": False,
            "reason": "1m + 5m + 15m MTF alignment is not confirmed",
            "price": 0.0,
            "bid": bid_f,
            "ask": ask_f,
            "imbalance": book_imbalance(bids, asks),
        }

    depth = book_imbalance(bids, asks)
    reference = micro_dip_reference(atp_vwap, p25_dip)
    unwinding = float(oi_change_5m_pct or 0.0) < 0
    aggressive = bool(
        high_velocity
        or (unwinding and depth["ratio"] >= 1.15 and (ask_f - bid_f) <= 4 * max(tick, 0.01))
    )
    if aggressive:
        price = ask_f
        basis = "Best ask on OI unwinding / high velocity"
    else:
        price = bid_f + max(float(tick), 0.01)
        basis = "Best bid + 1 tick on micro-pullback"
        if reference > 0 and ask_f <= reference * 1.003:
            price = max(bid_f + max(float(tick), 0.01), min(ask_f, reference))
            basis = "Micro-pullback at ATP VWAP / Kronos p25 dip"
    price = _round_tick(price, tick)
    # Never put a buy limit above the visible ask; that is a market chase.
    price = min(price, ask_f)
    return {
        "ok": True,
        "price": price,
        "bid": bid_f,
        "ask": ask_f,
        "spread": round(max(ask_f - bid_f, 0.0), 2),
        "reference": round(reference, 2),
        "basis": basis,
        "aggressive": aggressive,
        "oi_unwinding": unwinding,
        "side": side,
        "imbalance": depth,
        "mtf_alignment": (mtf or {}).get("alignment") if isinstance(mtf, dict) else mtf,
    }
