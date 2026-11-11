"""KRONOS six-feature input construction.

Kronos tokenizers consume ``[open, high, low, close, volume, amount]``.  An
index has no traded volume of its own, so the spot frame is fused with the
front-month futures bars (or a heavyweight composite when futures are not yet
available).  The index OHLC is never replaced; only the two flow columns are
filled from the institutional instrument.
"""

from __future__ import annotations

from typing import Any, Iterable

FEATURES = ("open", "high", "low", "close", "volume", "amount")


def _value(row: Any, names: tuple[str, ...], default: float = 0.0) -> float:
    for name in names:
        if isinstance(row, dict) and row.get(name) is not None:
            try:
                return float(row[name])
            except (TypeError, ValueError):
                pass
        if hasattr(row, name):
            try:
                return float(getattr(row, name))
            except (TypeError, ValueError):
                pass
    return default


def _time(row: Any) -> int:
    return int(_value(row, ("t", "time", "start", "timestamp", "ts")))


def _rows(rows: Iterable[Any] | None) -> list[Any]:
    return [row for row in (rows or []) if row is not None]


def _flow(row: dict[str, Any]) -> tuple[float, float]:
    volume = max(float(row.get("volume") or row.get("v") or 0.0), 0.0)
    amount = max(float(row.get("amount") or row.get("a") or row.get("turnover") or 0.0), 0.0)
    if amount <= 0 and volume > 0:
        price = (float(row.get("high") or row.get("h") or 0.0)
                 + float(row.get("low") or row.get("l") or 0.0)
                 + float(row.get("close") or row.get("c") or 0.0)) / 3.0
        amount = volume * price
    return round(volume, 2), round(amount, 2)


def _normalise(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {
        "time": _time(row),
        "open": _value(row, ("o", "open")),
        "high": _value(row, ("h", "high")),
        "low": _value(row, ("l", "low")),
        "close": _value(row, ("c", "close")),
        "volume": _value(row, ("v", "volume")),
        "amount": _value(row, ("a", "amount", "turnover")),
    }
    if out["time"] <= 0 or out["close"] <= 0:
        return None
    return out


def _flow_map(rows: Iterable[Any] | None) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    for row in _rows(rows):
        stamp = _time(row)
        if stamp <= 0:
            continue
        volume = _value(row, ("v", "volume"))
        amount = _value(row, ("a", "amount", "turnover"))
        if volume <= 0 and amount <= 0:
            continue
        close = _value(row, ("c", "close", "atp", "ltp"))
        if amount <= 0 and volume > 0 and close > 0:
            amount = volume * close
        out[stamp] = (round(max(volume, 0.0), 2), round(max(amount, 0.0), 2))
    return out


def build_ohlcva(
    candles: Iterable[Any] | None,
    funding: Iterable[Any] | None = None,
    *,
    limit: int = 512,
    prefer_funding_flow: bool = True,
) -> list[dict[str, float]]:
    """Build the exact six-column frame, aligning futures flow by bar time.

    ``funding`` is normally the front-month futures series.  The bar's OHLC is
    always the index/option series; futures volume and turnover replace the
    zero index volume.  No random or interpolated volume is manufactured.
    """
    flow = _flow_map(funding)
    out: list[dict[str, float]] = []
    for row in _rows(candles):
        normal = _normalise(row)
        if normal is None:
            continue
        volume, amount = _flow(normal)
        funding_volume, funding_amount = flow.get(normal["time"], (0.0, 0.0))
        if prefer_funding_flow and funding_volume > 0:
            volume, amount = funding_volume, funding_amount
        elif volume <= 0:
            volume, amount = funding_volume, funding_amount
        out.append({
            "time": float(normal["time"]),
            "open": round(normal["open"], 4),
            "high": round(normal["high"], 4),
            "low": round(normal["low"], 4),
            "close": round(normal["close"], 4),
            "volume": round(volume, 2),
            "amount": round(amount, 2),
        })
    return out[-max(1, int(limit)):]


def build_heavyweight_composite(
    series: Iterable[Iterable[Any] | None],
    weights: Iterable[float] | None = None,
) -> list[dict[str, float]]:
    """Aggregate lightweight stock volume/amount by timestamp.

    This is the documented fallback when the futures contract cannot be
    resolved.  The output contains flow only; the caller keeps index OHLC.
    """
    groups = list(series)
    weight_list = list(weights or [1.0] * len(groups))
    by_time: dict[int, dict[str, float]] = {}
    for index, group in enumerate(groups):
        weight = float(weight_list[index]) if index < len(weight_list) else 1.0
        for row in _rows(group):
            normal = _normalise(row)
            if normal is None:
                continue
            volume, amount = _flow(normal)
            slot = by_time.setdefault(normal["time"], {"volume": 0.0, "amount": 0.0})
            slot["volume"] += volume * weight
            slot["amount"] += amount * weight
    return [
        {"time": float(ts), "volume": round(value["volume"], 2),
         "amount": round(value["amount"], 2)}
        for ts, value in sorted(by_time.items())
    ]


def tensor_rows(frame: Iterable[dict[str, Any]]) -> list[list[float]]:
    """Return the model's six-feature tensor as a plain Python matrix."""
    return [[round(float(row.get(name) or 0.0), 4) for name in FEATURES] for row in frame]


def to_numpy(frame: Iterable[dict[str, Any]]):
    """Optional NumPy view with shape ``(bars, 6)``."""
    import numpy as np

    return np.asarray(tensor_rows(frame), dtype="float32")


def build_spot_model_input(
    spot_candles: Iterable[Any] | None,
    futures_candles: Iterable[Any] | None = None,
    heavyweights: Iterable[Iterable[Any] | None] | None = None,
    *,
    limit: int = 512,
) -> list[dict[str, float]]:
    """Convenience wrapper: futures first, heavyweight composite fallback."""
    funding = futures_candles
    if not _rows(funding) and heavyweights:
        funding = build_heavyweight_composite(heavyweights)
    return build_ohlcva(spot_candles, funding, limit=limit)


def build_option_model_input(
    option_candles: Iterable[Any] | None, *, limit: int = 512
) -> list[dict[str, float]]:
    """Option input uses its own traded volume and turnover, never spot flow."""
    return build_ohlcva(option_candles, None, limit=limit)