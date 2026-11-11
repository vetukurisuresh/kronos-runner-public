from __future__ import annotations

import time

import pandas as pd

from agent.kronos_ensemble import vectorized_path_projection
from market import live_ticks
from market.option_picker import selection_snapshot


def _frame(rows: int = 512) -> pd.DataFrame:
    index = pd.date_range("2026-11-11 09:15", periods=rows, freq="5min")
    close = [25_100 + ((step * 7) % 70) - 35 for step in range(rows)]
    return pd.DataFrame({
        "open": [value - 3 for value in close],
        "high": [value + 8 for value in close],
        "low": [value - 8 for value in close],
        "close": close,
        "volume": [10_000 + step for step in range(rows)],
    }, index=index)


def test_vectorized_100_paths_over_150_candles_is_bounded_and_fast() -> None:
    frame = _frame()
    spot = float(frame["close"].iloc[-1])
    started = time.perf_counter()
    result = vectorized_path_projection(
        frame, spot=spot, p10=spot - 40, p50=spot + 10, p90=spot + 55,
        pred_len=12,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert elapsed_ms < 120.0
    assert result["paths_total"] == 100
    assert len(result["paths"]) == 100
    assert len(result["paths"][0]) == 12
    assert result["paths_bullish"] + result["paths_bearish"] == 100
    assert result["candles_analyzed"] == 150
    assert result["buffer_total"] == 512
    assert result["p10_floor"] <= result["p25_band"] <= result["p50_median"]
    assert result["p50_median"] <= result["p75_band"] <= result["p90_ceiling"]
    assert result["projection_source"] == "VECTORIZED_100WAY_150CANDLE"


def test_ce_pe_five_condition_checklist_and_transition_banner() -> None:
    selected = selection_snapshot(
        symbol="NIFTY", strike=25_200, previous_side="CE",
        upside=18, downside=82, paths_up=16, paths_total=100,
        pcr=0.65, spot=25_174, vwap=25_214, obi_5=0.84, smc_side="PE",
    )
    assert selected["side"] == "PE"
    assert selected["contract"] == "NIFTY 25200 PE"
    assert selected["mode"] == "AUTO"
    assert selected["conditions_passed"] == 5
    assert len(selected["conditions"]) == 5
    assert selected["transition"] is True
    assert "CE ➔ PE" in selected["transition_te"]
    assert "84/100" in selected["transition_te"]

    locked = selection_snapshot(
        symbol="NIFTY", strike=25_200, active_trade={"status": "OPEN", "strike": 25_200,
            "option_type": "CE"}, manual="PE", previous_side="PE",
        upside=10, downside=90, pcr=0.6, spot=25_100, vwap=25_200, obi_5=0.7,
        smc_side="PE",
    )
    assert locked["side"] == "CE"
    assert locked["mode"] == "LOCKED_TO_OPEN_TRADE"
    assert locked["transition"] is False


def test_fast_delta_is_small_named_event(monkeypatch, tmp_path) -> None:
    now = time.time()
    fast = tmp_path / "live_fast.json"
    fast.write_text(live_ticks.json.dumps({
        "epoch": now, "tick_seq": 77, "socket_connected": True,
        "spot": {"NIFTY": {"ltp": 25_200, "change": 12.5, "pct_change": 0.05}},
        "option": {"NIFTY": {"strike_symbol": "NIFTY 25200 PE", "ltp": 140.5, "obi_5": 0.82}},
    }), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FAST_FILE", fast)
    out = live_ticks.fast_delta("NIFTY", now=now + 0.1)
    assert out["fast"] is True
    assert out["tick_seq"] == 77
    assert out["spot"]["ltp"] == 25_200
    assert out["option"]["ltp"] == 140.5
    assert len(live_ticks.json.dumps(out)) < 2_500


def test_fast_delta_refuses_stale_file(monkeypatch, tmp_path) -> None:
    fast = tmp_path / "live_fast.json"
    fast.write_text(live_ticks.json.dumps({"epoch": time.time() - 30,
        "spot": {"NIFTY": {"ltp": 25_200}}}), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FAST_FILE", fast)
    assert live_ticks.fast_delta("NIFTY") == {}


def test_fast_overlay_dispatch_is_sub_millisecond_p95(monkeypatch, tmp_path) -> None:
    now = time.time()
    fast = tmp_path / "live_fast.json"
    fast.write_text(live_ticks.json.dumps({
        "epoch": now, "tick_seq": 91, "socket_connected": True,
        "spot": {"NIFTY": {"ltp": 25_201.5}},
        "option": {"NIFTY": {"strike_symbol": "NIFTY 25200 PE", "ltp": 142.25}},
    }), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FAST_FILE", fast)
    payload = {
        "ts": now, "age_ms": 0, "live": True,
        "data_gate": {"live": True, "allowed": True},
        "spot": {"NIFTY": {"ltp": 25_200}}, "option": {}, "indices": {},
    }
    samples = []
    for _ in range(100):
        started = time.perf_counter()
        live_ticks.fast_overlay(payload, now=time.time())
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    p95 = samples[94]
    assert p95 < 1.0, f"fast_overlay p95 {p95:.3f}ms"