"""Expired options handling for NSE index derivatives.

NSE weekly/monthly contracts expire on Thursday (moved earlier when Thursday is a
holiday — that correction needs the exchange calendar, which is injected here
rather than hard-coded). This module owns: expiry arithmetic, contract symbol
construction, an on-disk cache, and a guarded fetch routine.

It is intentionally offline-first: nothing reaches the network unless a token is
supplied AND ``allow_network`` is explicitly true.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from config.settings import IST, LOG_DIR

EXPIRED_CACHE = LOG_DIR / "expired_options.json"
EXPIRY_WEEKDAY = 3  # Thursday


def last_weekday_of_month(year: int, month: int, weekday: int = EXPIRY_WEEKDAY) -> date:
    """Last given weekday (3 = Thursday) of a month."""
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    cursor = first_next - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def weekly_expiries(today: date | None = None, weeks: int = 12) -> list[date]:
    """Upcoming Thursday expiries from ``today`` (inclusive), holiday-adjusted later."""
    today = today or datetime.now(tz=IST).date()
    cursor = today
    while cursor.weekday() != EXPIRY_WEEKDAY:
        cursor += timedelta(days=1)
    days = 7 if cursor < today else 0
    start = cursor + timedelta(days=days)
    return [start + timedelta(weeks=offset) for offset in range(max(weeks, 1))]


def next_expiry(today: date | None = None, holiday_shift: dict[date, date] | None = None) -> date:
    """Nearest expiry, optionally shifted by an exchange holiday map."""
    holiday_shift = holiday_shift or {}
    for candidate in weekly_expiries(today, weeks=4):
        shifted = holiday_shift.get(candidate, candidate)
        if shifted >= (today or datetime.now(tz=IST).date()):
            return shifted
    raise RuntimeError("no expiry found in the next four weeks")


def is_expiry_day(today: date | None = None, holiday_shift: dict[date, date] | None = None) -> bool:
    today = today or datetime.now(tz=IST).date()
    return next_expiry(today, holiday_shift) == today


def expired_symbol(underlying: str, expiry: date, strike: int, option_type: str) -> str:
    """NSE historical contract key, e.g. NIFTY24O1024800CE."""
    option_type = option_type.upper()
    if option_type not in {"CE", "PE"}:
        raise ValueError("option_type must be CE or PE")
    tag = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}.get(underlying.upper(), underlying.upper())
    return f"{tag}{expiry:%y}{'O' if _is_monthly(expiry) else ''}{expiry:%d}{int(strike)}{option_type}"


def _is_monthly(expiry: date) -> bool:
    return expiry == last_weekday_of_month(expiry.year, expiry.month)


@dataclass(frozen=True)
class ExpiredContract:
    symbol: str
    underlying: str
    expiry: date
    strike: int
    option_type: str

    @classmethod
    def build(cls, underlying: str, expiry: date, strike: int, option_type: str) -> "ExpiredContract":
        return cls(
            symbol=expired_symbol(underlying, expiry, strike, option_type),
            underlying=underlying.upper(),
            expiry=expiry,
            strike=int(strike),
            option_type=option_type.upper(),
        )


def _load_cache() -> dict:
    try:
        if EXPIRED_CACHE.is_file():
            return json.loads(EXPIRED_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        EXPIRED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXPIRED_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


def cached_candles(symbol: str) -> list[dict] | None:
    return _load_cache().get(symbol)


def cache_candles(symbol: str, candles: Iterable[dict]) -> None:
    cache = _load_cache()
    cache[symbol] = list(candles)
    _save_cache(cache)


def fetch_expired_candles(
    contract: ExpiredContract,
    access_token: str | None = None,
    allow_network: bool = False,
    timeout: float = 15.0,
) -> list[dict]:
    """Fetch 1-minute historical candles for an expired contract.

    Returns cached data when available. Without an explicit ``allow_network``
    flag the function refuses to make a request — backtests must be repeatable.
    """
    cached = cached_candles(contract.symbol)
    if cached is not None:
        return cached

    if not allow_network or not access_token:
        raise PermissionError(
            "network access for expired contracts requires allow_network=True "
            "and an Upstox access token"
        )

    import requests  # local import keeps module import light

    url = "https://api.upstox.com/v2/historical-candle"
    params = {
        "instrument_key": contract.symbol,
        "interval": "1minute",
        "to_date": contract.expiry.isoformat(),
        "from_date": (contract.expiry - timedelta(days=5)).isoformat(),
    }
    response = requests.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    candles = response.json().get("data", {}).get("candles", [])
    cache_candles(contract.symbol, candles)
    return candles


if __name__ == "__main__":  # pragma: no cover
    today = datetime.now(tz=IST).date()
    print(f"today      : {today}")
    print(f"next expiry: {next_expiry(today)} (expiry day={is_expiry_day(today)})")
    sample = ExpiredContract.build("NIFTY", next_expiry(today), 24800, "CE")
    print(f"contract   : {sample.symbol}")
