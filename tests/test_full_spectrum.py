"""Full V3 feed, six-feature KRONOS tensor and institutional alpha tests."""

from __future__ import annotations

import json
import time

import pytest

from market import live_ticks
from market.upstox_full_decoder import (
    FullSpectrumBook,
    decode_frame,
    option_window_keys,
    select_front_month_futures,
    select_option_window,
)
from market.upstox_tick_engine import UpstoxTickEngine
from model.kronos_inputs import FEATURES, build_ohlcva, build_spot_model_input, to_numpy
from strategy.full_spectrum_alpha import (
    build_snapshot,
    chain_metrics,
    evaluate,
    field_coverage,
    greeks_gate,
)

PROTO_KEY = "NSE_FO|TEST"


def _proto_frame():
    import market.proto.MarketDataFeedV3_pb2 as pb

    response = pb.FeedResponse()
    response.type = 1
    response.currentTs = 1_760_000_000_000
    market = response.feeds[PROTO_KEY].fullFeed.marketFF
    market.ltpc.ltp = 112.5
    market.ltpc.ltt = 1_760_000_000_000
    market.ltpc.ltq = 75
    market.ltpc.cp = 100.0
    market.atp = 108.0
    market.vtt = 12_345
    market.oi = 1_245_000
    market.tbq = 900
    market.tsq = 700
    market.iv = 14.2
    market.iep = 111.0
    market.rp = 112.0
    market.ieq = 5
    market.iiqTotal = 50
    market.iiqM = 20
    market.casEligible = True
    market.optionGreeks.delta = 0.52
    market.optionGreeks.theta = -4.2
    market.optionGreeks.gamma = 0.002
    market.optionGreeks.vega = 8.1
    market.optionGreeks.rho = 0.03
    for bid_p, bid_q, ask_p, ask_q in (
        (112.4, 50, 112.5, 30),
        (112.3, 40, 112.6, 20),
        (112.2, 30, 112.7, 10),
        (112.1, 20, 112.8, 10),
        (112.0, 10, 112.9, 5),
    ):
        quote = market.marketLevel.bidAskQuote.add()
        quote.bidP, quote.bidQ, quote.askP, quote.askQ = bid_p, bid_q, ask_p, ask_q
    for interval in ("I1", "I30", "1d"):
        row = market.marketOHLC.ohlc.add()
        row.interval = interval
        row.open, row.high, row.low, row.close = 100.0, 115.0, 99.0, 112.5
        row.vol = 1000
        row.ts = 1_760_000_000_000
    return response.SerializeToString()


def test_full_proto_decoder_captures_depth_flow_greeks_and_native_ohlc() -> None:
    frame = decode_frame(_proto_frame())
    assert frame.type == 1
    assert frame.current_ts == 1_760_000_000_000
    key, tick = frame.ticks[0]
    assert key == PROTO_KEY
    assert tick["ltp"] == 112.5 and tick["ltq"] == 75 and tick["cp"] == 100.0
    assert tick["atp"] == 108.0 and tick["vtt"] == 12_345
    assert tick["oi"] == 1_245_000
    assert tick["tbq"] == 900 and tick["tsq"] == 700
    assert tick["tbq_tsq"] == pytest.approx(900 / 700, abs=0.001)
    assert tick["levels"] == 5
    assert tick["bid_qty_5"] == 150 and tick["ask_qty_5"] == 75
    assert tick["bid_qty_top"] == 50 and tick["ask_qty_top"] == 30
    assert tick["obi_5"] == pytest.approx(2.0)
    assert tick["bids"][0] == {"price": 112.4, "qty": 50.0}
    assert tick["delta"] == 0.52 and tick["theta"] == -4.2
    assert tick["gamma"] == 0.002 and tick["vega"] == 8.1 and tick["rho"] == 0.03
    assert tick["iv"] == 14.2
    assert tick["market_ohlc"]["1m"]["high"] == 115.0
    assert tick["market_ohlc"]["30m"]["close"] == 112.5
    assert tick["market_ohlc"]["1d"]["volume"] == 1000.0
    assert tick["feed_kind"] == "market_full"


def test_json_legacy_efeed_details_path_supports_full_fields() -> None:
    payload = {"feeds": {"NSE_EQ|TEST": {"ff": {"marketFF": {
        "ltpc": {"ltp": 101.0, "ltt": 1_760_000_000_000, "ltq": 5, "cp": 99.0},
        "eFeedDetails": {"atp": 100.0, "vtt": 1000, "oi": 2000, "tbq": 30, "tsq": 20},
        "marketLevel": {"bidAskQuote": [
            {"bidP": 100.9, "bidQ": 10, "askP": 101.1, "askQ": 20},
        ]},
        "optionGreeks": {"delta": 0.4, "theta": -1.5, "gamma": 0.001,
                          "vega": 4.0, "rho": 0.02, "iv": 12.5},
    }}}}}
    key, tick = decode_frame(json.dumps(payload)).ticks[0]
    assert key == "NSE_EQ|TEST"
    assert tick["tbq"] == 30 and tick["tsq"] == 20
    assert tick["theta"] == -1.5 and tick["iv"] == 12.5
    assert tick["obi_5"] == 0.5


def test_spectrum_book_tracks_iv_and_gamma_velocity() -> None:
    book = FullSpectrumBook()
    book.update("K", {"ts_ms": 1_000_000, "iv": 10.0, "gamma": 0.001})
    book.update("K", {"ts_ms": 1_300_000, "iv": 11.0, "gamma": 0.002})
    assert book.iv_velocity("K", 300) == pytest.approx(10.0)
    assert book.gamma_velocity("K", 300) == pytest.approx(100.0)


def test_spectrum_book_dedupes_repeated_ltt_vtt_frames() -> None:
    book = FullSpectrumBook()
    first = {"ts_ms": 1_000_000, "ltt": 1_000_000, "vtt": 500,
             "iv": 10.0, "gamma": 0.001}
    book.update("K", first)
    book.update("K", {**first, "iv": 99.0, "gamma": 0.9})
    assert len(book.iv_history["K"]) == 1
    assert len(book.gamma_history["K"]) == 1
    assert book.ticks["K"]["iv"] == 10.0
    book.update("K", {**first, "vtt": 501, "iv": 11.0, "gamma": 0.002})
    assert len(book.iv_history["K"]) == 2
    assert len(book.gamma_history["K"]) == 2


def test_window_and_front_futures_resolution_are_deterministic() -> None:
    rows = [
        {"strike_price": strike,
         "call_options": {"instrument_key": f"CE{strike}"},
         "put_options": {"instrument_key": f"PE{strike}"}}
        for strike in range(24_700, 25_301, 50)
    ]
    window = select_option_window(rows, 25_000, 5)
    assert len(window) == 11
    assert len(option_window_keys(window)) == 22
    instruments = [
        {"name": "NIFTY", "instrument_type": "FUT", "expiry": "2026-12-31",
         "instrument_key": "NSE_FO|FAR"},
        {"name": "NIFTY", "instrument_type": "FUT", "expiry": "2026-11-26",
         "instrument_key": "NSE_FO|NEAR"},
        {"name": "BANKNIFTY", "instrument_type": "FUT", "expiry": "2026-11-26",
         "instrument_key": "NSE_FO|BANK"},
    ]
    assert select_front_month_futures(instruments, "NIFTY")["instrument_key"] == "NSE_FO|NEAR"


def test_six_feature_kronos_tensor_fuses_futures_flow() -> None:
    spot = [
        {"t": 1_760_000_000 + i * 60, "o": 100, "h": 102, "l": 99, "c": 101, "v": 0}
        for i in range(3)
    ]
    futures = [
        {"time": 1_760_000_000 + i * 60, "open": 100, "high": 102, "low": 99,
         "close": 101, "volume": 120 + i, "amount": 12_000 + i * 100}
        for i in range(3)
    ]
    frame = build_ohlcva(spot, futures)
    assert len(frame) == 3
    assert tuple(frame[0].keys())[1:7] == FEATURES
    assert frame[0]["volume"] == 120
    assert frame[0]["amount"] == 12_000
    assert to_numpy(frame).shape == (3, 6)
    blended = build_spot_model_input(spot, futures, limit=512)
    assert blended[-1]["volume"] == 122


def test_pcr_max_pain_iv_crush_gate_and_full_coverage() -> None:
    chain = []
    for strike in range(24_950, 25_051, 10):
        chain.append({
            "strike_price": strike,
            "call_options": {"market_data": {"oi": 1000, "oi_change": 20, "ltp": 120.0}},
            "put_options": {"market_data": {"oi": 1400, "oi_change": -10, "ltp": 110.0}},
        })
    metrics = chain_metrics(chain)
    assert metrics["pcr"] == pytest.approx(1.4)
    assert metrics["doi_pcr"] == pytest.approx(-0.5)
    assert metrics["max_pain"] > 0

    blocked = greeks_gate(delta=0.5, theta=-500, gamma=0.002, iv=14,
                          iv_velocity_5m=-4.0, kronos_move=2.0)
    assert blocked["blocked"] is True
    assert blocked["allow"] is False

    boosted = greeks_gate(delta=0.5, theta=-4, gamma=0.002, vega=8.0, iv=14,
                          iv_velocity_5m=1.2, gamma_velocity_5m=0.2,
                          obi_5=1.5, tbq_tsq=1.4, kronos_move=5.0)
    assert boosted["conviction"] == "A+"

    snap = build_snapshot(
        symbol="NIFTY", spot=25_000,
        futures={"ltp": 25_020, "atp": 24_990, "volume": 10, "amount": 200, "oi": 1000},
        depth={"obi_5": 1.4, "bid_qty_5": 100, "ask_qty_5": 70,
               "tbq": 110, "tsq": 80, "tbq_tsq": 1.37,
               "bids": [{"price": 24999.5, "qty": 60},
                        {"price": 24999.0, "qty": 40},
                        {"price": 24998.5, "qty": 30},
                        {"price": 24998.0, "qty": 20},
                        {"price": 24997.5, "qty": 10}],
               "asks": [{"price": 25000.5, "qty": 30},
                        {"price": 25001.0, "qty": 40},
                        {"price": 25001.5, "qty": 20},
                        {"price": 25002.0, "qty": 15},
                        {"price": 25002.5, "qty": 10}]},
        chain=chain,
        greeks={"delta": 0.52, "gamma": 0.002, "theta": -4.2,
                "vega": 8.1, "rho": 0.03, "iv": 14.2},
        vix={"ltp": 13.4, "change_pct": 1.2, "direction": "RISING"},
        breadth={"aligned": 4, "total": 5, "rows": []},
        market_ohlc={
            "1m": {"open": 24990, "high": 25010, "low": 24980, "close": 25000},
            "30m": {"open": 24950, "high": 25020, "low": 24940, "close": 25000},
            "1d": {"open": 24800, "high": 25100, "low": 24750, "close": 25000},
        },
        previous_basis=10, iv_velocity_5m=0.4, gamma_velocity_5m=0.1,
        kronos_move=15, side="CE", as_of="2026-11-10T10:00:00+05:30",
        generated_epoch=1_760_000_000, age_ms=0, stale=False,
    )
    assert snap["coverage"]["pct"] == 100.0
    assert snap["coverage"]["valid"] == snap["coverage"]["total"]
    assert snap["coverage"]["status"] == "OK"
    assert snap["pcr"]["strike_count"] == 11
    assert snap["chain_degraded"] is False
    assert snap["greeks"]["iv_velocity_5m"] == 0.4
    assert snap["alpha"]["allow_entry"] is True
    assert snap["alpha"]["structure_ok"] is True


def test_engine_retains_native_ohlc_depth_and_amount() -> None:
    engine = UpstoxTickEngine()
    engine.on_frame({"feeds": {PROTO_KEY: {"ff": {"marketFF": {
        "ltpc": {"ltp": 101.0, "ltt": 1_760_000_000_000},
        "eFeedDetails": {"atp": 100.0, "vtt": 500, "oi": 1000},
        "marketLevel": {"bidAskQuote": [
            {"bidP": 100.9, "bidQ": 10, "askP": 101.1, "askQ": 5},
            {"bidP": 100.8, "bidQ": 20, "askP": 101.2, "askQ": 10},
        ]},
        "marketOHLC": {"ohlc": [
            {"interval": "I1", "open": 100, "high": 102, "low": 99,
             "close": 101, "vol": 500, "ts": 1_760_000_000_000},
        ]},
    }}}}})
    quote = engine.instruments[PROTO_KEY].quote
    assert len(quote.bids) == 2 and len(quote.asks) == 2
    assert quote.bid_qty == 30 and quote.ask_qty == 15
    assert quote.bid_qty_5lvl == 30 and quote.ask_qty_5lvl == 15
    assert quote.bid_qty_top == 10 and quote.ask_qty_top == 5
    assert quote.tbq == 0 and quote.theta == 0
    assert engine.instruments[PROTO_KEY].market_ohlc["1m"]["close"] == 101


def test_live_ticks_snapshot_passes_full_spectrum(tmp_path, monkeypatch) -> None:
    feed = {
        "updated_epoch": time.time(), "socket_connected": True, "prices": {},
        "spot": {"NIFTY": {"ltp": 25_000, "candles_1m": []}},
        "full_spectrum": {"NIFTY": {"coverage": {"pct": 100.0}, "futures": {"ltp": 25_010}}},
        "option": {}, "trade": {}, "scanner": {}, "hud": {},
        "updated_at": "2026-11-10T10:00:00+05:30",
    }
    path = tmp_path / "live_feed.json"
    path.write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FEED_FILE", path)
    snap = live_ticks.snapshot("NIFTY")
    assert snap["full_spectrum"]["NIFTY"]["coverage"]["pct"] == 100.0
    assert snap["full_spectrum"]["NIFTY"]["stale"] is False
    assert snap["full_spectrum"]["NIFTY"]["age_ms"] >= 0
    assert snap["entries_suspended"] is False


def test_stale_full_spectrum_is_marked_and_suspends_entries(tmp_path, monkeypatch) -> None:
    now = time.time()
    feed = {
        # The LKG window is deliberately 12 s; 20 s is a genuinely stale chain.
        "updated_epoch": now - 20.0,
        "updated_at": "2026-11-10T10:00:00+05:30",
        "socket_connected": True,
        "full_spectrum": {"NIFTY": {"coverage": {"pct": 80.0}}},
    }
    path = tmp_path / "live_feed.json"
    path.write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FEED_FILE", path)
    # The real 4 Hz fast frame is unrelated to this stale heavy-chain probe;
    # isolate it so a previous local session cannot turn the gate green.
    monkeypatch.setattr(live_ticks, "FAST_FILE", tmp_path / "live_fast.json")
    snap = live_ticks.snapshot("NIFTY", now=now)
    assert snap["full_spectrum"]["NIFTY"]["stale"] is True
    assert snap["full_spectrum"]["NIFTY"]["age_ms"] >= 10_000
    assert snap["full_spectrum"]["NIFTY"]["as_of"] == "2026-11-10T10:00:00+05:30"
    assert snap["entries_suspended"] is True


def test_alpha_fails_closed_on_empty_or_zero_market_structure() -> None:
    decision = evaluate(chain=[], spot=0, futures_ltp=0, depth={})
    assert decision["allow_entry"] is False
    assert decision["conviction"] == "BLOCKED"
    assert decision["structure_ok"] is False
    assert decision["reason"] == "STALE_FEED"
    assert decision["block_reason"] == "STALE_FEED"
    assert "STALE_FEED" in decision["reasons"]
    assert set(decision["missing_structures"]) == {"greeks", "option_chain", "depth"}

    snap = build_snapshot(
        symbol="NIFTY", spot=0, futures={}, depth={}, chain=[], greeks={},
        vix={}, breadth={}, market_ohlc={},
    )
    assert snap["alpha"]["allow_entry"] is False
    assert snap["alpha"]["conviction"] == "BLOCKED"
    assert snap["coverage"]["pct"] < 100.0
    assert snap["coverage"]["status"] == "DEGRADED"


def test_coverage_rejects_zero_valued_fields() -> None:
    snapshot = {
        "futures": {"ltp": 0, "atp": 0, "volume": 0, "amount": 0,
                    "oi": 0, "basis": 0},
        "depth": {"obi_5": 0, "bid_qty_5": 0, "ask_qty_5": 0,
                  "bid_qty_top": 0, "ask_qty_top": 0,
                  "tbq": 0, "tsq": 0, "bids": [], "asks": []},
        "pcr": {"pcr": 0, "doi_pcr": 0, "max_pain": 0, "ce_oi": 0, "pe_oi": 0,
                "strike_count": 0, "valid_strikes": 0},
        "greeks": {"delta": 0, "gamma": 0, "theta": 0, "vega": 0,
                   "rho": 0, "iv": 0, "iv_velocity_5m": 0},
        "vix": {"ltp": 0},
        "breadth": {"aligned": 0, "total": 0},
        "market_ohlc": {"1m": {}, "30m": {}, "1d": {}},
    }
    coverage = field_coverage(snapshot)
    assert coverage["pct"] < 50.0
    assert coverage["degraded"] is True
    assert coverage["status"] == "DEGRADED"
    assert "depth.bids" in coverage["missing"]
    assert "greeks.iv" in coverage["missing"]


def test_chain_window_at_the_edge_marks_chain_degraded() -> None:
    chain = [
        {"strike_price": 25_000 + index * 50,
         "call_options": {"market_data": {"oi": 1000}},
         "put_options": {"market_data": {"oi": 1000}}}
        for index in range(6)
    ]
    snap = build_snapshot(
        symbol="NIFTY", spot=25_000,
        futures={"ltp": 25_020, "atp": 25_010, "volume": 10, "amount": 200, "oi": 1000},
        depth={"obi_5": 1.2, "bid_qty_5": 100, "ask_qty_5": 80,
               "tbq": 50, "tsq": 40, "tbq_tsq": 1.25,
               "bids": [{"price": 24_999.5, "qty": 100}],
               "asks": [{"price": 25_000.5, "qty": 80}]},
        chain=chain,
        greeks={"delta": 0.5, "gamma": 0.002, "theta": -4.0,
                "vega": 8.0, "rho": 0.03, "iv": 14.0},
        vix={"ltp": 13.0}, breadth={"aligned": 3, "total": 5},
        market_ohlc={"1m": {"open": 1, "high": 2, "low": 1, "close": 2}},
    )
    assert snap["pcr"]["strike_count"] == 6
    assert snap["chain_degraded"] is True
    assert snap["alpha"]["chain_degraded"] is True
