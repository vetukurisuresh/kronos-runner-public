"""Story-Mode narration: phases, plain-language trade stories, SSE passthrough."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from market import live_ticks
from state.telugu_story import StoryTeller, phase_for

IST = ZoneInfo("Asia/Kolkata")


def test_phase_for_market_blocks():
    assert phase_for(datetime(2026, 11, 9, 9, 20, tzinfo=IST))[0] == "open"
    assert phase_for(datetime(2026, 11, 9, 10, 30, tzinfo=IST))[0] == "trend"
    assert phase_for(datetime(2026, 11, 9, 13, 0, tzinfo=IST))[0] == "midday"
    assert phase_for(datetime(2026, 11, 9, 14, 30, tzinfo=IST))[0] == "power"
    assert phase_for(datetime(2026, 11, 9, 8, 0, tzinfo=IST))[0] == "preopen"


def test_opening_story_is_human_telugu():
    teller = StoryTeller(clock=lambda: datetime(2026, 11, 9, 9, 16, tzinfo=IST))
    entry = teller.opening(spot=24900.5, support=24830, resistance=24980,
                           paths_up=4, samples=6, side="CE")
    assert "9:15" in entry["text"]
    assert "24,900.50" in entry["text"]
    assert "మార్కెట్" in entry["text"]
    assert entry["phase_label"].startswith("\U0001F305")


def test_trade_story_explains_why_in_two_sentences():
    teller = StoryTeller(clock=lambda: datetime(2026, 11, 9, 11, 5, tzinfo=IST))
    entry = teller.trade_taken(symbol="NIFTY 25000 CE", entry=58.55, stop=48.55,
                               target1=73.55, target2=88.55, risk=10.0,
                               paths_up=5, samples=6, heavy=4, side="CE", qty=300)
    assert "NIFTY 25000 CE" in entry["text"]
    assert "కొన్నాం" in entry["text"]
    assert "58.55" in entry["text"]
    assert "300 Qty" in entry["text"]
    assert entry["text"].count(".") >= 2
    assert entry["kind"] == "trade"


def test_partial_booking_story_names_the_quantity_and_lock():
    teller = StoryTeller(clock=lambda: datetime(2026, 11, 9, 11, 30, tzinfo=IST))
    entry = teller.partial_booked(name="T1", qty=150, price=73.55, pnl=2250,
                                  remaining=150, stop=58.55)
    assert "T1" in entry["text"] and "150 Qty" in entry["text"]
    assert "2,250" in entry["text"]
    assert "బ్రేక్‌ఈవెన్" in entry["text"]
    assert entry["kind"] == "win"


def test_story_keeps_only_the_last_twenty():
    teller = StoryTeller(limit=20, clock=lambda: datetime(2026, 11, 9, 12, 0, tzinfo=IST))
    for i in range(30):
        teller.waiting(spot=24900 + i, support=24800, resistance=25000,
                       reason=f"test {i}", paths_up=3, samples=6, side="CE")
    snapshot = teller.snapshot()
    assert len(snapshot) == 20
    assert snapshot[-1]["text"].find("test 29") >= 0
    assert snapshot[0]["text"].find("test 10") >= 0


def test_tick_snapshot_carries_the_story(tmp_path, monkeypatch):
    feed = tmp_path / "live_feed.json"
    feed.write_text(json.dumps({
        "updated_epoch": 1_800_000_000,
        "prices": {"NSE_INDEX|Nifty 50": 24930.0},
        "spot": {"NIFTY": {"ltp": 24930.0, "change": 0, "pct_change": 0}},
        "story": [{"t": "09:15", "text": "మార్కెట్ ఓపెన్", "emoji": "🌅"}],
        "story_at": 1_800_000_000,
    }), encoding="utf-8")
    monkeypatch.setattr(live_ticks, "FEED_FILE", feed)
    payload = live_ticks.snapshot("NIFTY", now=1_800_000_002)
    assert payload["story"][0]["text"] == "మార్కెట్ ఓపెన్"
    assert payload["story_at"] == 1_800_000_000
