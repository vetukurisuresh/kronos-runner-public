"""HTTP-level test for the one-click close endpoint."""

from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import dashboard.server as server_module  # noqa: E402
import state.paper_trade_ledger as ledger_module  # noqa: E402
from execution.order_router import OrderRouter  # noqa: E402


class _FakeLedger:
    def __init__(self) -> None:
        self.saved = None

    def upsert_trade(self, trade):
        self.saved = trade
        return self

    def save(self):
        return True


def test_close_now_http_closes_stale_paper_feed(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    feed = {
        "updated_epoch": 0,
        "socket_connected": False,
        "trade": {},
        "option": {},
        "hud": {"starting_capital": 100000.0, "realized_today": 0.0,
                "capital": 100000.0, "trades_today": 1},
    }
    trade = {
        "status": "OPEN", "entry_price": 100.0, "qty": 300, "qty_open": 300,
        "initial_qty": 300, "realized_pnl_rs": 0.0, "booked_legs": [],
        "current_trailing_sl": 92.0, "target_1": 110.0, "target_2_p90": 120.0,
        "option_symbol": "NIFTY 25000 CE", "instrument_key": "NSE_FO|TEST",
    }
    feed["trade"]["NIFTY"] = trade
    feed["option"]["NIFTY"] = {"ltp": 112.5, "atp_vwap": 108.0}
    (state_dir / "live_feed.json").write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(server_module, "REPO_ROOT", tmp_path)

    router = OrderRouter(mode_path=tmp_path / "trade_mode.json")
    monkeypatch.setattr("execution.order_router.OrderRouter", lambda: router)

    fake_ledger = _FakeLedger()
    monkeypatch.setattr(
        ledger_module.PaperLedger, "load",
        classmethod(lambda cls: fake_ledger),
    )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"symbol": "NIFTY", "mode": "PAPER"}).encode()
        request = Request(
            f"http://127.0.0.1:{httpd.server_port}/api/trade/close-now",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    assert result["ok"] is True
    assert result["fill_price"] == 112.5
    assert result["pnl"] == 3750.0
    assert result["feed_updated"] is True
    saved_feed = json.loads((state_dir / "live_feed.json").read_text(encoding="utf-8"))
    assert saved_feed["trade"]["NIFTY"]["status"] == "CLOSED"
    assert saved_feed["trade"]["NIFTY"]["exit_reason"] == "MANUAL_SMART_CLOSE_AT_LIVE"
    assert saved_feed["hud"]["realized_today"] == 3750.0
    assert fake_ledger.saved is not None
