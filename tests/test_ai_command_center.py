from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import dashboard.server as server_module
from market.institutional_footprint_tracker import InstitutionalFootprintTracker
from market.single_pass_sync_verifier import SinglePassSyncVerifier, verify_feed
from dashboard.command_center import CommandCenterService
from state.firebase_log_archiver import FirebaseLogArchiver
from state.telugu_narration_logger import SplitTeluguNarrationLogger
from strategy.human_vs_kronos_analyzer import (
    build_accuracy_snapshot,
    calculate_lead_gap,
)

IST = timezone(timedelta(hours=5, minutes=30))


def live_feed(now: float | None = None) -> dict:
    moment = now or 1_800_000_000.0
    return {
        "updated_epoch": moment,
        "updated_at": datetime.fromtimestamp(moment, tz=IST).isoformat(),
        "market_open": True,
        "socket_connected": True,
        "socket_frames": 8,
        "tick_rate": 6.4,
        "spot": {
            "NIFTY": {
                "ltp": 24950.0,
                "candles_1m": [{"close": 24950.0}],
                "candles_5m": [{"close": 24950.0}],
                "current_candle": {"low": 24940.0, "high": 24960.0},
            }
        },
        "full_spectrum": {
            "NIFTY": {
                "spot": 24950.0,
                "futures": {"ltp": 24985.0, "vtt_ratio": 3.1},
                "pcr": {"strike_count": 11, "pcr": 1.2},
                "depth": {"obi_5": 1.82, "bid_qty_5": 1000, "ask_qty_5": 800},
                "greeks": {"delta": 0.54, "gamma": 0.0018, "theta": -3.2, "vega": 11.0, "iv": 13.8},
                "vix": {"ltp": 12.8},
                "breadth": {"total": 5, "aligned": 4},
                "option": {"atr": 2.4},
            }
        },
        "sync_proof": {
            "model_counters": {
                "frames_read_by_102m": 8,
                "frames_read_by_25m": 8,
                "frames_read_by_4m": 8,
            },
            "single_pass_ms": 0.24,
        },
    }


def test_sync_verifier_passes_only_with_all_8_streams_and_3_model_reads() -> None:
    now = 1_800_000_000.0
    proof = verify_feed(live_feed(now), now=now + 0.2)
    assert proof["live_status"] == "LIVE_MARKET_VERIFIED"
    assert proof["data_gate"]["allowed"] is True
    assert proof["streams_live"] == 8
    assert proof["frames_read_by_102m"] == 8
    assert proof["frames_read_by_25m"] == 8
    assert proof["frames_read_by_4m"] == 8
    assert proof["data_gate"]["zero_dummy"] is True

    stale = verify_feed(live_feed(now), now=now + 10.0)
    assert stale["live_status"] == "OFFLINE_OR_STALE_FEED"
    assert stale["data_gate"]["allowed"] is False


def test_single_pass_decodes_once_and_credits_each_model_once() -> None:
    now = 1_800_000_000.0
    reads = {"102m": 0, "25m": 0, "4m": 0}
    verifier = SinglePassSyncVerifier(now=lambda: now)

    def reader(key):
        def consume(frame):
            reads[key] += 1
            assert frame.frame_id == 1
            assert len(frame.ticks) == 0
        return consume

    proof = verifier.consume_frame(
        {"feeds": {}},
        feed=live_feed(now),
        readers={key: reader(key) for key in reads},
        now=now,
    )
    assert reads == {"102m": 1, "25m": 1, "4m": 1}
    assert verifier.counters == {
        "frames_read_by_102m": 1,
        "frames_read_by_25m": 1,
        "frames_read_by_4m": 1,
    }
    assert proof["single_pass"]["frame_id"] == 1
    assert proof["single_pass"]["under_budget"] is True


def test_firebase_archiver_never_blocks_and_keeps_local_fallback(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def flaky_push(path: str, record: dict) -> None:
        calls.append((path, record))
        if len(calls) <= 2:
            raise RuntimeError("temporary Firebase outage")

    archive = FirebaseLogArchiver(root=tmp_path, firebase_push=flaky_push, retries=4)
    result = archive.enqueue(
        "scalper_logs", {"text": "⚡ verified scalp print", "kind": "entry"},
        date="2026-11-11", ts="2026-11-11T09:20:01+05:30",
    )
    assert result["queued"] is True
    assert result["enqueue_ms"] < 0.5
    assert archive.flush(timeout=5.0) is True
    archive.close()

    path = tmp_path / "2026-11-11" / "scalper_logs.jsonl"
    assert path.is_file()
    rows = archive.load_day("2026-11-11")["scalper_logs"]
    assert rows[0]["text"] == "⚡ verified scalp print"
    assert calls[-1][0] == "/kronos_archive/2026-11-11/scalper_logs"
    assert archive.snapshot()["firebase_ok"] == 1
    assert archive.snapshot()["local_fallback"] == 0


def test_human_vs_kronos_lead_gap_and_accuracy() -> None:
    gap = calculate_lead_gap(
        signal_time="2026-11-11T09:41:17+05:30",
        candle_close_time="2026-11-11T09:42:00+05:30",
        signal_price=74.20,
        human_price=80.60,
        side="CE",
        obi_5=1.78,
        tbq_tsq=1.31,
    )
    assert gap["seconds_ahead"] == 46.5
    assert gap["lead_time_seconds_ahead_of_human"] == 46.5
    assert gap["price_advantage_inr"] == 6.4
    assert "46" in gap["human_failure_reason_te"]
    assert "6.40" in gap["human_failure_reason_te"]

    summary = build_accuracy_snapshot([
        {"outcome": "win", "pnl": 1200, "lead_time_seconds_ahead": 40},
        {"outcome": "loss", "pnl": -500, "lead_time_seconds_ahead": 42},
        {"outcome": "early_exit", "pnl": 300, "lead_time_seconds_ahead": 38},
        {"outcome": "blocked_trap", "trap_confirmed": True, "lead_time_seconds_ahead": 44},
        {"outcome": "open", "pnl": 0},
    ])
    assert summary["total_decisions"] == 5
    assert summary["scored_decisions"] == 4
    assert summary["trade_win_rate_pct"] == 50.0
    assert summary["decision_accuracy_pct"] == 75.0
    assert summary["avg_lead_time_sec"] == 41.0
    assert summary["correctly_blocked_traps"] == 1


def test_whale_tracker_has_13_windows_and_exact_footprint() -> None:
    tracker = InstitutionalFootprintTracker()
    ts = datetime(2026, 11, 11, 9, 50, tzinfo=IST)
    tracker.add_tick(
        24945.0, 100, ts=ts, futures_ratio=3.1, obi_5=1.82,
        tbq_tsq=1.4, oi_delta=420000,
    )
    tracker.add_tick(
        24952.0, 10, ts=ts + timedelta(seconds=2), futures_ratio=1.2,
        obi_5=0.9, oi_delta=-1000,
    )
    windows = tracker.snapshot("2026-11-11", now=ts + timedelta(minutes=5))
    assert len(windows) == 13
    active = windows[1]
    assert active["window"] == "09:45–10:15"
    assert active["whale_zone"]["price_low"] == 24945
    assert active["whale_zone"]["futures_volume_ratio"] == 3.1
    assert "పెద్ద ప్లేయర్ల" in active["whale_zone"]["text_te"]
    assert windows[0]["whale_zone"] is None
    assert windows[-1]["window"] == "15:15–15:30"
    assert windows[-1]["end_ist"] == "15:30"


def test_three_split_telugu_streams_are_independent() -> None:
    archived: list[tuple[str, dict]] = []

    class FakeArchive:
        def enqueue(self, scope, payload, **kwargs):
            archived.append((scope, dict(payload)))

    logger = SplitTeluguNarrationLogger(archiver=FakeArchive())
    logger.record_scalp(spot=24950, spread=0.4, obi_5=1.78, trigger="OBI EDGE", entry=74.2,
                        stop=70, target=81)
    logger.record_intraday(spot=24950, zone="bullish order block", p10=24920,
                           p90=25010, side="CE", trigger="5/5 reversal")
    logger.record_model(model="Kronos-base (102M)", thought_te="100 దారులలో 82 పైకి",
                        decision="BUY_CE", lead_seconds=43.0, gate_passed=True)
    snapshot = logger.snapshot()
    assert len(snapshot["scalper"]) == 1
    assert len(snapshot["intraday"]) == 1
    assert len(snapshot["model"]) == 1
    assert [scope for scope, _ in archived] == [
        "scalper_logs", "intraday_sniper_logs", "model_thoughts",
    ]


class _FakeCommandCenter:
    def live(self, symbol: str) -> dict:
        return {"ok": True, "date": "2026-11-11", "sync": {"live_status": "LIVE_MARKET_VERIFIED"},
                "accuracy": {"total_decisions": 0}, "logs": {}, "whale_windows": []}

    def history(self, date: str) -> dict:
        return {"ok": True, "date": date, "source": "no_archive_for_date",
                "logs": {}, "whale_windows": []}

    def download(self, date: str, scope: str = "all", fmt: str = "txt") -> dict:
        return {"body": b"KRONOS command center\n", "filename": f"logs_{date}_{scope}.{fmt}",
                "content_type": "text/plain; charset=utf-8"}


def test_command_center_history_and_download_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(server_module, "COMMAND_CENTER", _FakeCommandCenter())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        with urlopen(base + "/api/command-center/live?symbol=NIFTY", timeout=5) as response:
            live = json.loads(response.read())
        with urlopen(base + "/api/command-center/history?date=2026-11-11", timeout=5) as response:
            history = json.loads(response.read())
        with urlopen(
            base + "/api/command-center/download-logs?date=2026-11-11&scope=all&format=txt",
            timeout=5,
        ) as response:
            body = response.read()
            disposition = response.headers.get("Content-Disposition", "")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    assert live["ok"] is True
    assert history["date"] == "2026-11-11"
    assert body == b"KRONOS command center\n"
    assert "attachment" in disposition


def test_command_center_history_and_downloads_read_the_disk_archive(tmp_path: Path) -> None:
    day = tmp_path / "2026-11-11"
    day.mkdir()
    rows = {
        "scalper_logs": [{
            "ts": "2026-11-11T09:20:01+05:30", "kind": "entry",
            "text": "⚡ verified scalp print", "scope": "scalper_logs",
        }],
        "intraday_sniper_logs": [{
            "ts": "2026-11-11T09:45:01+05:30", "kind": "setup",
            "text": "🎯 verified SMC setup", "scope": "intraday_sniper_logs",
        }],
        "model_thoughts": [{
            "ts": "2026-11-11T09:45:02+05:30", "kind": "decision",
            "text": "🧠 verified model thought", "scope": "model_thoughts",
        }],
        "whale_30m_footprints": [{
            "ts": "2026-11-11T09:45:00+05:30", "window": "09:45–10:15",
            "metrics": {"tick_count": 12, "last_price": 24950.0},
            "whale_zone": {"text_te": "🐋 verified whale zone", "bias": "BULLISH"},
        }],
        "accuracy_summary": [{
            "ts": "2026-11-11T15:30:00+05:30", "total_decisions": 4,
            "scored_decisions": 4, "winning_trades": 3, "losing_trades": 1,
            "trade_win_rate_pct": 75.0, "decision_accuracy_pct": 75.0,
            "avg_lead_time_sec": 41.5, "correctly_blocked_traps": 0,
        }],
    }
    for scope, scope_rows in rows.items():
        (day / f"{scope}.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in scope_rows) + "\n",
            encoding="utf-8",
        )

    service = CommandCenterService(
        feed_path=tmp_path / "missing-live-feed.json",
        archive_root=tmp_path / "archive",
        clock=lambda: 1_800_000_000.0,
    )
    # Point the archive at the fixture day created above.
    service.archive.root = tmp_path
    history = service.history("2026-11-11")
    assert history["source"] == "disk_archive"
    assert len(history["logs"]["scalper"]) == 1
    assert len(history["logs"]["intraday"]) == 1
    assert len(history["logs"]["model"]) == 1
    assert history["whale_windows"][0]["whale_zone"]["text_te"].startswith("🐋")
    assert history["accuracy"]["decision_accuracy_pct"] == 75.0

    for scope in ("all", "scalper", "intraday", "whale"):
        for fmt in ("txt", "jsonl", "csv"):
            out = service.download("2026-11-11", scope=scope, fmt=fmt)
            assert out["rows"] > 0
            assert out["body"]
            assert out["filename"] == f"kronos_command_center_2026-11-11_{scope}.{fmt}"
    assert json.loads(service.download("2026-11-11", "whale", "jsonl")["body"].splitlines()[0])["scope"] == "whale"
