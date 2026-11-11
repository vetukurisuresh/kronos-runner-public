"""Proofs for the autonomous ₹35,000 continuous scalp sleeve."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from dashboard import server as dashboard_server

from execution.scalp_sleeve_ledger import ScalpSleeveLedger
from market import live_ticks
from strategy.continuous_scalp_engine import (
    MAX_SCALP_CAPITAL,
    SCALP_SLEEVE_CAPITAL,
    ContinuousScalpEngine,
    detect_scalp_flip,
    evaluate_continuous_scalp,
    manage_continuous_scalp,
    select_scalp_contract,
)


def _context(**overrides):
    values = dict(
        now=datetime(2026, 11, 10, 10, 30),
        symbol="NIFTY",
        main_state="WAITING_FOR_PRICE_ZONE",
        spot=24_960,
        spot_atr_1m=7.5,
        spot_range_1m=8.0,
        option_atr_1m=2.8,
        spread=0.20,
        spot_vwap=24_950,
        spot_ema5=24_945,
        p10=24_930,
        p90=24_990,
        kronos_slope=0.35,
        side_bias="CE",
        ce_ltp=105.0,
        ce_atp=101.0,
        pe_ltp=88.0,
        pe_atp=90.0,
        ce_obi_5=1.72,
        ce_tbq=180_000,
        ce_tsq=100_000,
        pe_obi_5=1.10,
        pe_tbq=80_000,
        pe_tsq=100_000,
        futures_volume_ratio=2.1,
    )
    values.update(overrides)
    return values


def _chain():
    rows = []
    for strike, ce_ltp, pe_ltp, delta, gamma in (
        (24_900, 132.0, 74.0, 0.63, 0.0018),
        (24_950, 105.0, 98.0, 0.56, 0.0025),
        (25_000, 78.0, 128.0, 0.47, 0.0015),
    ):
        rows.append({
            "strike_price": strike,
            "call_options": {
                "instrument_key": f"NSE_FO|CE{strike}",
                "market_data": {"ltp": ce_ltp, "bid_price": ce_ltp - 0.10,
                                "ask_price": ce_ltp + 0.10},
                "option_greeks": {"delta": delta, "gamma": gamma},
            },
            "put_options": {
                "instrument_key": f"NSE_FO|PE{strike}",
                "market_data": {"ltp": pe_ltp, "bid_price": pe_ltp - 0.10,
                                "ask_price": pe_ltp + 0.10},
                "option_greeks": {"delta": -delta, "gamma": gamma},
            },
        })
    return rows


def test_auto_scalp_remains_independent_when_macro_sniper_is_active():
    ready = evaluate_continuous_scalp(**_context())
    assert ready["trigger"] is True
    assert ready["all_five"] is True
    assert ready["signature_count"] == 5
    assert ready["side"] == "CE"
    assert ready["order"] == "SCALP_BUY_CE"
    assert ready["engine_state"] == "AUTO_SCALPING"

    # The ₹35K sleeve is a separate risk book.  A live macro position must not
    # park AUTO at 0 Qty / TREND ONLY while its own ignition is valid.
    macro = evaluate_continuous_scalp(**_context(main_state="IN_TRADE"))
    assert macro["trigger"] is True
    assert macro["reason"] == "SCALP_READY"
    assert macro["trigger_route"] == "TREND_CONTINUATION"

    early = evaluate_continuous_scalp(**_context(now=datetime(2026, 11, 10, 9, 15)))
    assert early["trigger"] is False
    assert early["reason"] == "OUTSIDE_SCALP_SESSION"

    late = evaluate_continuous_scalp(**_context(now=datetime(2026, 11, 10, 15, 1)))
    assert late["trigger"] is False
    assert late["reason"] == "OUTSIDE_SCALP_SESSION"


def test_flexible_routes_still_require_a_real_ignition():
    # Structure alone at OBI 1.20 is below the Trend Continuation floor and the
    # SMC route requires an actual p10/p90 sweep, so AUTO correctly waits.
    no_flow = evaluate_continuous_scalp(**_context(ce_obi_5=1.20))
    assert no_flow["trigger"] is False
    assert no_flow["reason"] == "NO_FLEXIBLE_ROUTE"

    no_structure = evaluate_continuous_scalp(**_context(
        spot=24_900, spot_vwap=24_950, spot_ema5=24_945, p10=0,
        ce_obi_5=1.20, ce_tbq=100_000,
    ))
    assert no_structure["trigger"] is False
    assert no_structure["reason"] == "NO_FLEXIBLE_ROUTE"


def test_dead_tape_pauses_before_brokerage_bleed():
    dead = evaluate_continuous_scalp(**_context(spot_atr_1m=2.5, spot_range_1m=2.5))
    assert dead["trigger"] is False
    assert dead["reason"] == "DEAD_TAPE_AUTOPAUSE"
    assert dead["engine_state"] == "PAUSED_DEAD_TAPE"


def test_position_sizer_stays_under_35000_and_picks_delta_sweet_spot():
    contract = select_scalp_contract(_chain(), "CE")
    assert contract is not None
    assert contract["delta"] == pytest.approx(0.56)
    assert contract["strike"] == 24_950.0
    assert contract["qty"] == 300
    assert contract["deployed"] <= MAX_SCALP_CAPITAL
    assert contract["deployed"] == pytest.approx(31_500.0)

    middle = select_scalp_contract(_chain(), "CE", capital=35_000, lot_size=75)
    assert middle is not None

    no_eligible = select_scalp_contract(_chain(), "CE", capital=1_000)
    assert no_eligible is None


def test_management_locks_breakeven_books_half_and_trails_tick_by_tick():
    engine = ContinuousScalpEngine()
    assert engine.open({
        "symbol": "NIFTY", "side": "CE", "strike": 24_950, "ltp": 105.0,
        "qty": 300, "sl_points": 4.0,
    }, now=datetime(2026, 11, 10, 10, 30))["ok"] is True
    trade = engine.active

    at_lock = manage_continuous_scalp(trade, 110.50)
    assert at_lock["breakeven"] is True
    assert trade["current_trailing_sl"] == 106.50
    assert at_lock["stage1"] is False

    at_book = manage_continuous_scalp(trade, 111.50)
    assert at_book["stage1"] is True
    assert trade["qty_open"] == 150
    assert trade["booked_legs"][0]["qty"] == 150
    assert trade["current_trailing_sl"] == 109.50

    trail = manage_continuous_scalp(trade, 116.00)
    assert trail["exit"] is False
    assert trade["current_trailing_sl"] == 114.00

    stopped = manage_continuous_scalp(trade, 113.90)
    assert stopped["exit"] is True
    assert stopped["reason"] == "SCALP_TRAIL_STOP"
    assert trade["status"] == "CLOSED"


def test_flip_requires_profit_and_opposite_ignition():
    flip = detect_scalp_flip(
        "CE", pe_obi_5=1.62, pe_tbq=170_000, pe_tsq=100_000,
        profit_booked=True,
    )
    assert flip["flip"] is True
    assert flip["side"] == "PE"

    not_paid = detect_scalp_flip(
        "CE", pe_obi_5=1.62, pe_tbq=170_000, pe_tsq=100_000,
        profit_booked=False,
    )
    assert not_paid["flip"] is False

    cooldown = detect_scalp_flip(
        "CE", pe_obi_5=1.62, pe_tbq=170_000, pe_tsq=100_000,
        profit_booked=True, cooldown_sec=12,
    )
    assert cooldown["flip"] is False


def test_sleeve_ledger_marks_live_pnl_and_emits_complete_api_fields():
    ledger = ScalpSleeveLedger().ensure_day("2026-11-10")
    assert ledger.start_trade({
        "symbol": "NIFTY", "side": "PE", "strike": 24_950,
        "entry": 104.20, "qty": 300, "stop": 100.20,
    })["ok"] is True
    ledger.mark(109.60)
    snap = ledger.snapshot(main_sniper_total_pnl_today=6_200)
    assert snap["scalp_capital_allocated"] == SCALP_SLEEVE_CAPITAL
    assert snap["scalp_capital_deployed"] == 31_260.0
    assert snap["scalp_live_open_pnl"] == 1_620.0
    assert snap["scalp_realized_pnl_today"] == 0.0
    assert snap["scalp_total_pnl_today"] == 1_620.0
    assert snap["scalp_roi_pct_today"] == pytest.approx(4.63)
    assert snap["combined_intraday_net_pnl"] == 7_820.0
    assert snap["scalp_stats"]["active_strike"] == "NIFTY 24950 PE"
    assert snap["scalp_stats"]["engine_state"] == "AUTO_SCALPING"
    assert snap["scalp_live_ticker"].startswith("NIFTY 24950 PE")

    ledger.close(109.60, reason="SCALP_TRAIL_STOP")
    closed = ledger.snapshot()
    assert closed["scalp_realized_pnl_today"] == 1_620.0
    assert closed["scalp_live_open_pnl"] == 0.0
    assert closed["scalp_stats"]["total_scalps"] == 1
    assert closed["scalp_stats"]["wins"] == 1


def test_live_payload_and_http_stream_expose_scalp_sleeve(monkeypatch, tmp_path):
    now = time.time()
    feed = {
        "updated_epoch": now,
        "updated_at": "2026-11-10T10:30:00+05:30",
        "market_open": True,
        "socket_connected": True,
        "prices": {"NSE_INDEX|Nifty 50": 24_960.0},
        "scalp_sleeve": {
            "scalp_capital_allocated": 35_000.0,
            "scalp_capital_deployed": 31_260.0,
            "scalp_live_open_pnl": 1_620.0,
            "scalp_realized_pnl_today": 8_450.0,
            "scalp_total_pnl_today": 10_070.0,
            "scalp_roi_pct_today": 28.77,
            "combined_intraday_net_pnl": 16_270.0,
            "scalp_stats": {"total_scalps": 6, "wins": 5, "losses": 1,
                            "win_rate_pct": 83.33, "active_strike": "NIFTY 24950 PE",
                            "engine_state": "AUTO_SCALPING"},
        },
    }
    path = tmp_path / "live_feed.json"
    path.write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FEED_FILE", path)

    snap = live_ticks.snapshot("NIFTY", now=now)
    assert snap["scalp_sleeve"]["scalp_live_open_pnl"] == 1_620.0
    assert snap["scalp_live_open_pnl"] == 1_620.0
    assert snap["scalp_realized_pnl_today"] == 8_450.0
    assert snap["scalp_total_pnl_today"] == 10_070.0
    assert snap["scalp_roi_pct_today"] == 28.77
    assert snap["scalp_stats"]["active_strike"] == "NIFTY 24950 PE"
    assert snap["combined_intraday_net_pnl"] == 16_270.0

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/live-ticks?symbol=NIFTY", timeout=3) as response:
            live = json.loads(response.read().decode("utf-8"))
        assert live["scalp_live_open_pnl"] == 1_620.0
        assert live["scalp_sleeve"]["scalp_realized_pnl_today"] == 8_450.0

        with urllib.request.urlopen(f"{base}/api/tick-stream?symbol=NIFTY", timeout=3) as response:
            line = response.readline().decode("utf-8")
        assert line.startswith("data: ")
        streamed = json.loads(line[6:].strip())
        assert streamed["scalp_roi_pct_today"] == 28.77
        assert streamed["scalp_stats"]["engine_state"] == "AUTO_SCALPING"
    finally:
        httpd.shutdown()
        httpd.server_close()
