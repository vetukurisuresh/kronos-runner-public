"""Upstox V3 market-data feed and 1m/5m candle aggregation.

The websocket transport is optional (``websocket-client``); the aggregation
logic is pure Python so it can be unit-tested and replayed offline.

Upstox V3 sends protobuf ``FeedResponse`` frames. Decoding requires the bundled
proto module; when it is unavailable we fall back to JSON frames, which is what
the replay harness and the sandbox bridge emit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from config.settings import env_flag

try:  # optional transport
    import websocket
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

V3_FEED_URL = "wss://api.upstox.com/v3/feed/market-data-feed"


@dataclass
class Candle:
    start: int  # epoch seconds, aligned to the interval
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 1
    amount: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> float:
        return max(self.high - self.low, 0.0)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "start": self.start,
            "minutes": self.minutes,
            "open": round(self.open, 2),
            "high": round(self.high, 2),
            "low": round(self.low, 2),
            "close": round(self.close, 2),
            "volume": round(self.volume, 2),
            "amount": round(self.amount, 2),
            "ticks": self.ticks,
        }


@dataclass
class _Bucket:
    key: int
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 1
    amount: float = 0.0

    def update(self, price: float, volume: float, amount: float = 0.0) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume
        self.amount += amount
        self.ticks += 1

    def freeze(self) -> Candle:
        return Candle(
            start=self.key,
            minutes=self.minutes,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            ticks=self.ticks,
            amount=self.amount,
        )


class CandleAggregator:
    """Rolls ticks into completed candles for several intervals at once."""

    def __init__(self, intervals: Iterable[int] = (1, 5)) -> None:
        self.intervals = tuple(sorted({int(minutes) for minutes in intervals}))
        if not self.intervals or min(self.intervals) < 1:
            raise ValueError("intervals must be positive minute counts")
        self._open: dict[int, _Bucket] = {}
        self.completed: dict[int, list[Candle]] = {minutes: [] for minutes in self.intervals}

    @staticmethod
    def _align(epoch: int, minutes: int) -> int:
        step = minutes * 60
        return int(epoch) - (int(epoch) % step)

    def add_tick(self, epoch: int, price: float, volume: float = 0.0) -> list[Candle]:
        """Feed one tick; returns every candle that closed because of it."""
        if price <= 0:
            raise ValueError("price must be positive")
        closed: list[Candle] = []
        for minutes in self.intervals:
            key = self._align(epoch, minutes)
            bucket = self._open.get(minutes)
            if bucket is None:
                self._open[minutes] = _Bucket(key, minutes, price, price, price, price, volume)
                continue
            if bucket.key != key:
                closed.append(self._close(minutes))
                self._open[minutes] = _Bucket(key, minutes, price, price, price, price, volume)
            else:
                bucket.update(price, volume)
        return closed

    def _close(self, minutes: int) -> Candle:
        candle = self._open[minutes].freeze()
        self.completed[minutes].append(candle)
        return candle

    def flush(self) -> list[Candle]:
        """Force-close every open bucket (end of session)."""
        closed = [self._close(minutes) for minutes in self._open]
        self._open.clear()
        return closed

    def history(self, minutes: int, count: int = 20) -> list[Candle]:
        return self.completed.get(minutes, [])[-count:]


@dataclass
class FeedState:
    """Mutable snapshot shared with the dashboard."""

    last_price: float = 0.0
    last_update: float = 0.0
    ticks: int = 0
    candles_1m: list[Candle] = field(default_factory=list)
    candles_5m: list[Candle] = field(default_factory=list)


def parse_v3_message(raw: str | bytes) -> dict[str, float] | None:
    """Extract a usable tick from an Upstox V3 frame.

    Protobuf frames are not decoded here (that is the SDK's job); JSON frames are
    parsed directly so the same code path powers replay and sandbox testing.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None

    feeds = payload.get("feeds") or {}
    for feed in feeds.values():
        market = (feed or {}).get("ff", {}).get("marketFF") or (feed or {}).get("ltpc")
        if not market:
            continue
        price = market.get("ltp")
        if price is None:
            continue
        ts = market.get("ltt") or int(time.time() * 1000)
        return {"price": float(price), "volume": float(market.get("v", 0) or 0), "ts_ms": float(ts)}
    return None


class UpstoxStream:
    """Thin websocket wrapper; all strategy logic stays outside."""

    def __init__(
        self,
        access_token: str,
        on_candle: Callable[[Candle], None] | None = None,
        intervals: Iterable[int] = (1, 5),
    ) -> None:
        self.access_token = access_token
        self.on_candle = on_candle
        self.aggregator = CandleAggregator(intervals)
        self.state = FeedState()

    def connect(self):  # pragma: no cover - requires network
        if websocket is None:
            raise RuntimeError("websocket-client is not installed; install requirements.txt")
        if not self.access_token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing")
        return websocket.create_connection(
            V3_FEED_URL,
            header=[f"Authorization: Bearer {self.access_token}"],
            timeout=10,
        )

    def handle_frame(self, raw: str | bytes) -> list[Candle]:
        tick = parse_v3_message(raw)
        if tick is None:
            return []
        closed = self.aggregator.add_tick(
            int(tick["ts_ms"] // 1000), tick["price"], tick["volume"]
        )
        self.state.last_price = tick["price"]
        self.state.last_update = tick["ts_ms"] / 1000.0
        self.state.ticks += 1
        self.state.candles_1m = self.aggregator.history(1, 100)
        self.state.candles_5m = self.aggregator.history(5, 100)
        for candle in closed:
            if self.on_candle is not None:
                self.on_candle(candle)
        return closed

    def live_enabled(self) -> bool:
        return env_flag("UPSTOX_LIVE_FEED")


if __name__ == "__main__":  # pragma: no cover
    aggregator = CandleAggregator()
    base = 1_760_000_000
    for index, price in enumerate([100, 101, 102, 99, 103, 104]):
        closed = aggregator.add_tick(base + index * 20, float(price), 10)
        for candle in closed:
            print(candle.as_dict())
