"""Replay proofs for the five choke removals and the asymmetric rider.

The two audited scenarios are represented by their real values: the
66.7 / 92.2 / 70.0 model consensus crushed to 56.5 by an advisory chop number,
and the 14:56 87.5% downside / OBI 10.50 wave that the old 5/5 checklist vetoed.
"""

from __future__ import annotations

import copy
import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from execution.asymmetric_trend_rider import (
    attach_plan_to_trade,
    build_asymmetric_plan,
    manage_asymmetric_trade,
)
from model.tri_model_orchestrator import build_model_quorum
from state.eod_pnl_reconciler import reconcile_eod
from state.paper_trade_ledger import PaperLedger, paper_trade_from_live
from strategy.continuous_scalp_engine import (
    evaluate_continuous_scalp,
    select_scalp_contract,
)
from strategy.master_signal_arbitrator import build_master_arbitrator
from strategy.model_oscillator import build_model_oscillator
from strategy.multi_angle_playbook_engine import evaluate_playbooks

IST = timezone(timedelta(hours=5, minutes=30))
REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "live_feed_1456.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _quorum_feed(*, bull: float, obi: float = 1.10, tbq: float = 1.10) -> dict:
    """A real-shape feed with exactly one or two confirmed model roles."""
    now = 1_794_389_100.0
    chain_row = {
        "strike_price": 25_100,
        "call_options": {
            "market_data": {"ltp": 120.0, "bid_price": 119.5, "ask_price": 120.5},
            "option_greeks": {"delta": 0.55, "gamma": 0.001, "theta": -1.0},
        },
        "put_options": {
            "market_data": {"ltp": 200.0, "bid_price": 199.5, "ask_price": 200.5},
            "option_greeks": {"delta": 0.55, "gamma": 0.001, "theta": -1.0},
        },
    }
    return {
        "updated_epoch": now,
        "updated_at": "2026-11-11T14:56:03+05:30",
        "spot": {
            "NIFTY": {
                "ltp": 25_100.0,
                "vwap": 25_200.0,
                "current_candle_5m": {"t": now, "o": 25_220, "h": 25_230, "l": 25_090, "c": 25_100},
                "candles_5m": [
                    {"t": now - 600, "o": 25_250, "h": 25_260, "l": 25_200, "c": 25_220},
                    {"t": now, "o": 25_220, "h": 25_230, "l": 25_090, "c": 25_100},
                ],
                "candles_1m": [
                    {"t": now - 60, "o": 25_220, "h": 25_225, "l": 25_210, "c": 25_220},
                    {"t": now, "o": 25_220, "h": 25_220, "l": 25_095, "c": 25_100},
                ],
            }
        },
        "option": {
            "NIFTY": {
                "ltp": 200.0,
                "atp_vwap": 220.0,
                "option_type": "PE",
                "strike_price": 25_100,
                "obi_5": obi,
                "tbq_tsq": tbq,
                "depth": {"obi_5": obi, "tbq_tsq": tbq},
            }
        },
        "model_oscillator": {
            "bullish_upside_prob_pct": bull,
            "bearish_downside_prob_pct": round(100.0 - bull, 1),
        },
        "model_brain": {
            "NIFTY": {"paths_up": bull, "samples": 100.0, "p10": 25_200.0, "p90": 25_350.0}
        },
        "full_spectrum": {
            "NIFTY": {
                "pcr": {"pcr": 1.00, "doi_pcr": 1.00},
                "depth": {"obi_5": obi, "tbq_tsq": tbq},
                "futures": {"ltp": 25_100.0, "atp": 25_100.0},
            }
        },
        "option_chain": {"NIFTY": [chain_row]},
        "smc": {
            "NIFTY": {
                "structures": {
                    "5m": {"active_bos": "", "active_choch": ""},
                    "1m": {"active_bos": "", "active_choch": ""},
                }
            }
        },
        "scalper": {
            "NIFTY": {
                "decision": {
                    "trigger": True, "side": "CE", "entry": 200.0,
                    "target": 210.0, "sl_points": 5.0, "confidence_pct": 95.0,
                    "reason": "SCALP_READY",
                }
            }
        },
    }


def test_chop_pressure_never_crushes_true_model_consensus() -> None:
    oscillator = build_model_oscillator(
        base_upside_pct=66.7,
        small_probability_pct=92.2,
        mini_probability_pct=70.0,
    )
    # 0.40*66.7 + 0.25*92.2 + 0.35*70.0 = 74.2, not the audited 56.5.
    assert oscillator["bullish_upside_prob_pct"] == pytest.approx(74.2)
    assert oscillator["directional_probability_pct"] == pytest.approx(74.2)
    assert oscillator["directional_side"] == "BULL"
    assert oscillator["chop_pressure_suppression_applied"] is False
    assert oscillator["chop_neutral_pressure_pct"] > 0


def test_single_model_super_conviction_path_fires_with_vwap_alignment() -> None:
    quorum = build_model_quorum(_quorum_feed(bull=12.5), symbol="NIFTY")
    assert quorum["trigger_path"] == "SINGLE_MODEL_SUPER_CONVICTION"
    assert quorum["side"] == "PE"
    assert quorum["single_model"]["confidence"] >= 78.0
    assert quorum["single_model"]["price_aligned"] is True
    assert quorum["auto_execute"] is True
    assert quorum["trigger_label"].startswith("SINGLE-SNIPER:")
    assert "25M" in quorum["trigger_label"] or "102M" in quorum["trigger_label"]


def test_two_of_three_consensus_path_fires_without_third_model() -> None:
    quorum = build_model_quorum(_quorum_feed(bull=30.0, obi=1.30, tbq=1.30), symbol="NIFTY")
    assert quorum["trigger_path"] == "MULTI_MODEL_CONSENSUS"
    assert quorum["side"] == "PE"
    assert quorum["auto_execute"] is True
    assert quorum["individual_floor"] == 60.0
    assert quorum["quorum_count"] == "2/3"
    assert quorum["trigger_label"].startswith("MULTI:")


def test_1456_wave_fires_under_2ms_with_flexible_routes() -> None:
    feed = _fixture()
    first = evaluate_playbooks(feed, symbol="NIFTY")
    assert first["auto_execute"] is True
    assert first["side"] == "PE"
    assert first["entry"]["contract"] == "NIFTY 25200 PE"
    assert any(route["pass"] for route in first["flexible_routes"])
    assert first["route_hit"] is True
    assert "AUTO-EXECUTED" in first["headline"]

    # The dual-path trigger is the latency-critical piece.  The playbook also
    # assembles four UI angles, so its budget is validated separately.
    samples = []
    for _ in range(15):
        started = time.perf_counter()
        quorum = build_model_quorum(feed, symbol="NIFTY")
        samples.append((time.perf_counter() - started) * 1000.0)
    assert quorum["auto_execute"] is True
    assert quorum["trigger_path"] in {"MULTI_MODEL_CONSENSUS", "SINGLE_MODEL_SUPER_CONVICTION"}
    assert statistics.median(samples) < 2.0, f"median trigger latency {statistics.median(samples):.3f}ms"


def test_stale_non_core_field_keeps_scores_but_core_gate_uses_12s_lkg() -> None:
    now = time.time()
    feed = _quorum_feed(bull=30.0, obi=2.0, tbq=2.0)
    feed["liveness_epoch"] = now - 5.0
    feed["updated_epoch"] = now - 5.0
    # Simulate a single lagging scanner field: it is not a core liveness fact.
    feed["scanner"] = {"NIFTY": [{"key": "one_of_37", "value": "STALE"}]}
    live = build_master_arbitrator(feed, symbol="NIFTY", now=now, feed_live=True)
    assert live["data_gate"]["allowed"] is True
    assert live["data_gate"]["core_fields"]["spot_present"] is True
    assert live["data_gate"]["core_fields"]["option_present"] is True
    assert live["sleeve_a"]["candidates"]
    assert any(float(row["edge_score"]) > 0 for row in live["sleeve_a"]["candidates"])
    assert all(row["side"] for row in live["sleeve_a"]["candidates"] if row["trigger"])
    assert live["sleeve_a"]["selected"] is not None

    feed["liveness_epoch"] = now - 13.0
    stale = build_master_arbitrator(feed, symbol="NIFTY", now=now, feed_live=True)
    assert stale["data_gate"]["allowed"] is False
    assert stale["data_gate"]["lkg_window_sec"] == 12.0
    assert any(float(row["edge_score"]) > 0 for row in stale["sleeve_a"]["candidates"])


def test_asymmetric_small_sl_breakeven_35_35_30_and_uncapped_runner() -> None:
    start = datetime(2026, 11, 11, 10, 0, tzinfo=IST)
    plan = build_asymmetric_plan(
        entry_price=100.0, ignition_low=91.0, qty=75, side="CE",
        trigger_label="TEST MULTI", created_at=start,
    )
    assert 6.0 <= plan["risk_pct"] <= 7.0
    assert plan["initial_sl"] == pytest.approx(93.0)
    assert (plan["t1_qty"], plan["t2_qty"], plan["runner_qty"]) == (26, 26, 23)

    trade = {
        "status": "OPEN", "entry_price": 100.0, "initial_qty": 75, "qty": 75,
        "qty_open": 75, "realized_pnl_rs": 0.0, "booked_legs": [],
        "entry_at": start.isoformat(), "option_type": "CE",
    }
    attach_plan_to_trade(trade, plan)

    before = manage_asymmetric_trade(
        trade, 106.0, obi_5=0.90, ema9=98.0, prior_1m_low=95.0, now=start + timedelta(seconds=10),
    )
    assert before["status"] == "OPEN"
    assert trade["current_trailing_sl"] == pytest.approx(93.0)

    lock = manage_asymmetric_trade(
        trade, 107.0, obi_5=0.90, ema9=100.0, prior_1m_low=96.0, now=start + timedelta(seconds=20),
    )
    assert "BREAKEVEN_LOCKED" in lock["events"]
    assert trade["current_trailing_sl"] == pytest.approx(101.5)

    t1 = manage_asymmetric_trade(
        trade, plan["t1_price"], obi_5=1.10, ema9=104.0, prior_1m_low=99.0,
        now=start + timedelta(seconds=30),
    )
    assert "T1_35PCT_BOOKED" in t1["events"]
    assert trade["qty_open"] == 49
    assert trade["booked_legs"][0]["qty"] == 26

    t2 = manage_asymmetric_trade(
        trade, plan["t2_price"], obi_5=1.10, ema9=110.0, prior_1m_low=108.0,
        now=start + timedelta(seconds=40),
    )
    assert "T2_35PCT_BOOKED" in t2["events"]
    assert trade["qty_open"] == 23
    assert trade["status"] == "OPEN"
    assert trade["current_trailing_sl"] >= plan["t1_price"]

    run = manage_asymmetric_trade(
        trade, 140.0, obi_5=0.95, ema9=120.0, prior_1m_low=125.0,
        now=start + timedelta(seconds=50),
    )
    assert run["status"] == "OPEN"
    assert trade["qty_open"] == 23
    assert trade["runner_hold"] is True

    exited = manage_asymmetric_trade(
        trade, 130.0, obi_5=0.60, ema9=120.0, prior_1m_low=125.0,
        now=start + timedelta(seconds=60),
    )
    assert exited["status"] == "CLOSED"
    assert "RUNNER_EXITED" in exited["events"]
    assert trade["qty_open"] == 0
    assert trade["gross_pnl_rs"] > 0
    assert trade["net_pnl_rs"] == pytest.approx(trade["gross_pnl_rs"] - trade["brokerage_taxes"], abs=0.01)
    assert trade["vaulted"] == pytest.approx(round(max(0, trade["net_pnl_rs"]) * 0.50, 2))


def test_eod_grand_total_and_vault_reconcile_with_zero_discrepancy() -> None:
    rows = [
        {
            "trade_id": "S1", "date": "2026-11-11", "sleeve": "SCALP_35K",
            "status": "CLOSED", "gross": 1200.0, "costs": 50.0, "net": 1150.0,
            "entry_price": 100.0, "exit_price": 115.0, "initial_qty": 75,
            "option_type": "CE", "trigger_label": "SCALP TREND_CONTINUATION",
        },
        {
            "trade_id": "B1", "date": "2026-11-11", "sleeve": "SNIPER_65K",
            "status": "CLOSED", "gross": -500.0, "costs": 40.0, "net": -540.0,
            "entry_price": 200.0, "exit_price": 193.0, "initial_qty": 75,
            "option_type": "PE", "trigger_label": "MULTI: 102M (67%) + 4M (74%)",
        },
    ]
    summary = reconcile_eod(rows, date="2026-11-11", journal_rows=rows, reported_vault=575.0)
    assert summary["scalp_35k_realized_pnl"] == 1150.0
    assert summary["sniper_65k_realized_pnl"] == -540.0
    assert summary["grand_total_net_pnl"] == 610.0
    assert summary["vault_locked_inr"] == 575.0
    assert summary["vault_discrepancy_inr"] == 0.0
    assert summary["ledger_journal_discrepancy_inr"] == 0.0
    assert summary["reconciled_zero_discrepancy"] is True
    assert summary["trades_won"] == 1 and summary["trades_total"] == 2
    assert summary["win_rate_pct"] == 50.0
    assert summary["profit_factor"] == pytest.approx(2.13)
    assert summary["avg_sl_lost_inr"] == -540.0
    assert summary["avg_trend_won_inr"] == 1150.0


def test_asymmetric_legs_map_into_canonical_ledger_and_eod_row() -> None:
    trade = {
        "status": "CLOSED", "trade_id": "ASYM-1", "qty": 75, "initial_qty": 75,
        "qty_open": 0, "entry_price": 100.0, "exit_price": 130.0,
        "gross_pnl_rs": 1623.40, "brokerage_taxes": 40.0,
        "option_symbol": "NIFTY 25200 CE", "option_type": "CE",
        "sleeve": "SNIPER_65K", "trigger_label": "MULTI: 25M (92%) + 4M (70%)",
        "entry_at": "2026-11-11T10:00:00+05:30",
        "exit_at": "2026-11-11T10:20:00+05:30",
        "booked_legs": [
            {"leg": "ASYM_T1_35PCT", "qty": 26, "price": 113.50, "pnl": 351.0},
            {"leg": "ASYM_T2_35PCT", "qty": 26, "price": 122.40, "pnl": 582.4},
            {"leg": "ASYM_RUNNER_30PCT", "qty": 23, "price": 130.00, "pnl": 690.0},
        ],
    }
    row = paper_trade_from_live(trade, date="2026-11-11", symbol="NIFTY")
    assert (row.t1_qty, row.t1_price) == (26, 113.50)
    assert (row.t2_qty, row.t2_price) == (26, 122.40)
    assert (row.sl_price, row.sl_reason) == (130.0, "ASYM_RUNNER_30PCT")
    assert row.net == pytest.approx(1583.40)
    assert row.vaulted == pytest.approx(791.70)
    serialized = PaperLedger(trades=[row]).serialize_trade(row)
    assert serialized["t1"]["qty"] == 26 and serialized["t2"]["qty"] == 26
    assert serialized["sl"]["reason"] == "ASYM_RUNNER_30PCT"
    eod = reconcile_eod([row], date="2026-11-11", journal_rows=[row])
    assert eod["grand_total_net_pnl"] == 1583.40
    assert eod["vault_locked_inr"] == 791.70


def test_35k_auto_has_independent_route_and_minimum_one_lot() -> None:
    decision = evaluate_continuous_scalp(
        datetime(2026, 11, 11, 10, 30), symbol="NIFTY",
        main_state="IN_TRADE", spot=24_960, spot_atr_1m=7.5,
        spot_range_1m=8.0, option_atr_1m=2.8, spread=0.20,
        spot_vwap=24_950, spot_ema5=24_945, p10=24_930, p90=24_990,
        kronos_slope=0.35, side_bias="CE", ce_ltp=105.0, ce_atp=101.0,
        pe_ltp=88.0, pe_atp=90.0, ce_obi_5=1.30, ce_tbq=150_000,
        ce_tsq=100_000, pe_obi_5=1.10, pe_tbq=80_000, pe_tsq=100_000,
        futures_volume_ratio=1.00,
    )
    assert decision["trigger"] is True
    assert decision["trigger_route"] == "TREND_CONTINUATION"
    assert decision["engine_state"] == "AUTO_SCALPING"

    chain = [{
        "strike_price": 25_000,
        "call_options": {
            "instrument_key": "NSE_FO|CE25000",
            "market_data": {"ltp": 105.0, "bid_price": 104.9, "ask_price": 105.1},
            "option_greeks": {"delta": 0.20, "gamma": 0.0005},
        },
    }]
    contract = select_scalp_contract(chain, "CE", capital=35_000, lot_size=75)
    assert contract is not None
    assert contract["qty"] >= 75
    assert contract["qty"] % 75 == 0
    assert contract["deployed"] <= 35_000
    assert contract["min_one_lot_enforced"] is True
