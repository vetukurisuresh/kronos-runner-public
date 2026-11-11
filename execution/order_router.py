"""Paper vs. real trading router.

The router is the only place that chooses a venue. Paper mode is the default and
never reaches Upstox. Real mode requires an explicit ``submit`` confirmation,
a saved access token, and a successful live margin read; the mode is persisted so
a refresh cannot silently switch the desk back to live.

The actual broker adapter lives in :mod:`execution.upstox_live_broker`; this
module also exposes the paper close primitive used by ``/api/trade/close-now``.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from execution.pro_money_manager import close_remaining
from execution.trading_cutoff import require_new_entry
from execution.upstox_live_broker import UpstoxBrokerError, UpstoxLiveBroker

REPO_ROOT = Path(__file__).resolve().parents[1]
MODE_FILE = REPO_ROOT / "state" / "trade_mode.json"
IST = timezone(timedelta(hours=5, minutes=30))
PAPER = "PAPER"
REAL = "REAL"
VALID_MODES = {PAPER, REAL}
MANUAL_CLOSE_REASON = "MANUAL_SMART_CLOSE_AT_LIVE"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    temp.replace(path)


class ModeRejected(RuntimeError):
    """Raised when a mode switch or venue order is not allowed."""


class OrderRouter:
    def __init__(
        self,
        *,
        mode_path: Path = MODE_FILE,
        broker: UpstoxLiveBroker | None = None,
        broker_factory=None,
    ) -> None:
        self.mode_path = mode_path
        self._broker = broker
        self._broker_factory = broker_factory or UpstoxLiveBroker

    @property
    def broker(self) -> UpstoxLiveBroker:
        if self._broker is None:
            self._broker = self._broker_factory()
        return self._broker

    # -- state -------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        raw = _read_json(self.mode_path)
        mode = str(raw.get("mode") or PAPER).upper()
        if mode not in VALID_MODES:
            mode = PAPER
        return {
            "mode": mode,
            "confirmed": bool(raw.get("confirmed")) and mode == REAL,
            "available_margin": float(raw.get("available_margin") or 0.0),
            "used_margin": float(raw.get("used_margin") or 0.0),
            "confirmed_at": raw.get("confirmed_at") or "",
            "paper_capital": float(raw.get("paper_capital") or 100_000.0),
        }

    def snapshot(
        self,
        *,
        paper_capital: float = 100_000.0,
        paper_realized: float = 0.0,
        include_broker: bool = True,
    ) -> dict[str, Any]:
        state = self.load()
        token_present = False
        if include_broker:
            try:
                token_present = bool(self.broker.token_present)
            except Exception:  # noqa: BLE001 - missing optional SDK/token is not fatal
                token_present = False
        real = state["mode"] == REAL and state["confirmed"]
        return {
            "ok": True,
            "mode": state["mode"],
            "confirmed": state["confirmed"],
            "real": real,
            "paper_mode": state["mode"] == PAPER,
            "token_present": token_present,
            "available_margin": round(state["available_margin"], 2) if real else 0.0,
            "used_margin": round(state["used_margin"], 2) if real else 0.0,
            "paper_capital": round(float(paper_capital), 2),
            "paper_realized": round(float(paper_realized), 2),
            "confirmed_at": state["confirmed_at"],
            "label": "REAL MONEY LIVE" if real else "PAPER TRADE",
            "badge": "REAL MONEY LIVE" if real else "PAPER TRADE",
            "submit_required": not real,
        }

    def set_mode(
        self,
        mode: str,
        *,
        confirm: bool = False,
        paper_capital: float = 100_000.0,
        paper_realized: float = 0.0,
    ) -> dict[str, Any]:
        desired = str(mode or "").upper()
        if desired not in VALID_MODES:
            raise ModeRejected("mode must be PAPER or REAL")
        if desired == PAPER:
            _write_json(
                self.mode_path,
                {
                    "mode": PAPER,
                    "confirmed": False,
                    "paper_capital": round(float(paper_capital), 2),
                    "paper_realized": round(float(paper_realized), 2),
                    "switched_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
                },
            )
            return self.snapshot(paper_capital=paper_capital, paper_realized=paper_realized)

        if not confirm:
            raise ModeRejected("REAL mode requires the explicit Submit Mode confirmation")
        try:
            margin = self.broker.funds_and_margin("SEC")
        except (UpstoxBrokerError, Exception) as exc:  # noqa: BLE001 - surface safe reason
            raise ModeRejected(f"Upstox margin check failed: {exc}") from exc
        available = float(margin.get("available_margin") or 0.0)
        used = float(margin.get("used_margin") or 0.0)
        if available <= 0:
            raise ModeRejected("Upstox reports no available SEC margin; REAL mode stays disabled")
        _write_json(
            self.mode_path,
            {
                "mode": REAL,
                "confirmed": True,
                "available_margin": available,
                "used_margin": used,
                "paper_capital": round(float(paper_capital), 2),
                "paper_realized": round(float(paper_realized), 2),
                "confirmed_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
            },
        )
        return self.snapshot(paper_capital=paper_capital, paper_realized=paper_realized)

    def require_real(self) -> dict[str, Any]:
        state = self.load()
        if state["mode"] != REAL or not state["confirmed"]:
            raise ModeRejected("REAL mode is not confirmed; use Submit Mode first")
        return state

    # -- orders ------------------------------------------------------------
    def place_entry(
        self,
        *,
        trade: dict[str, Any],
        price: float,
        quantity: int | None = None,
        side: str = "BUY",
        order_type: str = "LIMIT",
        tag: str = "KRONOS-ENTRY",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        # This method is an entry path, whether the venue is paper or real.
        # The close paths below deliberately do not call this guard.
        require_new_entry(now)
        state = self.load()
        qty = int(quantity or trade.get("qty_open") or trade.get("initial_qty") or trade.get("qty") or 0)
        if qty <= 0:
            raise ModeRejected("entry quantity is zero")
        if state["mode"] == PAPER:
            return {
                "ok": True,
                "mode": PAPER,
                "filled_price": round(float(price), 2),
                "quantity": qty,
                "order_id": "",
            }
        self.require_real()
        instrument = str(trade.get("instrument_key") or trade.get("instrument_token") or "")
        if not instrument:
            raise ModeRejected("REAL entry needs the exact NSE_FO instrument_key")
        return {
            "ok": True,
            "mode": REAL,
            **self.broker.place_order(
                instrument_key=instrument,
                quantity=qty,
                transaction_type=side,
                order_type="LIMIT",
                price=float(price),
                tag=tag,
            ),
        }

    def place_exit(
        self,
        *,
        instrument_key: str,
        quantity: int,
        price: float | None = None,
        order_type: str = "MARKET",
        tag: str = "KRONOS-EXIT",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        # Exit-only after 15:00 is intentional: protective sells stay legal.
        del now
        state = self.load()
        qty = int(quantity)
        if qty <= 0:
            raise ModeRejected("exit quantity is zero")
        if state["mode"] == PAPER:
            return {"ok": True, "mode": PAPER, "filled_price": round(float(price or 0.0), 2), "quantity": qty, "order_id": ""}
        self.require_real()
        return {
            "ok": True,
            "mode": REAL,
            **self.broker.place_order(
                instrument_key=instrument_key,
                quantity=qty,
                transaction_type="SELL",
                order_type=order_type,
                price=price,
                tag=tag,
            ),
        }

    def modify_stop(self, order_id: str, *, quantity: int, trigger_price: float) -> dict[str, Any]:
        self.require_real()
        return {"ok": True, **self.broker.modify_order(
            order_id,
            quantity=quantity,
            order_type="SL-M",
            trigger_price=trigger_price,
        )}

    def close_paper_at_live(self, trade: dict[str, Any], ltp: float) -> dict[str, Any]:
        """Close the remaining paper quantity at the current live premium."""
        leg = close_remaining(trade, float(ltp), reason=MANUAL_CLOSE_REASON)
        if not leg:
            raise ModeRejected("no open paper quantity to close")
        return {
            "ok": True,
            "mode": PAPER,
            "leg": leg,
            "reason": MANUAL_CLOSE_REASON,
            "fill_price": float(leg.get("price") or ltp),
            "quantity": int(leg.get("qty") or 0),
            "pnl": float(leg.get("pnl") or 0.0),
        }

    def close_live_at_market(self, trade: dict[str, Any]) -> dict[str, Any]:
        """Submit the live market SELL for the remaining quantity."""
        self.require_real()
        instrument = str(trade.get("instrument_key") or "")
        qty = int(trade.get("qty_open") or 0)
        if not instrument:
            raise ModeRejected("REAL close needs the exact NSE_FO instrument_key")
        if qty <= 0:
            raise ModeRejected("no open live quantity to close")
        result = self.broker.market_exit(instrument_key=instrument, quantity=qty)
        return {"ok": True, "mode": REAL, **result, "quantity": qty}

    def make_close_request(self, symbol: str, mode: str | None = None) -> dict[str, Any]:
        """Write the cross-process close request consumed by the live session."""
        state = self.load()
        request = {
            "request_id": uuid.uuid4().hex,
            "symbol": str(symbol or "NIFTY").upper(),
            "mode": str(mode or state["mode"]).upper(),
            "requested_at": round(time.time(), 3),
        }
        _write_json(REPO_ROOT / "state" / "live_close_request.json", request)
        return request
