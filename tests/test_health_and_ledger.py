"""Upstox health, Telugu narration and the paper ledger."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from market.upstox_health import HealthInputs, authorize_url, five_checks  # noqa: E402
from state.paper_trade_ledger import PaperLedger, PaperTrade  # noqa: E402
from state.telugu_narration_logger import TeluguNarrationLogger  # noqa: E402


# -- health ----------------------------------------------------------------- #
def test_authorize_url_carries_client_and_redirect() -> None:
    url = authorize_url("abc-123", "http://localhost:8080/upstox/callback")
    assert "client_id=abc-123" in url
    assert "response_type=code" in url
    assert "localhost%3A8080" in url


def test_all_five_indicators_green_when_fully_synced() -> None:
    result = five_checks(HealthInputs(
        token_present=True, token_valid=True, feed_connected=True, ticks_per_sec=12.4,
        latency_ms=38, buffer_5m={"NIFTY": 512, "BANKNIFTY": 512}, capacity=512,
        option_chain_ready=True, option_strikes=["24950 CE", "25050 PE"],
        model_online=True, capital=100_000,
    ))
    assert [check["key"] for check in result["checks"]] == ["auth", "feed", "buffers", "chain", "model"]
    assert all(check["status"] == "GREEN" for check in result["checks"])
    assert result["overall"] == "GREEN"
    assert "tick" in result["checks"][1]["detail"]


def test_token_without_feed_is_yellow_replay_mode() -> None:
    result = five_checks(HealthInputs(token_present=True, token_valid=False, buffer_5m={}))
    assert result["checks"][0]["status"] == "YELLOW"
    assert "REPLAY" in result["checks"][0]["detail"]
    assert result["checks"][1]["status"] == "RED"
    assert result["checks"][2]["status"] == "RED"
    assert result["overall"] == "RED"


def test_warming_buffer_is_yellow() -> None:
    result = five_checks(HealthInputs(buffer_5m={"NIFTY": 120, "BANKNIFTY": 90}))
    assert result["checks"][2]["status"] == "YELLOW"
    assert "120/512" in result["checks"][2]["detail"]


# -- telugu narration ------------------------------------------------------- #
def test_lifecycle_narration_has_all_five_events() -> None:
    logger = TeluguNarrationLogger()
    logger.sync_confirmation(spot=24_986.4, strike="NIFTY 25000 CE", option_type="CE",
                             option_ltp=150.0, option_atp=148.5, oi_change_pct=-7.2,
                             bid=149.95, ask=150.05, paths_up=88, samples=100)
    logger.order_placed(strike="NIFTY 25000 CE", option_type="CE", qty=300, lots=4,
                        entry=150.0, stop=140.5, target1=167.1, target_p90=180.4)
    logger.sl_breakeven(entry=150.0, old_stop=140.5, new_stop=151.5, ltp=158.0, qty=300)
    logger.partial_profit(entry=150.0, exit_price=169.0, qty_booked=150, remaining=150,
                          locked=2_850.0)
    logger.structure_trail(old_stop=151.5, new_stop=163.4, ltp=171.2)
    logger.final_booked(entry=150.0, exit_price=178.0, qty=300, reason="p90 reversed",
                        gross=8_400.0, costs=120.0, net=8_280.0, vaulted=5_796.0, gear=4)

    kinds = [event.kind for event in logger.events]
    assert kinds == ["sync", "order", "trail", "partial", "trail", "final"]
    assert "\u20b9150.00" in logger.events[1].text
    assert "Zero Risk Locked" in logger.events[2].text
    assert "Vault 70%" in logger.events[-1].text
    assert logger.events[-1].meta["net"] == 8_280.0


def test_narration_exports_dashboard_shape() -> None:
    logger = TeluguNarrationLogger()
    logger.order_placed(strike="NIFTY 25000 CE", option_type="CE", qty=300, lots=4,
                        entry=150.0, stop=140.5, target1=167.1, target_p90=180.4)
    logs = logger.as_logs()
    assert logs[0]["event"] == "order"
    assert logs[0]["kind"] == "order"
    assert set(logs[0]) == {"t", "kind", "event", "text", "meta"}


# -- ledger ----------------------------------------------------------------- #
def _trade(date: str, net: float, **overrides) -> PaperTrade:
    base = dict(date=date, light="GREEN", symbol="NIFTY", strike="NIFTY 25000 CE",
                option_type="CE", qty=300, entry=150.0, exit=160.0, points=10.0,
                gross=net, costs=40.0, net=net, vaulted=max(net, 0) * 0.7)
    base.update(overrides)
    return PaperTrade(**base)


def test_ledger_starts_at_one_lakh() -> None:
    ledger = PaperLedger()
    assert ledger.capital == 100_000
    assert ledger.snapshot()["rows"] == []


def test_day_aggregates_and_running_balance(tmp_path: Path) -> None:
    ledger = PaperLedger()
    ledger.record(_trade("2026-11-02", 2_000))
    ledger.record(_trade("2026-11-02", -500, strike="NIFTY 24950 CE", outcome="SL"))
    ledger.record(_trade("2026-11-03", 3_000, light="YELLOW"))

    rows = ledger.week_table()
    assert [row["date"] for row in rows] == ["2026-11-02", "2026-11-03"]
    assert rows[0]["trades"] == 2 and rows[0]["wins"] == 1 and rows[0]["sls"] == 1
    assert rows[0]["net"] == 1_500
    assert rows[0]["balance"] == 101_500
    assert rows[1]["balance"] == 104_500
    assert ledger.vault == round(2_000 * 0.7 + 3_000 * 0.7, 2)


def test_ledger_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = PaperLedger()
    ledger.register_day("2026-11-04", "RED")
    ledger.record(_trade("2026-11-04", 0.0, light="RED"))
    ledger.save(path)

    loaded = PaperLedger.load(path)
    assert loaded.capital == 100_000
    assert len(loaded.trades) == 1
    assert loaded.week_table()[0]["light"] == "RED"

    loaded.reset(path)
    assert PaperLedger.load(path).trades == []


def test_table_render_contains_all_days() -> None:
    ledger = PaperLedger()
    for index, date in enumerate(["2026-11-02", "2026-11-03", "2026-11-04"], start=1):
        ledger.record(_trade(date, 1_000 * index))
    table = ledger.render_table()
    assert "2026-11-02" in table and "2026-11-04" in table
    assert "TOTAL" in table
    assert "Super-Trend" in table
