"""Broker-grade gate, SMC, tri-model audit and post-T2 runner tests."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from dashboard import server as dashboard_server
from execution.pro_money_manager import (
    evaluate_post_t2_continuation,
    manage_post_t2_super_trend,
)
from market.data_sanitizer import SanitizedTickPipeline, feed_state, validate_tensor
from model.tri_model_orchestrator import (
    MODEL_SPECS,
    build_audit_snapshot,
    run_model_brain,
)
from state.paper_trade_ledger import paper_trade_from_live
from strategy.smc_engine import (
    FairValueGap,
    OrderBlock,
    SmcStateMachine,
    Structure,
    analyze_structure,
    build_wait_zone,
    detect_fvgs,
    detect_liquidity_sweeps,
    detect_order_blocks,
    wait_state,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _tick(ltt: int = 1_760_000_000_000, price: float = 25_000.0,
          vtt: float = 100.0, bid: float = 0.0, ask: float = 0.0) -> dict:
    return {"ltp": price, "price": price, "ltt": ltt, "vtt": vtt,
            "bid": bid, "ask": ask, "atp": price - 1.0}


def test_sanitizer_accepts_good_tick_and_rejects_corrupt_frames() -> None:
    pipeline = SanitizedTickPipeline()
    # Warm the pure-Python hot path first. The production contract is about
    # sustained sanitizer latency, not the one-off interpreter cold call.
    warm: list[float] = []
    for index in range(5):
        result = pipeline.sanitize_tick(
            "NSE_INDEX|Nifty 50",
            _tick(ltt=1_759_999_999_900 + index),
        )
        assert result.ok is True
        warm.append(result.latency_us)
    good = pipeline.sanitize_tick("NSE_INDEX|Nifty 50", _tick())
    assert good.ok is True
    assert statistics.median([*warm, good.latency_us]) < 500.0
    assert pipeline.sanitize_tick(
        "NSE_INDEX|Nifty 50", _tick(ltt=1_760_000_000_001, price=0)
    ).reason == "SPOT_LTP_INVALID"
    assert pipeline.sanitize_tick(
        "NSE_INDEX|Nifty 50", _tick(ltt=1_760_000_000_002, price=float("nan"))
    ).ok is False
    inverted = pipeline.sanitize_tick(
        "NSE_FO|NIFTY|25000|CE",
        _tick(ltt=1_760_000_000_003, price=120.0, bid=121.0, ask=120.0),
    )
    assert inverted.reason == "INVERTED_SPREAD"
    assert pipeline.sanitize_tick(
        "NSE_FO|NIFTY|25000|CE", _tick(ltt=1_760_000_000_004, price=0.01, bid=0.0, ask=0.0)
    ).reason == "OPTION_LTP_TOO_LOW"


def test_sanitizer_drops_duplicate_and_out_of_order_ticks() -> None:
    pipeline = SanitizedTickPipeline()
    first = _tick(ltt=1_760_000_000_000, vtt=100)
    assert pipeline.sanitize_tick("K", first).ok is True
    assert pipeline.sanitize_tick("K", first).reason == "DUPLICATE_FRAME"
    assert pipeline.sanitize_tick("K", _tick(ltt=1_759_999_999_999)).reason == "OUT_OF_ORDER_LTT"
    snap = pipeline.snapshot()
    assert snap["frames_received"] == 3
    assert snap["frames_sanitized_ok"] == 1
    assert snap["duplicates_dropped"] == 1
    assert snap["corrupt_frames_rejected"] >= 1
    assert snap["within_500us_budget"] is True


def test_sanitizer_telemetry_latency_is_below_half_millisecond() -> None:
    pipeline = SanitizedTickPipeline()
    for index in range(500):
        result = pipeline.sanitize_tick("K", _tick(ltt=1_760_000_000_000 + index, vtt=1000 + index))
        assert result.ok is True
    snap = pipeline.snapshot()
    assert snap["sanitize_latency_p95_us"] < 500.0
    assert snap["sanitize_latency_avg_us"] < 500.0
    assert snap["within_500us_budget"] is True


def test_tensor_requires_512_positive_flow_bars() -> None:
    rows = []
    for index in range(512):
        rows.append({
            "time": 1_760_000_000 + index * 300,
            "open": 100.0 + index * 0.01,
            "high": 101.0 + index * 0.01,
            "low": 99.0 + index * 0.01,
            "close": 100.5 + index * 0.01,
            "volume": 1000 + index,
            "amount": 250_000 + index,
        })
    verdict = validate_tensor(rows)
    assert verdict.ok is True
    assert verdict.shape == (1, 512, 6)
    rows[300]["volume"] = 0
    assert validate_tensor(rows).reason == "BAR_300_ZERO_FLOW"
    assert validate_tensor(rows[:511]).ok is False


def test_feed_gate_is_strictly_offline_after_three_seconds() -> None:
    now = 1_760_000_000.0
    fresh = feed_state({"updated_epoch": now - 2.9, "market_open": True}, now=now)
    stale = feed_state({"updated_epoch": now - 3.1, "market_open": True}, now=now)
    assert fresh.status == "FEED_LIVE" and fresh.allow_order_generation is True
    assert stale.status == "FEED_OFFLINE_OR_STALE"
    assert stale.allow_order_generation is False
    assert "3000" in stale.reason


def _candles(highs: list[float], lows: list[float], closes: list[float]) -> list[dict]:
    return [
        {"time": index + 1, "open": lows[index], "high": highs[index],
         "low": lows[index], "close": closes[index], "volume": 1000 + index}
        for index in range(len(closes))
    ]


def test_fvg_order_block_and_liquidity_sweep_detectors() -> None:
    gap_rows = _candles(
        [101, 106, 108], [99, 99, 102], [100, 105, 107]
    )
    gaps = detect_fvgs(gap_rows)
    assert gaps and gaps[0].direction == 1
    assert (gaps[0].low, gaps[0].high) == (101.0, 102.0)

    blocks_rows = [
        {"time": 1, "open": 105, "high": 106, "low": 100, "close": 101, "volume": 1000},
        {"time": 2, "open": 101, "high": 116, "low": 101, "close": 115, "volume": 1000},
        {"time": 3, "open": 115, "high": 117, "low": 110, "close": 114, "volume": 1000},
        {"time": 4, "open": 114, "high": 118, "low": 111, "close": 115, "volume": 1000},
        {"time": 5, "open": 115, "high": 119, "low": 112, "close": 116, "volume": 1000},
    ]
    futures = [
        {"time": 1, "vtt": 100},
        {"time": 2, "vtt": 1000},
        {"time": 3, "vtt": 1100},
        {"time": 4, "vtt": 1200},
        {"time": 5, "vtt": 1300},
    ]
    blocks = detect_order_blocks(blocks_rows, futures)
    assert blocks
    assert blocks[0].direction == 1
    assert blocks[0].low == 100.0 and blocks[0].high == 106.0
    assert blocks[0].volume_multiple >= 2.0

    sweep_rows = _candles(
        [102, 103, 102, 101, 100, 101, 102, 103, 102, 101],
        [101, 100, 99, 98.2, 98, 98.4, 99, 100, 100, 97.5],
        [101, 101, 100, 99, 99, 100, 101, 102, 101, 99.0],
    )
    sweeps = detect_liquidity_sweeps(sweep_rows, obi_5=1.5)
    assert sweeps and sweeps[0].direction == 1
    assert sweeps[0].swept_level == 98.0


def test_structure_and_patient_wait_zone() -> None:
    rows = _candles(
        [100, 101, 102, 101, 100, 99, 100, 101, 105],
        [98, 99, 100, 99, 98, 97, 98, 99, 103],
        [99, 100, 101, 100, 99, 98, 99, 100, 104],
    )
    structure = analyze_structure(rows)
    assert structure.bos is True
    zone = build_wait_zone(
        spot=100.0, p10=95.0, p25=97.0,
        order_blocks=[OrderBlock(1, 96.0, 98.0, 4, 2.4)],
        fvgs=[FairValueGap(1, 95.5, 96.5, 5)],
        option_atp=52.0,
    )
    assert zone is not None
    assert zone.spot_low == 96.0 and zone.spot_high == 96.5
    state, reason = wait_state(zone, spot=100.0, obi_5=1.5, structure=structure)
    assert state == "WAITING_FOR_PRICE_ZONE"
    assert "above" in reason
    state, _ = wait_state(zone, spot=96.2, obi_5=1.0, structure=structure)
    assert state == "WAITING_FOR_IGNITION"
    state, _ = wait_state(zone, spot=96.2, obi_5=1.5, structure=structure)
    assert state == "TRIGGER_READY"
    assert "Target Spot" in zone.as_dict()["label"]


def test_smc_machine_evaluates_1m_5m_15m_and_vetoes_bearish_macro() -> None:
    base = _candles(
        [100, 101, 102, 101, 100, 99, 100, 101, 105],
        [98, 99, 100, 99, 98, 97, 98, 99, 103],
        [99, 100, 101, 100, 99, 98, 99, 100, 104],
    )
    macro = _candles(
        [104, 103, 102, 103, 104, 105, 104, 103, 100],
        [100, 99, 98, 99, 100, 101, 100, 99, 96],
        [103, 102, 101, 102, 103, 104, 103, 102, 97],
    )
    ready = SmcStateMachine().update(
        candles_1m=base, candles_5m=base, candles_15m=[],
        p10=95, p25=97, spot=98.5, obi_5=1.5, feed_live=True,
    )
    assert set(ready.structures) == {"1m", "5m"}
    assert ready.state == "TRIGGER_READY"

    vetoed = SmcStateMachine().update(
        candles_1m=base, candles_5m=base, candles_15m=macro,
        p10=95, p25=97, spot=98.5, obi_5=1.5, feed_live=True,
    )
    assert set(vetoed.structures) == {"1m", "5m", "15m"}
    assert vetoed.structures["15m"].active_bos == "BEARISH_BOS"
    assert vetoed.structures["5m"].active_bos == "BULLISH_BOS"
    assert vetoed.state == "WAITING_FOR_STRUCTURE"
    assert "15m" in vetoed.reason


def test_ledger_maps_super_trend_legs_and_runner_exit() -> None:
    trade = {
        "status": "CLOSED", "entry_price": 100.0, "initial_qty": 100,
        "qty_open": 0, "realized_pnl_rs": 3230.0, "exit_reason": "RUNNER_EXHAUSTION",
        "booked_legs": [
            {"leg": "SUPER_T1_40", "qty": 40, "price": 120.0, "pnl": 800.0},
            {"leg": "SUPER_T2_30", "qty": 30, "price": 140.0, "pnl": 1200.0},
            {"leg": "SUPER_RUNNER_EXIT", "qty": 30, "price": 141.0, "pnl": 1230.0},
        ],
    }
    row = paper_trade_from_live(trade, date="2026-11-10", symbol="NIFTY")
    assert row.t1_qty == 40 and row.t2_qty == 30
    assert row.qty_open == 0
    assert len(row.booked_legs) == 3
    assert row.net == 3230.0


def test_branch_a_exits_and_branch_b_keeps_runner() -> None:
    def trade() -> dict:
        return {
            "status": "OPEN", "entry_price": 100.0, "initial_qty": 100,
            "qty": 100, "qty_open": 100, "target_1": 120.0,
            "target_2_p90": 140.0, "current_trailing_sl": 90.0,
            "booked_legs": [], "realized_pnl_rs": 0.0,
        }

    branch_b = trade()
    t1 = manage_post_t2_super_trend(branch_b, 120.0, smc_bull_bos=True)
    assert t1["legs"][0]["qty"] == 40
    assert branch_b["qty_open"] == 60
    assert branch_b["current_trailing_sl"] == 101.5
    t2 = manage_post_t2_super_trend(
        branch_b, 140.0, smc_bull_bos=True, smc_bear_choch=False,
        obi_5=1.60, tbq=160.0, tsq=100.0, futures_volume_ratio=1.4,
        ema9=135.0, atp=130.0, oi_change_5m_pct=-4.0,
        atr_1m=5.0, latest_1m_low=138.0,
    )
    assert t2["state"] == "POST_T2_SUPER_TREND_RUNNER"
    assert t2["legs"][0]["qty"] == 30
    assert branch_b["qty_open"] == 30
    assert branch_b["current_trailing_sl"] == 136.5
    runner = manage_post_t2_super_trend(
        branch_b, 141.0, smc_bull_bos=True, obi_5=0.50,
        atr_1m=5.0, latest_1m_low=140.0,
    )
    assert runner["state"] == "CLOSED_RUNNER"
    assert branch_b["status"] == "CLOSED"
    assert branch_b["realized_pnl_rs"] == 3230.0

    branch_a = trade()
    manage_post_t2_super_trend(branch_a, 120.0, smc_bull_bos=True)
    result = manage_post_t2_super_trend(
        branch_a, 140.0, smc_bull_bos=True, obi_5=1.0, tbq=100.0, tsq=100.0,
        futures_volume_ratio=0.5, ema9=145.0, atp=145.0,
        oi_change_5m_pct=1.0, atr_1m=5.0, latest_1m_low=138.0,
    )
    assert result["state"] == "CLOSED_T2"
    assert branch_a["qty_open"] == 0
    assert branch_a["exit_reason"] == "T2_FULL_EXIT"
    assert result["continuation"]["passed"] < 3


def test_continuation_evaluation_is_sub_millisecond() -> None:
    result = evaluate_post_t2_continuation(
        smc_bull_bos=True, obi_5=1.6, tbq=160.0, tsq=100.0,
        futures_volume_ratio=1.5, option_ltp=140.0, ema9=135.0,
        atp=130.0, oi_change_5m_pct=-4.0,
    )
    assert result["branch"] == "BRANCH_B_GEAR5_SUPER_TREND_RUNNER"
    assert result["passed"] == 4
    assert result["latency_us"] < 1000.0


def _bar(time_value: int, close: float = 25_000.0) -> dict:
    return {"time": time_value, "open": close - 4, "high": close + 6,
            "low": close - 6, "close": close, "volume": 1000, "amount": 25_000_000}


def _complete_feed(now: float) -> dict:
    bars = [_bar(1_760_000_000 + index * 300, 25_000 + index * 0.2) for index in range(512)]
    chain = []
    for index in range(11):
        strike = 24_750 + index * 50
        chain.append({
            "strike_price": strike,
            "call_options": {"market_data": {"oi": 1000 + index, "oi_change": 20},
                             "option_greeks": {"delta": 0.5, "gamma": 0.002,
                                               "theta": -4.0, "vega": 8.0,
                                               "rho": 0.02, "iv": 14.0}},
            "put_options": {"market_data": {"oi": 1400 + index, "oi_change": -10},
                            "option_greeks": {"delta": -0.5, "gamma": 0.002,
                                              "theta": -4.0, "vega": 8.0,
                                              "rho": 0.02, "iv": 14.0}},
        })
    depth = {
        "bids": [{"price": 24_999.0 - index, "qty": 10 + index} for index in range(5)],
        "asks": [{"price": 25_001.0 + index, "qty": 10 + index} for index in range(5)],
        "obi_5": 1.5, "tbq": 160.0, "tsq": 100.0, "tbq_tsq": 1.6,
    }
    spot = {"ltp": 25_000.0, "candles_1m": bars[-30:], "candles_5m": bars[-30:],
            "candles_15m": bars[-30:]}
    return {
        "updated_at": datetime.fromtimestamp(now, tz=IST).isoformat(timespec="seconds"),
        "updated_epoch": now, "market_open": True, "socket_connected": True,
        "prices": {"NSE_INDEX|Nifty 50": 25_000.0},
        "spot": {"NIFTY": spot},
        "buffer_1m": {"NIFTY": 512}, "buffer_5m": {"NIFTY": 512},
        "buffer_15m": {"NIFTY": 512},
        "kronos_inputs": {"NIFTY": {
            "features": ["open", "high", "low", "close", "volume", "amount"],
            "shape_1m": [512, 6], "shape_5m": [512, 6],
            "last_5m": [bars[-1]], "volume_source": "FUT",
        }},
        "full_spectrum": {"NIFTY": {
            "depth": depth, "futures": {"basis": 18.0, "ltp": 25_018.0},
            "greeks": {"delta": 0.5, "gamma": 0.002, "theta": -4.0,
                        "vega": 8.0, "rho": 0.02, "iv": 14.0},
            "vix": {"ltp": 13.4}, "breadth": {"aligned": 4, "total": 5},
        }},
        "option_chain": {"NIFTY": chain},
        "smc": {"NIFTY": {
            "structure": {"bias": 1, "bos": True, "active_bos": "BULLISH_BOS",
                          "choch": False, "active_choch": "",
                          "swing_high": 25_020.0, "swing_low": 24_980.0},
            "wait_zone": {"spot_low": 24_950.0, "spot_high": 24_970.0},
            "state": "WAITING_FOR_PRICE_ZONE",
            "reason": "spot above the confluence",
        }},
        "sanitizer": {"frames_received": 100, "frames_sanitized_ok": 99,
                      "corrupt_frames_rejected": 1, "duplicates_dropped": 0,
                      "sanitize_latency_us": 120.0, "within_500us_budget": True,
                      "events": []},
    }


def test_tri_model_cards_explain_inputs_and_do_not_invent_probabilities() -> None:
    feed = _complete_feed(time.time())
    payload = build_audit_snapshot(feed)
    assert payload["verified_count"] == 8
    assert payload["total_checks"] == 8
    assert len(payload["models"]) == 3
    assert {card["role"] for card in payload["models"]} == {"base", "small", "mini"}
    assert all(card["shape"] == [1, 512, 6] for card in payload["models"])
    assert all(card["probability_pct"] is None for card in payload["models"])
    assert all(card["state"] == "WAITING_FOR_MODEL_INFERENCE" for card in payload["models"])
    assert payload["decision"]["loaded_models"] == 0
    assert payload["decision"]["total_models"] == 3
    assert "ZERO-FAKE" in payload["decision"]["state"]
    assert len(MODEL_SPECS) == 3


def test_injected_real_predictors_produce_model_cards() -> None:
    feed = _complete_feed(time.time())

    def predictor(request):
        assert request["spec"].role in {"base", "small", "mini"}
        return {"direction_probability_pct": 84.0, "direction": 1,
                "state": "WAITING_FOR_PRICE_ZONE", "reason": "injected test inference",
                "inference_ms": 4.0}

    payload = run_model_brain(feed, predictors={"base": predictor, "small": predictor,
                                                 "mini": predictor})
    assert payload["decision"]["loaded_models"] == 3
    assert all(card["probability_pct"] == 84.0 for card in payload["models"])
    assert all(card["inference_ms"] is not None for card in payload["models"])


def test_audit_without_feed_is_missing_not_green() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    market_hours = datetime(2026, 11, 10, 10, 30, tzinfo=ist).timestamp()
    payload = build_audit_snapshot({}, now=market_hours)
    assert payload["ok"] is False
    assert payload["feed_status"]["status"] == "FEED_OFFLINE_OR_STALE"
    assert len(payload["matrix"]) == 8
    assert all(row["verified"] is False for row in payload["matrix"])
    assert len(payload["models"]) == 3
    assert all(card["probability_pct"] is None for card in payload["models"])


def test_model_audit_http_endpoint_returns_eight_checks_and_three_models(
    tmp_path: Path, monkeypatch,
) -> None:
    feed = _complete_feed(time.time())
    state = tmp_path / "state"
    state.mkdir()
    (state / "live_feed.json").write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(dashboard_server, "REPO_ROOT", tmp_path)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/model-audit"
        with urlopen(url, timeout=10) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
    assert len(payload["matrix"]) == 8
    assert len(payload["models"]) == 3
    assert payload["feed_status"]["status"] == "FEED_LIVE"
    assert payload["verified_count"] == 8
