import re
import time
from datetime import datetime
from pathlib import Path

from market.programmatic_telemetry import ProgrammaticTelemetry
from state.telugu_narration_logger import IST, ProgrammaticBrainLogger
from strategy.model_oscillator import build_model_oscillator, build_oscillator_from_feed


def _feed(now):
    return {
        "updated_epoch": now, "tick_rate": 16.4,
        "spot": {"NIFTY": {"ltp": 24948.5, "candles_1m": [[0]] * 512,
                           "candles_5m": [[0]] * 512, "candles_15m": [[0]] * 512}},
        "full_spectrum": {"NIFTY": {
            "spot": 24948.5,
            "futures": {"volume": 12845000, "amount": 32114500000, "basis": 34.8},
            "depth": {"obi_5": 1.72, "tbq": 912400, "tsq": 704600,
                      "tbq_tsq": 1.295, "bids": [{}] * 5, "asks": [{}] * 5},
            "pcr": {"pcr": 1.1281, "doi_pcr": .0545, "ce_oi": 17600,
                    "pe_oi": 19855, "strike_count": 11, "valid_strikes": 11},
            "greeks": {"delta": .54, "gamma": .00184, "theta": -3.62,
                       "vega": 11.24, "iv": 13.82},
            "vix": {"ltp": 12.84, "change_pct": 1.18},
            "breadth": {"aligned": 4, "total": 5},
        }},
        "option": {"NIFTY": {"strike": 24950, "side": "CE", "ltp": 78.4, "atp_vwap": 72.4}},
        "scanner": {"NIFTY": [{"key": "kronos_base_paths", "value": "82 / 100"}]},
    }


def test_speed_window_and_six_reader_mapping():
    now = time.time()
    telemetry = ProgrammaticTelemetry()
    for i in range(10):
        telemetry.record_tick(
            now_ns=i * 100_000_000, server_epoch_ms=now * 1000 - i * 10,
            ltt_ms=now * 1000 - i * 10 - 11, payload_bytes=1024,
            sanitize_us=280,
        )
        telemetry.record_scan(18.4, now_ns=i * 100_000_000)
    snap = telemetry.snapshot(_feed(now), now=now, now_ns=1_000_000_000)
    speed = snap["speed_telemetry"]
    assert speed["ticks_per_sec"] == 10.0
    assert speed["payload_kbps"] == 10.0
    assert speed["exchange_to_server_latency_ms"] == 11.0
    assert speed["python_sanitize_us"] == 280.0
    assert speed["speed_grade"] == "⚡ ULTRA-FAST (<25ms)"
    assert len(snap["data_pipe_health"]) == 6
    readers = [row["reader"] for row in snap["data_pipe_health"]]
    assert readers == [
        "Kronos-base (102M) & SMC Engine",
        "Kronos-base 6-Feature Tokenizer",
        "Kronos-small (25M) Regime Judge",
        "Kronos-small Greek Gate & Target Mapper",
        "Kronos-mini (4M) 1m Execution Sniper",
        "Parallel Tsunami & Lead-Lag Engine",
    ]
    assert all(row["status"] == "SYNCED_LIVE" for row in snap["data_pipe_health"])


def test_oscillator_sum_borders_and_range():
    live = build_model_oscillator(
        base_upside_pct=90, small_probability_pct=82, mini_probability_pct=84,
        obi_5=1.6, tbq_tsq=1.4, futures_volume_ratio=2.4,
    )
    assert live["bullish_upside_prob_pct"] + live["bearish_downside_prob_pct"] == 100.0
    assert live["bullish_border_reached"] is True
    assert live["bearish_border_reached"] is False
    assert -100 <= live["oscillator_value"] <= 100
    bear = build_model_oscillator(
        base_upside_pct=10, small_probability_pct=15, mini_probability_pct=20,
        obi_5=.6, tbq_tsq=.7, futures_volume_ratio=.4,
    )
    assert bear["bearish_border_reached"] is True
    assert bear["bullish_upside_prob_pct"] + bear["bearish_downside_prob_pct"] == 100.0
    exact = build_model_oscillator(
        base_upside_pct=78, small_probability_pct=78, mini_probability_pct=78,
    )
    assert exact["bullish_border_reached"] is True
    assert exact["bullish_upside_prob_pct"] == 78.0
    feed_osc = build_oscillator_from_feed(_feed(time.time()))
    assert feed_osc["probability_sum_pct"] == 100.0


def test_millisecond_telugu_cards_and_ring_limit():
    fixed = datetime(2026, 11, 10, 9, 22, 4, 318000, tzinfo=IST)
    feed = _feed(fixed.timestamp())
    osc = build_oscillator_from_feed(feed, plan={"paths_up": 82, "samples": 100})
    logger = ProgrammaticBrainLogger(max_entries=80)
    result = logger.record_cycle(
        feed, plan={"paths_up": 82, "samples": 100, "p50": 25020, "p10": 24930},
        oscillator=osc, now=fixed, scan_ms=18.4, sanitize_ms=.28,
        latency_ms=11, futures_volume_ratio=2.4,
    )
    assert [card["role"] for card in result["thoughts"]] == ["base", "small", "mini"]
    assert "512 క్యాండిల్స్" in result["thoughts"][0]["reading_te"]
    assert "11-స్ట్రైక్స్" in result["thoughts"][1]["reading_te"]
    assert "5-లెవల్" in result["thoughts"][2]["reading_te"]
    assert result["log"][0]["ts"].startswith("09:22:04.")
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{3}", result["log"][0]["ts"])
    categories = {row["category"] for row in result["log"]}
    assert {"[DATA SYNC]", "[KRONOS-102M]", "[KRONOS-25M]", "[KRONOS-4M]"}.issubset(categories)
    for i in range(30):
        logger.record_cycle(feed, oscillator=osc, now=fixed)
    assert len(logger.snapshot()) == 80


def test_deck_markup_has_four_zones_and_pause_control():
    html = Path("dashboard/static/index.html").read_text(encoding="utf-8")
    for marker in ("td-zone-speed", "td-zone-osc", "td-zone-models", "td-zone-log",
                   "tdLogPause", "78% BEAR", "78% BULL", "tdPipes"):
        assert marker in html
