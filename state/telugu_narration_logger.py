"""Telugu narration for the full option-trade lifecycle.

Every event carries the exact rupee figures and a timestamp so the dashboard's
draft ``[📜 తెలుగు లైవ్ లాగ్]`` reads like a trading journal, not a status line.

Event order for one trade::

    📡 sync_confirmation -> 🟢 order_placed -> 🛡️ sl_breakeven
    -> 🚀 partial_profit / structure_trail -> 🎯 final_booked
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = REPO_ROOT / "state" / "logs" / "telugu_lifecycle.jsonl"

SYNC = "sync"
ORDER = "order"
TRAIL = "trail"
PARTIAL = "partial"
FINAL = "final"

KIND_LABEL = {
    SYNC: "UPSTOX SYNC",
    ORDER: "ORDER",
    TRAIL: "SL TRAIL",
    PARTIAL: "PARTIAL",
    FINAL: "PROFIT",
}


def rupees(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}\u20b9{abs(value):,.2f}"


def points(value: float) -> str:
    return f"{value:+.2f} pts"


@dataclass
class NarrationEvent:
    at: str
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.at, "kind": self.kind, "text": self.text, "meta": self.meta}


class TeluguNarrationLogger:
    """Accumulates events and optionally streams them to a JSONL file."""

    def __init__(self, persist: bool = False, path: Path = LOG_FILE,
                 clock: callable | None = None) -> None:
        self.events: list[NarrationEvent] = []
        self.persist = persist
        self.path = path
        self.clock = clock or time.strftime

    def _emit(self, kind: str, text: str, meta: dict[str, Any]) -> NarrationEvent:
        event = NarrationEvent(at=self.clock("%Y-%m-%dT%H:%M:%S"), kind=kind, text=text, meta=meta)
        self.events.append(event)
        if self.persist:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
            except OSError:
                pass
        return event

    # -- 1. sync confirmation ---------------------------------------------
    def sync_confirmation(
        self,
        *,
        spot: float,
        strike: str,
        option_type: str,
        option_ltp: float,
        option_atp: float,
        oi_change_pct: float,
        bid: float,
        ask: float,
        paths_up: int,
        samples: int,
    ) -> NarrationEvent:
        text = (
            f"\U0001F4E1 UPSTOX SYNC \u2014 Spot {spot:,.2f} \u00b7 {strike} {option_type} "
            f"LTP {rupees(option_ltp)} \u00b7 ATP(VWAP) {rupees(option_atp)} \u00b7 "
            f"5m \u0394OI {oi_change_pct:+.2f}% \u00b7 Bid/Ask {bid:.2f}/{ask:.2f} "
            f"(spread {max(ask - bid, 0):.2f}) \u00b7 KRONOS {paths_up}/{samples} paths up."
        )
        return self._emit(SYNC, text, {
            "spot": round(spot, 2), "strike": strike, "option_type": option_type,
            "option_ltp": round(option_ltp, 2), "option_atp": round(option_atp, 2),
            "oi_change_pct": round(oi_change_pct, 2), "bid": bid, "ask": ask,
            "paths_up": paths_up, "samples": samples,
        })

    # -- 2. order placed ---------------------------------------------------
    def order_placed(
        self,
        *,
        strike: str,
        option_type: str,
        qty: int,
        lots: int,
        entry: float,
        stop: float,
        target1: float,
        target_p90: float,
    ) -> NarrationEvent:
        risk = max(entry - stop, 0) * qty
        text = (
            f"\U0001F7E2 ORDER PLACED \u2014 {strike} {option_type} \u00b7 {qty} Qty "
            f"({lots} lots) \u00b7 Entry {rupees(entry)} \u00b7 Gear 1 SL {rupees(stop)} "
            f"(max risk {rupees(risk)}) \u00b7 Target-1 {rupees(target1)} \u00b7 "
            f"p90 target {rupees(target_p90)}."
        )
        return self._emit(ORDER, text, {
            "strike": strike, "option_type": option_type, "qty": qty, "lots": lots,
            "entry": round(entry, 2), "stop": round(stop, 2), "risk_rupees": round(risk, 2),
            "target1": round(target1, 2), "target_p90": round(target_p90, 2),
        })

    # -- 3. breakeven ------------------------------------------------------
    def sl_breakeven(self, *, entry: float, old_stop: float, new_stop: float, ltp: float,
                     qty: int) -> NarrationEvent:
        locked = max(new_stop - entry, 0.0) * qty
        text = (
            f"\U0001F6E1\uFE0F SL TRAILED TO BREAKEVEN (Gear 2) \u2014 LTP {rupees(ltp)} at +0.8R. "
            f"SL {rupees(old_stop)} \u2192 {rupees(new_stop)} (Entry + "
            f"{rupees(new_stop - entry)}). Zero Risk Locked ({rupees(locked)})."
        )
        return self._emit(TRAIL, text, {
            "entry": round(entry, 2), "old_stop": round(old_stop, 2),
            "new_stop": round(new_stop, 2), "ltp": round(ltp, 2), "locked_rupees": round(locked, 2),
        })

    # -- 4. partial + structure trail --------------------------------------
    def partial_profit(self, *, entry: float, exit_price: float, qty_booked: int,
                       remaining: int, locked: float) -> NarrationEvent:
        text = (
            f"\U0001F680 PARTIAL PROFIT (Gear 3) \u2014 booked 50% ({qty_booked} Qty) at "
            f"{rupees(exit_price)} \u00b7 {points(exit_price - entry)} \u00b7 "
            f"{rupees(locked)} locked \u00b7 {remaining} Qty riding."
        )
        return self._emit(PARTIAL, text, {
            "exit": round(exit_price, 2), "qty_booked": qty_booked,
            "remaining": remaining, "locked_rupees": round(locked, 2),
        })

    def structure_trail(self, *, old_stop: float, new_stop: float, ltp: float,
                        reason: str = "5m candle low") -> NarrationEvent:
        text = (
            f"\U0001F9F1 STRUCTURE TRAIL \u2014 {reason} \u2192 SL {rupees(old_stop)} \u2192 "
            f"{rupees(new_stop)} (LTP {rupees(ltp)})."
        )
        return self._emit(TRAIL, text, {
            "old_stop": round(old_stop, 2), "new_stop": round(new_stop, 2),
            "ltp": round(ltp, 2), "reason": reason,
        })

    # -- 5. final ----------------------------------------------------------
    def final_booked(self, *, entry: float, exit_price: float, qty: int, reason: str,
                     gross: float, costs: float, net: float, vaulted: float,
                     gear: int) -> NarrationEvent:
        text = (
            f"\U0001F3AF FINAL PROFIT BOOKED (Gear {gear}) \u2014 exit {rupees(exit_price)} "
            f"({points(exit_price - entry)}) \u00b7 reason: {reason} \u00b7 Gross {rupees(gross)} "
            f"\u2212 costs {rupees(costs)} = Net {rupees(net)} \u00b7 "
            f"Vault 70% locked {rupees(vaulted)}."
        )
        return self._emit(FINAL, text, {
            "exit": round(exit_price, 2), "qty": qty, "reason": reason,
            "gross": round(gross, 2), "costs": round(costs, 2),
            "net": round(net, 2), "vaulted": round(vaulted, 2), "gear": gear,
        })

    # -- export ------------------------------------------------------------
    def as_logs(self) -> list[dict[str, Any]]:
        return [
            {
                "t": event.at,
                "kind": {"sync": "no_trade", "order": "order", "trail": "order",
                         "partial": "order", "final": "order"}.get(event.kind, "no_trade"),
                "event": event.kind,
                "text": event.text,
                "meta": event.meta,
            }
            for event in self.events
        ]

    def reset(self) -> None:
        self.events.clear()


# The live terminal narrates with the Story-Mode teller in state/telugu_story.py.
# Re-exported here so this module stays the single import point for Telugu logs.
from state.telugu_story import StoryTeller  # noqa: E402,F401
