"""Order manager: ATM selection, sandbox/live execution, hard safety gates.

Every order passes through the same guard chain before it can reach a broker:

1. kill switch (env flag, local flag file, or Firebase)
2. session window (09:15-15:30 IST)
3. regime light (RED means zero new trades)
4. per-light trade budget

Sandbox mode fills instantly at the requested price and writes a JSONL ledger.
Live mode is opt-in via ``KRONOS_LIVE=1`` and refuses to run without the SDK, so
a misconfigured runner can never send an unintended order.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import (
    LOG_DIR,
    Settings,
    get_settings,
    in_session_window,
    kill_switch_enabled,
    to_ist,
)
from strategy.market_regime_traffic import trades_remaining
from execution.trading_cutoff import NewEntryBlocked, require_new_entry

LEDGER_FILE = LOG_DIR / "orders.jsonl"


class OrderRejected(RuntimeError):
    """Raised when a safety gate blocks an order."""


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str  # BUY / SELL
    quantity: int
    price: float
    option_type: str = "CE"
    strike: int = 0
    tag: str = ""
    order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.upper(),
            "quantity": self.quantity,
            "price": round(self.price, 2),
            "option_type": self.option_type,
            "strike": self.strike,
            "tag": self.tag,
        }


@dataclass
class Fill:
    order: Order
    filled_price: float
    mode: str
    at: str


def select_atm(spot: float, strikes: list[int], option_type: str = "CE") -> int:
    """Nearest strike to spot; ties resolve to the lower strike."""
    if not strikes:
        raise ValueError("strike ladder is empty")
    option_type = option_type.upper()
    if option_type not in {"CE", "PE"}:
        raise ValueError("option_type must be CE or PE")
    return min(strikes, key=lambda strike: (abs(strike - spot), strike))


class OrderManager:
    def __init__(
        self,
        settings: Settings | None = None,
        ledger_path: Path = LEDGER_FILE,
        firebase=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ledger_path = ledger_path
        self.firebase = firebase
        self.fills: list[Fill] = []

    # -- gates -------------------------------------------------------------
    def _kill_switch(self) -> bool:
        if kill_switch_enabled():
            return True
        if self.firebase is not None:
            try:
                return bool(self.firebase.kill_switch_active())
            except Exception:
                return False
        return False

    def check_gates(
        self,
        *,
        now: datetime | None = None,
        regime_light: str = "GREEN",
        executed: int = 0,
        side: str = "BUY",
    ) -> None:
        if self._kill_switch():
            raise OrderRejected("kill switch is active")
        if not in_session_window(now):
            raise OrderRejected("outside the 09:15-15:30 IST session window")
        if str(side).upper() == "BUY":
            try:
                require_new_entry(now)
            except NewEntryBlocked as exc:
                raise OrderRejected(str(exc)) from exc
        if regime_light.upper() == "RED":
            raise OrderRejected("regime is RED - no new trades")
        if trades_remaining(regime_light, executed) <= 0:
            raise OrderRejected(f"trade budget exhausted for {regime_light.upper()}")

    # -- execution ---------------------------------------------------------
    def place(
        self,
        order: Order,
        *,
        now: datetime | None = None,
        regime_light: str = "GREEN",
        executed: int = 0,
    ) -> Fill:
        self.check_gates(now=now, regime_light=regime_light, executed=executed,
                         side=order.side)
        if order.quantity <= 0 or order.price <= 0:
            raise OrderRejected("quantity and price must be positive")

        mode = "live" if self.settings.live_trading else "sandbox"
        filled_price = order.price if mode == "sandbox" else self._place_live(order)
        fill = Fill(order=order, filled_price=filled_price, mode=mode, at=to_ist(now).isoformat())
        self.fills.append(fill)
        self._append_ledger(fill)
        return fill

    def _place_live(self, order: Order) -> float:  # pragma: no cover - needs broker
        try:
            import upstox_client  # noqa: F401
        except ImportError as exc:
            raise OrderRejected(
                "live mode requires `upstox-python-sdk` and a valid access token"
            ) from exc
        raise OrderRejected(
            "live order placement is intentionally not wired in this build; "
            "validate through the Upstox sandbox before enabling KRONOS_LIVE=1"
        )

    def _append_ledger(self, fill: Fill) -> None:
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            record = {**fill.order.as_dict(), "filled": fill.filled_price, "mode": fill.mode, "at": fill.at}
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def square_off_all(self) -> list[Fill]:
        exits: list[Fill] = []
        for fill in list(self.fills):
            closing = Order(
                symbol=fill.order.symbol,
                side="SELL" if fill.order.side.upper() == "BUY" else "BUY",
                quantity=fill.order.quantity,
                price=fill.filled_price,
                option_type=fill.order.option_type,
                strike=fill.order.strike,
                tag="square_off",
            )
            exit_fill = Fill(closing, closing.price, fill.mode, to_ist().isoformat())
            exits.append(exit_fill)
            self._append_ledger(exit_fill)
        self.fills.clear()
        return exits

    @property
    def open_positions(self) -> int:
        return len(self.fills)


def atm_order(
    spot: float,
    strikes: list[int],
    side: str,
    quantity: int,
    premium: float,
    option_type: str = "CE",
    tag: str = "",
) -> Order:
    strike = select_atm(spot, strikes, option_type)
    symbol = f"{'NIFTY' if spot > 20_000 else 'BANKNIFTY'}{strike}{option_type}"
    return Order(symbol=symbol, side=side, quantity=quantity, price=premium,
                 option_type=option_type, strike=strike, tag=tag)


if __name__ == "__main__":  # pragma: no cover
    manager = OrderManager()
    fill = manager.place(atm_order(24_800, [24_750, 24_800, 24_850], "BUY", 50, 120.0))
    print(fill)
    print(f"positions open: {manager.open_positions}")
