"""End-to-end tests for the institutional pro trading journal.

Covers the lifecycle recorder, annotated chart renderer, all four PDF scopes,
and the real HTTP summary/download endpoints served by dashboard/server.py.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest
from PIL import Image

from dashboard.server import Handler
from reports.chart_snapshot_renderer import render_equity_curve, render_trade_chart
from reports.pro_journal_pdf import build_journal_pdf, resolve_period
from state.trade_journal_store import TradeJournalStore, chart_labels


def _option_candles(base: datetime, count: int = 45) -> list[dict]:
    rows = []
    for index in range(count):
        price = 72.0 + index * 0.62
        rows.append({
            "time": (base + timedelta(minutes=index)).isoformat(),
            "open": round(price, 2),
            "high": round(price + 1.8, 2),
            "low": round(price - 1.4, 2),
            "close": round(price + 0.7, 2),
            "volume": 1200 + index * 35,
        })
    return rows


def _spot_candles(base: datetime, count: int = 30) -> list[dict]:
    rows = []
    for index in range(count):
        price = 24930.0 + index * 2.1
        rows.append({
            "time": (base + timedelta(minutes=5 * index)).isoformat(),
            "open": round(price, 2),
            "high": round(price + 8, 2),
            "low": round(price - 7, 2),
            "close": round(price + 3, 2),
            "volume": 50_000 + index * 120,
        })
    return rows


def _trade(base: datetime) -> dict:
    return {
        "trade_id": "test-journal-trade-001",
        "status": "OPEN",
        "entry_at": base.isoformat(),
        "option_symbol": "NIFTY 24950 CE",
        "option_type": "CE",
        "entry_price": 72.5,
        "initial_qty": 300,
        "qty_open": 300,
        "qty": 300,
        "initial_sl": 65.0,
        "current_trailing_sl": 65.0,
        "target_1": 86.5,
        "target_2_p90": 104.2,
        "tsl_gear": 1,
        "realized_pnl_rs": 0.0,
        "booked_legs": [],
        "risk_points": 7.5,
    }


def _context() -> dict:
    return {
        "symbol": "NIFTY",
        "spot": 24930.0,
        "spot_exit": 25010.0,
        "basis": 34.85,
        "vix": 12.84,
        "pcr": 1.13,
        "obi_5": 1.72,
        "tbq_tsq": 1.42,
        "doi_5m_pct": -10.1,
        "atp": 72.4,
        "paths_up": 84,
        "samples": 100,
        "p10": 24920.0,
        "median": 24960.0,
        "p90": 25020.0,
        "mtf": {
            "m1": {"trend": "BULL"},
            "m5": {"trend": "BULL"},
            "m15": {"trend": "BULL"},
        },
        "futures_volume_ratio": 2.8,
        "expiry": "2026-11-12",
        "mode": "PAPER",
        "greeks": {
            "delta": 0.54, "gamma": 0.0018, "theta": -3.62,
            "vega": 11.24, "iv": 13.8,
        },
        "vault_locked_inr": 0.0,
    }


@pytest.fixture()
def lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRONOS_JOURNAL_FILE", str(tmp_path / "journal.json"))
    monkeypatch.setenv("KRONOS_JOURNAL_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("KRONOS_JOURNAL_DPI", "80")
    store = TradeJournalStore(tmp_path / "journal.json")
    base = datetime(2026, 11, 10, 9, 42)
    option = _option_candles(base)
    spot = _spot_candles(base)
    trade = _trade(base)
    context = _context()

    store.record_live_trade(trade, context=context, option_candles=option, spot_candles=spot)
    trade.update({
        "booked_legs": [{
            "leg": "T1", "qty": 150, "price": 86.5, "pnl": 2100.0,
            "at": (base + timedelta(minutes=12)).isoformat(),
        }],
        "qty_open": 150,
        "realized_pnl_rs": 2100.0,
        "current_trailing_sl": 74.0,
        "tsl_gear": 2,
    })
    store.record_live_trade(
        trade, context={**context, "spot": 24960.0},
        option_candles=option[:20], spot_candles=spot[:10],
    )
    trade.update({
        "status": "CLOSED",
        "exit_at": (base + timedelta(minutes=38)).isoformat(),
        "exit_price": 104.2,
        "exit_reason": "T2",
        "qty_open": 0,
        "booked_legs": trade["booked_legs"] + [{
            "leg": "T2", "qty": 150, "price": 104.2, "pnl": 4755.0,
            "at": (base + timedelta(minutes=37)).isoformat(),
        }],
        "realized_pnl_rs": 6855.0,
        "current_trailing_sl": 86.5,
        "tsl_gear": 3,
    })
    record = store.record_live_trade(
        trade, context={**context, "spot": 25010.0, "spot_exit": 25010.0},
        option_candles=option, spot_candles=spot,
    )
    return store, record, tmp_path


def test_full_lifecycle_record_has_reasons_greeks_and_chart_windows(lifecycle):
    store, record, _ = lifecycle
    assert record["status"] == "CLOSED"
    assert record["date_ist"] == "2026-11-10"
    assert record["t1_hit"] is True
    assert record["t2_hit"] is True
    assert record["final_exit_reason"] == "T2_HIT"
    assert record["total_qty"] == 300
    assert record["t1_booked_qty"] == 150
    assert record["final_exit_qty"] == 150
    assert record["max_gear_reached"] == 3
    assert record["gross_pnl_inr"] == pytest.approx(6855.0)
    assert record["estimated_charges_inr"] > 0
    assert record["net_pnl_inr"] == pytest.approx(6855.0 - record["estimated_charges_inr"])
    assert record["roi_pct"] > 25
    assert record["greeks_entry"]["delta"] == pytest.approx(0.54)
    assert record["greeks_entry"]["iv"] == pytest.approx(13.8)
    assert record["obi_5_entry"] == pytest.approx(1.72)
    assert record["tbq_tsq_ratio_entry"] == pytest.approx(1.42)
    assert record["pcr_entry"] == pytest.approx(1.13)
    assert len(record["charts"]["option_candles"]) == 45
    assert len(record["charts"]["spot_candles"]) == 30
    assert record["charts"]["p10"] == pytest.approx(24920.0)
    assert record["charts"]["p90"] == pytest.approx(25020.0)
    assert record["model_probability_pct"] == 84
    assert record["model_direction"] == "BULLISH"
    assert "M1 BULL" in record["mtf_summary"]
    assert "84%" in record["model_reason_simple"]
    assert "1.72" in record["technical_reason_simple"]
    assert "1.42" in record["technical_reason_simple"]
    assert "T1" in record["exit_reason_simple"]
    assert "T2" in record["exit_reason_simple"]
    assert record["model_reason_simple_te"]
    assert record["technical_reason_simple_te"]
    assert "క్వాంటిటీ" in record["exit_reason_simple_te"]
    assert store.get("test-journal-trade-001")["trade_id"] == "test-journal-trade-001"
    assert store.records_between("2026-11-10", "2026-11-10") == [record]

    labels = chart_labels(record)
    assert labels[0].startswith("ENTRY: ₹72.50")
    assert labels[1] == "SL: ₹65.00 (-₹7.50 pts)"
    assert "T1 HIT" in labels[2] and "+₹2,100" in labels[2]
    assert "T2 / EXIT" in labels[3] and "+₹4,755" in labels[3]


def test_unchanged_stop_is_initial_sl_not_trailing_sl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRONOS_JOURNAL_FILE", str(tmp_path / "journal.json"))
    store = TradeJournalStore(tmp_path / "journal.json")
    base = datetime(2026, 11, 10, 13, 5)
    trade = {
        **_trade(base),
        "trade_id": "test-initial-sl-001",
        "status": "CLOSED",
        "exit_at": (base + timedelta(minutes=17)).isoformat(),
        "exit_price": 87.5,
        "exit_reason": "SL",
        "initial_sl": 87.5,
        "current_trailing_sl": 87.5,
        "tsl_gear": 3,
        "qty_open": 0,
        "realized_pnl_rs": -2250.0,
        "booked_legs": [{
            "leg": "SL", "qty": 300, "price": 87.5, "pnl": -2250.0,
            "at": (base + timedelta(minutes=17)).isoformat(),
        }],
    }
    record = store.record_live_trade(trade, context=_context(), option_candles=[], spot_candles=[])
    assert record["t1_hit"] is False
    assert record["final_exit_reason"] == "INITIAL_SL_HIT"
    assert "initial stop" in record["exit_reason_simple"]


def test_annotated_chart_and_equity_curve_render_valid_high_dpi_pngs(lifecycle):
    _, record, tmp_path = lifecycle
    chart = render_trade_chart(record, tmp_path / "charts" / "trade.png", dpi=110)
    assert chart.is_file()
    assert chart.stat().st_size > 20_000
    with Image.open(chart) as image:
        assert image.format == "PNG"
        assert image.width > 1000
        assert image.height > 400

    summary = {
        "days": [
            {"date": "2026-11-10", "net": record["net_pnl_inr"]},
        ],
    }
    equity = render_equity_curve([record], summary, tmp_path / "charts" / "equity.png", dpi=110)
    assert equity.is_file()
    assert equity.stat().st_size > 10_000
    with Image.open(equity) as image:
        assert image.format == "PNG"


@pytest.mark.parametrize(
    "period,start,end",
    [
        ("daily", "2026-11-10", None),
        ("weekly", "2026-11-10", None),
        ("monthly", "2026-11-10", None),
        ("custom", "2026-11-01", "2026-11-30"),
    ],
)
def test_pdf_builder_all_scopes_embeds_images(lifecycle, period, start, end):
    store, _, tmp_path = lifecycle
    pdf = build_journal_pdf(
        store, period, start, end,
        output_dir=tmp_path / "pdfs", chart_dpi=90, missed=[],
    )
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF-")
    assert len(data) > 50_000
    pages = re.findall(rb"/Type\s*/Page\b", data)
    images = re.findall(rb"/Subtype\s*/Image", data)
    assert len(pages) >= 3
    assert len(images) >= 2
    assert pdf.name.startswith("KRONOS_Pro_Trading_Journal_")
    assert pdf.name.endswith(".pdf")


def test_resolve_period_scopes_are_inclusive():
    daily = resolve_period("daily", "2026-11-10", now="2026-12-01T00:00:00+05:30")
    assert daily["start"] == daily["end"] == "2026-11-10"
    weekly = resolve_period("weekly", "2026-11-10", now="2026-12-01T00:00:00+05:30")
    assert weekly["start"] == "2026-11-09" and weekly["end"] == "2026-11-15"
    monthly = resolve_period("monthly", "2026-11-10", now="2026-12-01T00:00:00+05:30")
    assert monthly["start"] == "2026-11-01" and monthly["end"] == "2026-11-30"
    custom = resolve_period("custom", "2026-11-30", "2026-11-01", now="2026-12-01T00:00:00+05:30")
    assert custom["start"] == "2026-11-01" and custom["end"] == "2026-11-30"


def test_http_summary_and_download_pdf_endpoints(lifecycle):
    _, record, _ = lifecycle
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/journal/summary?period=daily&from=2026-11-10", timeout=90) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["range"]["from"] == "2026-11-10"
        assert payload["trades"][0]["trade_id"] == record["trade_id"]
        assert payload["trades"][0]["t1_hit"] is True
        assert payload["summary"]["trades"] == 1
        assert payload["charts"][record["trade_id"]].startswith("data:image/png;base64,")
        assert payload["trades"][0]["model_reason_simple"]

        for period, start, end in [
            ("daily", "2026-11-10", None),
            ("weekly", "2026-11-10", None),
            ("monthly", "2026-11-10", None),
            ("custom", "2026-11-01", "2026-11-30"),
        ]:
            params = {"period": period, "from": start}
            if end:
                params["to"] = end
            with urlopen(f"{base}/api/journal/download-pdf?{urlencode(params)}", timeout=120) as response:
                data = response.read()
                assert response.status == 200
                assert response.headers["Content-Type"] == "application/pdf"
                disposition = response.headers["Content-Disposition"]
                assert "attachment;" in disposition
                assert ".pdf" in disposition
            assert data.startswith(b"%PDF-")
            assert len(data) > 50_000
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
