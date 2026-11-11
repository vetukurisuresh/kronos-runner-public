"""Pro Trade Ledger: sub-bookings, missed setups, and calendar summaries."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from state.paper_trade_ledger import (  # noqa: E402
    MissedSetup,
    PaperLedger,
    PaperTrade,
    paper_trade_from_live,
)


def _trade(**overrides) -> PaperTrade:
    base = dict(
        date="2026-11-09", light="GREEN", symbol="NIFTY", strike="NIFTY 25000 CE",
        option_type="CE", qty=300, entry=150.0, exit=151.5, points=1.5,
        gross=225.0, costs=0.0, net=225.0, vaulted=157.5, outcome="WIN",
        entry_at="2026-11-09T09:45:00", exit_at="2026-11-09T10:30:00",
        trade_id="t-1", status="CLOSED", initial_qty=300, qty_open=0,
        t1_qty=150, t1_price=167.1, t1_pnl=2565.0, t1_at="2026-11-09T10:05:00",
        t2_qty=0, t2_price=0.0, t2_pnl=0.0,
        sl_price=151.5, sl_pnl=225.0, sl_at="2026-11-09T10:30:00", sl_reason="SL",
        mfe_price=180.4, mae_price=149.0,
        booked_legs=[
            {"leg": "T1", "qty": 150, "price": 167.1, "pnl": 2565.0,
             "at": "2026-11-09T10:05:00"},
            {"leg": "SL", "qty": 150, "price": 151.5, "pnl": 225.0,
             "at": "2026-11-09T10:30:00"},
        ],
    )
    base.update(overrides)
    return PaperTrade(**base)


def test_t1_t2_sl_rows_and_green_calendar() -> None:
    ledger = PaperLedger()
    ledger.record(_trade())
    day = ledger.day_ledger("2026-11-09")

    row = day["trades"][0]
    assert row["t1"]["qty"] == 150
    assert row["t1"]["pnl"] == 2565.0
    assert row["t2"] is None
    assert row["sl"]["price"] == 151.5
    assert row["sl"]["pnl"] == 225.0
    assert row["max_profit"] == 9_120.0
    assert row["max_loss"] == -300.0
    assert day["summary"]["win_rate"] == 100.0
    assert ledger.calendar_days()[0]["status"] == "green"


def test_no_trade_day_is_gray_and_protected() -> None:
    ledger = PaperLedger()
    ledger.register_day("2026-11-10", "RED")
    day = ledger.calendar_days()[0]
    assert day["status"] == "gray"
    assert day["capital_protected"] is True
    assert day["trades"] == 0


def test_missed_setup_is_deduplicated_across_status_updates() -> None:
    ledger = PaperLedger()
    setup = MissedSetup(
        date="2026-11-09", time_identified="2026-11-09T09:20:00",
        symbol="NIFTY", strike="NIFTY 25000 CE", option_type="CE",
        planned_entry=150.0, status="Watching", light="YELLOW",
        setup_id="NIFTY-25000-CE",
    )
    ledger.upsert_missed(setup)
    ledger.upsert_missed(MissedSetup(
        date="2026-11-09", time_identified="2026-11-09T09:20:00",
        symbol="NIFTY", strike="NIFTY 25000 CE", option_type="CE",
        planned_entry=151.0, status="Cancelled by Traffic Light",
        reason="RED", light="RED", setup_id="NIFTY-25000-CE",
    ))
    assert len(ledger.missed) == 1
    assert ledger.missed[0].status == "Cancelled by Traffic Light"
    assert ledger.missed[0].planned_entry == 151.0
    assert ledger.day_ledger("2026-11-09")["missed"][0]["status"].startswith("Cancelled")


def test_live_trade_mapping_keeps_realised_and_open_marks_separate() -> None:
    live = {
        "trade_id": "live-1", "status": "OPEN", "entry_price": 150.0,
        "initial_qty": 300, "qty_open": 150, "realized_pnl_rs": 2565.0,
        "live_pnl_rs": 3000.0, "target_1": 167.1, "target_2_p90": 180.4,
        "current_trailing_sl": 151.5, "entry_at": "2026-11-09T09:45:00",
        "mfe_price": 180.4, "mae_price": 149.0,
        "booked_legs": [{"leg": "T1", "qty": 150, "price": 167.1,
                         "pnl": 2565.0, "at": "2026-11-09T10:05:00"}],
    }
    row = paper_trade_from_live(live, date="2026-11-09", symbol="NIFTY")
    assert row.status == "OPEN"
    assert row.net == 2565.0          # banked T1, not the live mark
    assert row.t1_qty == 150
    assert row.qty_open == 150
    serialized = PaperLedger().serialize_trade(row)
    assert serialized["t1"]["qty"] == 150


def test_upsert_does_not_double_count_capital() -> None:
    ledger = PaperLedger()
    open_row = _trade(status="OPEN", exit=0.0, points=0.0, gross=0.0, net=0.0,
                      vaulted=0.0, outcome="OPEN", exit_at="", t1_qty=0,
                      t1_price=0.0, t1_pnl=0.0, sl_price=0.0, sl_pnl=0.0,
                      booked_legs=[])
    ledger.upsert_trade(open_row)
    closed = _trade()
    ledger.upsert_trade(closed)
    assert len(ledger.trades) == 1
    assert ledger.capital == 100_225.0
    assert ledger.vault == 157.5


def test_snapshot_keeps_day_detail_for_each_date() -> None:
    ledger = PaperLedger()
    ledger.record(_trade())
    snapshot = ledger.snapshot()
    assert snapshot["days_detail"]["2026-11-09"]["trades"][0]["trade_id"] == "t-1"
    assert snapshot["calendar"][0]["status"] == "green"


def test_old_flat_ledger_json_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "capital": 100_000.0, "vault": 0.0,
        "trades": [{
            "date": "2026-11-08", "light": "GREEN", "symbol": "NIFTY",
            "strike": "NIFTY 24950 CE", "option_type": "CE", "qty": 300,
            "entry": 150.0, "exit": 160.0, "points": 10.0, "gross": 3000.0,
            "costs": 40.0, "net": 2960.0, "vaulted": 2072.0, "outcome": "WIN",
            "entry_at": "2026-11-08T10:00:00", "exit_at": "2026-11-08T11:00:00",
        }],
        "days": {},
    }), encoding="utf-8")
    loaded = PaperLedger.load(path)
    assert len(loaded.trades) == 1
    assert loaded.trades[0].trade_id
    assert loaded.calendar_days()[0]["net"] == 2960.0
