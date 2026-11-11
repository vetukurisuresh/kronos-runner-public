"""Institutional upgrade tests: parity, MTF, smart pricing and live guards."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from execution.order_router import ModeRejected, OrderRouter
from execution.upstox_live_broker import UpstoxBrokerError, UpstoxLiveBroker
from market.upstox_stream import Candle
from market.upstox_tick_engine import (
    fetch_official_backfill,
    parse_upstox_candle_row,
    parse_v3_frames,
    UpstoxTickEngine,
)
from strategy.mtf_confluence import analyze
from strategy.smart_order_pricer import book_imbalance, smart_limit_price

IST = timezone(timedelta(hours=5, minutes=30))
BASE = 1_760_000_100
KEY = "NSE_INDEX|Nifty 50"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "intraday" in url:
            return FakeResponse({"status": "success", "data": {"candles": [
                ["2026-11-09T09:15:00+05:30", 24990, 25010, 24985, 25005, 12000],
            ]}})
        return FakeResponse({"status": "success", "data": {"candles": [
            ["2026-11-06T09:15:00+05:30", 24900, 24950, 24880, 24940, 9000],
        ]}})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "get-funds-and-margin" in url:
            return FakeResponse({"status": "success", "data": {"equity": {
                "available_margin": 125000.0, "used_margin": 5000.0,
            }}})
        return FakeResponse({"status": "success", "data": {"order_id": "ORD-1"}})


def _candle(close, start=BASE, minutes=1):
    return Candle(start=start, minutes=minutes, open=close - 2, high=close + 3,
                  low=close - 4, close=close, volume=100)


def test_official_row_parsing_and_512_backfill(tmp_path):
    row = ["2026-11-09T09:15:00+05:30", 24990, 25010, 24985, 25005, 12000]
    candle = parse_upstox_candle_row(row, 5)
    assert candle is not None
    assert candle.start == int(datetime(2026, 11, 9, 9, 15, tzinfo=IST).timestamp())
    assert (candle.open, candle.high, candle.low, candle.close) == (24990, 25010, 24985, 25005)
    assert candle.volume == 12000

    calls = []
    bars = fetch_official_backfill(
        KEY, 5, access_token="test-token", days=30, session=FakeSession(calls),
        today=datetime(2026, 11, 9, 12, 0, tzinfo=IST),
    )
    assert len(bars) == 2  # prior day + today's intraday, de-duplicated
    assert {candle.minutes for candle in bars} == {5}
    urls = [call[1] for call in calls]
    assert any("/intraday/" in url and "/minutes/5" in url for url in urls)
    historical = [
        url for url in urls
        if "/historical-candle/" in url and "/intraday/" not in url
    ]
    # The 30-day archive is walked in <=30-day windows, never one wide request.
    assert len(historical) == 2
    assert any(url.endswith("/2026-11-08/2026-11-08") for url in historical)
    assert any("/2026-11-07/2026-10-09" in url for url in historical)


def test_live_engine_buckets_by_exchange_ltt_and_15m_mtf():
    frame = {"feeds": {KEY: {
        "ltpc": {"ltp": 25010.0, "ltt": (BASE + 5) * 1000},
        "ff": {"marketFF": {
            "ltpc": {"ltp": 25010.0, "ltt": (BASE + 5) * 1000},
            "eFeedDetails": {"atp": 25000.0, "oi": 1000000, "vtt": 500},
            "marketLevel": {"bidAskQuote": [{"bidP": 25009.95, "bidQ": 100,
                                             "askP": 25010.05, "askQ": 80}]},
        }},
    }}}
    engine = UpstoxTickEngine()
    engine.on_frame(frame)
    engine.on_tick(KEY, 25020.0, ts_ms=(BASE + 65) * 1000, volume=300, oi=990000)
    # 1m sealed, 15m bucket is still open; no local-clock synthetic bar exists.
    assert len(engine.windows(KEY, 1)) == 1
    assert engine.windows(KEY, 1)[0].start == BASE
    assert engine.windows(KEY, 15) == []
    telemetry = engine.telemetry()[0]
    assert telemetry["ltp"] == 25020.0


def test_seed_replaces_synthetic_history_with_official_candles():
    engine = UpstoxTickEngine()
    official = [_candle(25000 + index, start=BASE + index * 60, minutes=1) for index in range(520)]
    assert engine.seed(KEY, 1, official) == 512
    bars = engine.windows(KEY, 1)
    assert len(bars) == 512
    assert bars[-1].start == BASE + 519 * 60
    with pytest.raises(ValueError):
        engine.seed(KEY, 3, official)


def test_mtf_every_interval_is_analyzed_in_parallel():
    up_1m = [_candle(24900 + index, start=BASE + index * 60) for index in range(25)]
    up_5m = [_candle(24800 + index, start=BASE + index * 300, minutes=5) for index in range(25)]
    up_15m = [_candle(24700 + index, start=BASE + index * 900, minutes=15) for index in range(25)]
    result = analyze(up_1m, up_5m, up_15m, paths_up=82, samples=100)
    assert result["alignment"] == "BULL"
    assert result["frames"]["1m"]["trend"] == "BULL"
    assert result["frames"]["15m"]["trend"] == "BULL"
    assert result["pill"] == "MTF SYNC: [1m: 🟢 BULL] [5m: 🟢 BULL] [15m: 🟢 BULL]"
    assert result["score"] == 3

    mixed = analyze(up_1m, [dict(c.__dict__) for c in up_5m], list(reversed(up_15m)))
    assert mixed["aligned"] is False or mixed["alignment"] != "BULL"


def test_smart_pricer_uses_depth_and_mtf_gate():
    depth = book_imbalance(
        [{"price": 100.0, "qty": 500}, {"price": 99.9, "qty": 300}],
        [{"price": 100.1, "qty": 100}, {"price": 100.2, "qty": 100}],
    )
    assert depth["bid_qty"] == 800 and depth["ask_qty"] == 200
    assert depth["ratio"] == 4.0 and depth["imbalance"] > 0

    blocked = smart_limit_price(bid=100.0, ask=100.2, mtf="MIXED")
    assert blocked["ok"] is False
    aligned = smart_limit_price(
        bid=100.0, ask=100.2, bids=[{"price": 100.0, "qty": 500}],
        asks=[{"price": 100.2, "qty": 100}], atp_vwap=99.9, p25_dip=99.8,
        mtf={"alignment": "BULL"}, oi_change_5m_pct=-3.0,
    )
    assert aligned["ok"] is True
    assert aligned["price"] in (100.0, 100.05, 100.1, 100.15, 100.2)
    assert aligned["side"] == "CE"


def test_real_mode_requires_submit_and_live_margin(tmp_path):
    session = FakeSession([])
    broker = UpstoxLiveBroker(access_token="test-token", session=session)
    router = OrderRouter(mode_path=tmp_path / "mode.json", broker=broker)
    assert router.snapshot()["mode"] == "PAPER"
    with pytest.raises(ModeRejected):
        router.set_mode("REAL", confirm=False)
    snapshot = router.set_mode("REAL", confirm=True)
    assert snapshot["mode"] == "REAL" and snapshot["confirmed"] is True
    assert snapshot["available_margin"] == 125000.0
    # A fresh router sees the persisted confirmation, not a silent reset.
    again = OrderRouter(mode_path=tmp_path / "mode.json", broker=broker)
    assert again.load()["confirmed"] is True

    paper = router.set_mode("PAPER")
    assert paper["mode"] == "PAPER"
    intraday_ist = datetime(2026, 11, 10, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    placed = router.place_entry(
        trade={"qty": 75, "instrument_key": "NSE_FO|NIFTY"}, price=101.5,
        now=intraday_ist,
    )
    assert placed["mode"] == "PAPER" and placed["filled_price"] == 101.5


def test_broker_order_payload_uses_nse_fo_and_intraday_product():
    class OrderSession:
        def __init__(self):
            self.body = None

        def request(self, method, url, **kwargs):
            self.body = kwargs.get("json")
            return FakeResponse({"status": "success", "data": {"order_id": "LIVE-22"}})

    session = OrderSession()
    broker = UpstoxLiveBroker(access_token="test-token", session=session)
    result = broker.place_order(instrument_key="NSE_FO|12345", quantity=75,
                                transaction_type="BUY", order_type="LIMIT",
                                price=101.5, trigger_price=100.0)
    assert result["order_id"] == "LIVE-22"
    assert session.body["instrument_token"] == "NSE_FO|12345"
    assert session.body["product"] == "I"
    assert session.body["transaction_type"] == "BUY"
    assert session.body["order_type"] == "LIMIT"
    assert session.body["price"] == 101.5
    assert broker.snapshot()["token_present"] is True
    assert "test-token" not in json.dumps(broker.snapshot())


def test_close_router_records_manual_live_exit_and_saves_request(tmp_path, monkeypatch):
    router = OrderRouter(mode_path=tmp_path / "mode.json",
                         broker=UpstoxLiveBroker(access_token="test-token", session=FakeSession([])))
    trade = {"entry_price": 100.0, "qty": 300, "qty_open": 300, "initial_qty": 300,
             "realized_pnl_rs": 0.0, "booked_legs": [], "status": "OPEN"}
    result = router.close_paper_at_live(trade, 112.5)
    assert result["fill_price"] == 112.5
    assert result["pnl"] == 3750.0
    assert result["reason"] == "MANUAL_SMART_CLOSE_AT_LIVE"
    assert trade["status"] == "CLOSED"
    assert trade["exit_reason"] == "MANUAL_SMART_CLOSE_AT_LIVE"

    monkeypatch.setattr("execution.order_router.REPO_ROOT", tmp_path)
    request = router.make_close_request("NIFTY")
    saved = json.loads((tmp_path / "state" / "live_close_request.json").read_text())
    assert saved["request_id"] == request["request_id"]
    assert saved["symbol"] == "NIFTY"
