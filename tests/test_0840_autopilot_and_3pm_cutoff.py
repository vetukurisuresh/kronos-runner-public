"""Acceptance tests for the 08:40 autopilot and the 15:00 IST entry lock."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from execution.order_router import OrderRouter
from execution.trading_cutoff import (
    AFTER_3PM_ENTRY_CUTOFF,
    AFTER_3PM_UI_BADGE,
    NewEntryBlocked,
    cutoff_state,
)
from market.autopilot_readiness import (
    CHECK_LABELS,
    build_checks,
    build_from_runtime,
)
from scripts.check_0840_readiness import READY_STATUS, validate_readiness
from scripts.one_time_autopilot_setup import valid_totp_secret

IST = timezone(timedelta(hours=5, minutes=30))


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 10, hour, minute, second, tzinfo=IST)


def test_workflow_cron_is_before_0840_ist():
    auth = (REPO_ROOT / ".github/workflows/01-morning-auth.yml").read_text(encoding="utf-8")
    edge = (REPO_ROOT / ".github/workflows/02-live-market-edge.yml").read_text(encoding="utf-8")
    smoke = (REPO_ROOT / ".github/workflows/03-readiness-smoke.yml").read_text(encoding="utf-8")

    # 08:25 IST is 02:55 UTC and 08:32 IST is 03:02 UTC.  The request's
    # ``29 2``/``02 3`` pair would not be 08:25/08:32 IST; these are corrected.
    assert 'cron: "55 2 * * 1-5"' in auth
    assert 'cron: "02 3 * * 1-5"' in edge
    # Shift 2 starts 12:35 IST (07:05 UTC) for a 7-minute overlap.
    assert 'cron: "05 7 * * 1-5"' in edge
    assert "READY_0840_ALL_SYSTEMS_GO" not in auth  # auth job must not claim readers
    assert "autopilot_readiness.json" in auth
    assert "scripts/upstox_live_session.py --edge" in edge
    assert 'cron: "11 3 * * 1-5"' in smoke
    assert "scripts/check_0840_readiness.py" in smoke


def test_totp_secret_validation_rejects_otp_codes():
    assert valid_totp_secret("JBSWY3DPEHPK3PXP") is True
    assert valid_totp_secret("885163") is False
    assert valid_totp_secret("123456") is False
    assert valid_totp_secret("") is False


def _ready_smoke_payload() -> dict:
    return {
        "checks": [
            {"key": key, "status": "GREEN", "dot": "✅"}
            for key in ("auth", "runner", "edge", "socket", "bars", "models")
        ],
        "green": 6,
        "all_green": True,
        "status": READY_STATUS,
        "phase": "LIVE",
        "ready_at_ist": "2026-11-10T08:39:59+05:30",
        "ready_by_0840": True,
    }


def test_smoke_passes_only_for_complete_on_time_six_green_record():
    ok, errors = validate_readiness(_ready_smoke_payload(), now=_at(8, 41))
    assert ok is True
    assert errors == []

    early = _ready_smoke_payload()
    early["phase"] = "SYNCING"
    early["ready_at_ist"] = "2026-11-10T08:39:30+05:30"
    ok, errors = validate_readiness(early, now=_at(8, 41))
    assert ok is True
    assert errors == []

    late = _ready_smoke_payload()
    late["ready_at_ist"] = "2026-11-10T08:40:01+05:30"
    ok, errors = validate_readiness(late, now=_at(8, 41))
    assert ok is False
    assert any("08:40:00" in error for error in errors)

    incomplete = _ready_smoke_payload()
    incomplete["checks"] = incomplete["checks"][:5]
    incomplete["green"] = 5
    ok, errors = validate_readiness(incomplete, now=_at(8, 41))
    assert ok is False
    assert any("six readiness checks" in error for error in errors)

    wrong_status = _ready_smoke_payload()
    wrong_status["status"] = "WARMING_0840"
    ok, errors = validate_readiness(wrong_status, now=_at(8, 41))
    assert ok is False
    assert any("READY_0840_ALL_SYSTEMS_GO" in error for error in errors)


def test_smoke_diagnostic_is_explicit_and_strict_remains_default(monkeypatch):
    from scripts import check_0840_readiness as smoke

    monkeypatch.setattr(smoke, "fetch_readiness", lambda *args, **kwargs: {})
    assert smoke.main(["--url", "https://example.invalid"]) == 1
    assert smoke.main(["--url", "https://example.invalid", "--diagnostic"]) == 0


def test_public_rules_allow_readiness_read_but_not_write():
    from state.firebase_sync import PUBLIC_RULES

    rule = PUBLIC_RULES["rules"]["system"]["premarket_readiness"]
    assert rule[".read"] is True
    assert rule[".write"] is False


def test_six_checks_are_green_only_with_all_real_facts():
    ready = build_checks(
        auth_ok=True,
        runner_ok=True,
        edge_ok=True,
        socket_ok=True,
        chain_ok=True,
        buffer_ok=True,
        models_ok=True,
        details={key: "verified" for key in ("auth", "runner", "edge", "socket", "bars", "models")},
        now=_at(8, 35),
    )
    assert ready["status"] == "READY_0840_ALL_SYSTEMS_GO"
    assert ready["green"] == 6 and ready["all_green"] is True
    assert [check["label"] for check in ready["checks"]] == list(CHECK_LABELS)
    assert all(check["dot"] == "✅" for check in ready["checks"])
    assert ready["ready_by_0840"] is True

    waiting = build_checks(
        auth_ok=True,
        runner_ok=True,
        edge_ok=False,
        socket_ok=True,
        chain_ok=True,
        buffer_ok=True,
        models_ok=True,
        now=_at(8, 35),
    )
    assert waiting["status"] == "WARMING_0840"
    assert waiting["checks"][2]["status"] == "YELLOW"
    assert waiting["checks"][2]["dot"] == "⏳"

    late = build_checks(
        auth_ok=True,
        runner_ok=True,
        edge_ok=False,
        socket_ok=True,
        chain_ok=True,
        buffer_ok=True,
        models_ok=True,
        now=_at(9, 0),
    )
    assert late["checks"][2]["status"] == "RED"
    assert late["all_green"] is False


def test_runtime_payload_uses_feed_and_readiness_not_optimism():
    feed = {
        "updated_epoch": _at(9, 0).timestamp(),
        "socket_connected": True,
        "tick_rate": 4.2,
        "option_chain": {
            "NIFTY": [{"strike_price": 25000 + i} for i in range(11)],
            "BANKNIFTY": [{"strike_price": 50000 + i} for i in range(11)],
        },
        "buffer_1m": {"NIFTY": 512, "BANKNIFTY": 512},
        "buffer_5m": {"NIFTY": 512, "BANKNIFTY": 512},
        "buffer_15m": {"NIFTY": 512, "BANKNIFTY": 512},
    }
    readiness = {
        "auth": {"minted": True, "verified": True, "minted_at_ist": "2026-11-10T08:27:00+05:30"},
        "runner": {"online": True, "heartbeat_epoch": _at(9, 0).timestamp()},
        "edge": {"active": True, "url": "https://example.trycloudflare.com",
                 "updated_epoch": _at(9, 0).timestamp()},
        "models": {
            "base": {"ready": True, "bars": 512},
            "small": {"ready": True, "bars": 512},
            "mini": {"ready": True, "bars": 512},
        },
    }
    payload = build_from_runtime(
        feed=feed, readiness=readiness, token_present=True, now=_at(9, 0),
    )
    assert payload["green"] == 6
    assert payload["status"] == "READY_0840_ALL_SYSTEMS_GO"
    assert "example.trycloudflare.com" in payload["checks"][2]["detail"]
    assert "4.2" in payload["checks"][3]["detail"]


def test_stale_feed_is_never_green():
    feed = {
        "updated_epoch": _at(9, 0).timestamp() - 36000,
        "socket_connected": True,
        "tick_rate": 6.7,
        "option_chain": {"NIFTY": [{}] * 11, "BANKNIFTY": [{}] * 11},
        "buffer_1m": {"NIFTY": 512, "BANKNIFTY": 512},
        "buffer_5m": {"NIFTY": 512, "BANKNIFTY": 512},
        "buffer_15m": {"NIFTY": 512, "BANKNIFTY": 512},
    }
    payload = build_from_runtime(feed=feed, readiness={}, now=_at(9, 0))
    assert payload["green"] == 0
    assert all(check["status"] == "RED" for check in payload["checks"])


def test_paper_and_real_entry_paths_block_at_and_after_1500(tmp_path):
    router = OrderRouter(mode_path=tmp_path / "trade_mode.json")
    trade = {"qty": 75, "qty_open": 75, "initial_qty": 75, "instrument_key": "NSE_FO|TEST"}

    # 14:59:59 remains a legal entry.
    allowed = router.place_entry(trade=trade, price=100.0, now=_at(14, 59, 59))
    assert allowed["ok"] is True

    for moment in (_at(15, 0, 0), _at(15, 0, 1), _at(15, 10), _at(15, 25)):
        with pytest.raises(NewEntryBlocked, match="03:00 PM CUTOFF"):
            router.place_entry(trade=trade, price=100.0, now=moment)

    # An already-open position may still be exited after the cutoff.
    exit_fill = router.place_exit(
        instrument_key="NSE_FO|TEST", quantity=75, price=101.5, now=_at(15, 10),
    )
    assert exit_fill["ok"] is True
    assert exit_fill["quantity"] == 75


def test_cutoff_state_has_exact_badge_and_square_off():
    assert cutoff_state(_at(14, 59, 59))["new_entries_allowed"] is True
    at_cutoff = cutoff_state(_at(15, 0, 0))
    assert at_cutoff["new_entries_allowed"] is False
    assert at_cutoff["exit_only"] is True
    assert at_cutoff["reason"] == AFTER_3PM_ENTRY_CUTOFF
    assert at_cutoff["badge"] == AFTER_3PM_UI_BADGE
    assert cutoff_state(_at(15, 17, 59))["square_off_due"] is False
    assert cutoff_state(_at(15, 18, 0))["square_off_due"] is True


def test_dashboard_has_six_check_bar_and_no_login_prompt():
    page = (REPO_ROOT / "dashboard/static/index.html").read_text(encoding="utf-8")
    assert 'id="autopilotBar"' in page
    assert 'id="apChecks"' in page
    assert 'id="connectBanner"' in page
    assert "UPSTOX AUTO-CONNECTED (08:40 AM READY)" in page
    assert 'id="cutoffBadge"' in page
    assert "Mobile 1-Tap" not in page
    assert 'id="connectBtn"' not in page
    assert AFTER_3PM_UI_BADGE.split(" — ")[0] in page


def test_setup_script_is_presence_only_and_never_prints_a_secret():
    source = (REPO_ROOT / "scripts/one_time_autopilot_setup.py").read_text(encoding="utf-8")
    assert "--sync-only" in source and "--onboard" in source
    assert "127.0.0.1" in source
    assert "gh secret set" not in source.split("subprocess.run", 1)[1][:200]  # uses argv, not a shell string
    assert "print(values" not in source
