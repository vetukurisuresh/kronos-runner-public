"""Telugu narration for the full option-trade lifecycle.

Every event carries the exact rupee figures and a timestamp so the dashboard's
draft ``[📜 తెలుగు లైవ్ లాగ్]`` reads like a trading journal, not a status line.

Event order for one trade::

    📡 sync_confirmation -> 🟢 order_placed -> 🛡️ sl_breakeven
    -> 🚀 partial_profit / structure_trail -> 🎯 final_booked
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = REPO_ROOT / "state" / "logs" / "telugu_lifecycle.jsonl"

SYNC = "sync"
ORDER = "order"
TRAIL = "trail"
PARTIAL = "partial"
FINAL = "final"
SNIPER = "sniper"
VAULT = "vault"
LOCK = "lock"

KIND_LABEL = {
    SYNC: "UPSTOX SYNC",
    ORDER: "ORDER",
    TRAIL: "SL TRAIL",
    PARTIAL: "PARTIAL",
    FINAL: "PROFIT",
    SNIPER: "SNIPER",
    VAULT: "VAULT",
    LOCK: "LOCK",
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

    # -- 4b. sniper reversal / Greek targets / vault breaker ---------------
    def sniper_entry(self, *, symbol: str, side: str, entry: float, stop: float,
                     target1: float, target2: float, signatures: int,
                     p65: float, p90: float) -> NarrationEvent:
        text = (
            f"\U0001F3AF SNIPER {side} \u2014 {symbol} entry {rupees(entry)} \u00b7 "
            f"{signatures}/5 reversal signatures \u00b7 SL {rupees(stop)} \u00b7 "
            f"T1 {rupees(target1)} (p65) \u00b7 T2 {rupees(target2)} (p90 wave) \u00b7 "
            "retail candle confirmation కంటే ముందే order-flow మరియు futures volume "
            "absorption చూపించాయి."
        )
        return self._emit(SNIPER, text, {
            "symbol": symbol, "side": side, "entry": round(entry, 2),
            "stop": round(stop, 2), "target1": round(target1, 2),
            "target2": round(target2, 2), "signatures": signatures,
            "p65": round(p65, 2), "p90": round(p90, 2),
        })

    def greek_targets(self, *, entry: float, target1: float, target2: float,
                      delta: float, gamma: float, theta: float, vega: float,
                      iv_change: float) -> NarrationEvent:
        text = (
            f"\U0001F4D0 GREEK TARGETS \u2014 Entry {rupees(entry)} \u00b7 "
            f"T1 {rupees(target1)} (p65, 50% book) \u00b7 T2 {rupees(target2)} (p90). "
            f"\u0394 {delta:.3f}, \u0393 {gamma:.4f}, \u0398 {theta:.2f}, "
            f"V {vega:.2f}, \u0394IV {iv_change:+.2f}% \u2014 Taylor expansionతో లెక్కించాం."
        )
        return self._emit(SNIPER, text, {
            "entry": round(entry, 2), "target1": round(target1, 2),
            "target2": round(target2, 2), "delta": delta, "gamma": gamma,
            "theta": theta, "vega": vega, "iv_change": iv_change,
        })

    def vault_locked(self, *, profit: float, swept: float, vault: float,
                     tradable: float) -> NarrationEvent:
        text = (
            f"\U0001F512 PROFIT VAULT \u2014 profit {rupees(profit)}లో 50% "
            f"({rupees(swept)}) లాక్; మొత్తం vault {rupees(vault)}, "
            f"ట్రేడింగ్ క్యాపిటల్ {rupees(tradable)}."
        )
        return self._emit(VAULT, text, {
            "profit": round(profit, 2), "swept": round(swept, 2),
            "vault": round(vault, 2), "tradable": round(tradable, 2),
        })

    def daily_lock(self, *, losses: int, realized: float) -> NarrationEvent:
        text = (
            f"\U0001F6AB DAILY LOCK \u2014 {losses} consecutive SLs. "
            f"Net {rupees(realized)}. ఈరోజు కొత్త ట్రేడ్లు లేవు; capital protected."
        )
        return self._emit(LOCK, text, {"losses": losses, "realized": round(realized, 2)})

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
                         "partial": "order", "final": "order", "sniper": "order",
                         "vault": "order", "lock": "no_trade"}.get(event.kind, "no_trade"),
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


# ---------------------------------------------------------------------------
# Programmatic 4-zone brain narration (millisecond ring buffer)
# ---------------------------------------------------------------------------

MAX_LOG_ENTRIES = 80
CATEGORIES = (
    "[DATA SYNC]",
    "[KRONOS-102M]",
    "[KRONOS-25M]",
    "[KRONOS-4M]",
    "[BORDER ALERT]",
    "[EXECUTION]",
)

READING_BASE_TE = (
    "నిఫ్టీ 512 క్యాండిల్స్ + ఫ్యూచర్స్ వాల్యూమ్ (Δvtt) + "
    "SMC సపోర్ట్/రెసిస్టెన్స్ చదువుతోంది"
)
READING_SMALL_TE = (
    "11-స్ట్రైక్స్ ఆప్షన్ చైన్ (CE/PE OI) + Live PCR + India VIX + "
    "Option Greeks (IV/Theta) చదువుతోంది"
)
READING_MINI_TE = (
    "1-నిమిషం ఆప్షన్ చార్ట్ + 5-లెవల్ ఆర్డర్ బుక్ (Bids/Asks) + "
    "TBQ vs TSQ వేగాన్ని చదువుతోంది"
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_ist(now: Any = None) -> datetime:
    if now is None:
        return datetime.now(tz=IST)
    if isinstance(now, datetime):
        return now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(float(now), tz=IST)
    try:
        parsed = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(tz=IST)
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def millisecond_stamp(moment: datetime | None = None) -> str:
    return _as_ist(moment).strftime("%H:%M:%S.%f")[:-3]


def _wheel(value: Any) -> str:
    return f"{_f(value):,.0f}" if _f(value) else "—"


def _symbol(feed: Mapping[str, Any], symbol: str | None) -> str:
    if symbol:
        return str(symbol).upper()
    for key in ("full_spectrum", "option", "spot"):
        block = feed.get(key)
        if isinstance(block, Mapping) and block:
            return "NIFTY" if "NIFTY" in block else str(next(iter(block)))
    return "NIFTY"


def _scanner_value(feed: Mapping[str, Any], symbol: str, key: str) -> str:
    rows = (feed.get("scanner") or {}).get(symbol, []) if isinstance(feed.get("scanner"), Mapping) else []
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if isinstance(row, Mapping) and str(row.get("key") or "") == key:
            return str(row.get("value") or "")
    return ""


def _option(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    rows = feed.get("option") or {}
    if isinstance(rows, Mapping):
        row = rows.get(symbol)
        if isinstance(row, Mapping):
            return row
    return {}


def _option_symbol(feed: Mapping[str, Any], symbol: str, strike: Any, side: str) -> str:
    """The exact watched contract: strike_symbol is authoritative.

    The session stores ``strike_symbol``/``strike_price``; older code read only
    ``strike`` and printed ``0 CE`` while the real ATM was live.
    """
    row = _option(feed, symbol)
    label = str(row.get("strike_symbol") or "").strip()
    if label:
        return label
    number = _f(strike if strike is not None else row.get("strike_price"))
    if number <= 0:
        number = _f(row.get("strike"))
    if number > 0:
        return f"{symbol} {int(number)} {side}"
    return f"{symbol} ATM {side}"


def _base_target(plan: Mapping[str, Any]) -> tuple[float | None, float | None]:
    target = plan.get("p50") or plan.get("median") or plan.get("p65")
    support = plan.get("p10") or plan.get("p25")
    return (
        _f(target) if target is not None else None,
        _f(support) if support is not None else None,
    )


def _base_decision(confidence: float | None) -> str:
    if confidence is None:
        # A green 512-bar buffer is not a missing input.  Say the truth: the
        # model prediction has not reached the narration cache yet.
        return "⚪ 512 BARS READY · MODEL INFERENCE PENDING"
    if confidence >= 78.0:
        return f"🟢 BULLISH CONFIRMED ({confidence:.0f}%)"
    if confidence >= 60.0:
        return f"🟡 WEAK BULLISH ({confidence:.0f}%)"
    if confidence <= 30.0:
        return f"🔴 BEARISH PRESSURE ({confidence:.0f}%)"
    return f"⚪ RANGE / NO EDGE ({confidence:.0f}%)"


def _small_decision(pcr: float | None, iv: float) -> str:
    if pcr is None and iv <= 0:
        return "⚪ WAITING FOR CHAIN + GREEKS"
    if pcr is not None and iv > 0 and (pcr >= 1.05 or pcr <= 0.95):
        return "🟢 RISK GATE PASSED (A+)"
    return "🟡 RISK GATE PARTIAL"


def _mini_decision(obi: float | None, tbq_tsq: float | None, ltp: float, vwap: float) -> str:
    if obi is None and tbq_tsq is None:
        return "⚪ WAITING FOR OBI_5 + TBQ/TSQ"
    above_vwap = ltp > 0 and vwap > 0 and ltp > vwap
    if (_f(obi) >= 1.35 or _f(tbq_tsq) >= 1.20) and above_vwap:
        return "🎯 SNIPER TRIGGER READY"
    if _f(obi) >= 1.15 or _f(tbq_tsq) >= 1.10:
        return "🟡 BUILDING EDGE"
    return "⚪ WAITING FOR OBI EDGE"


class ProgrammaticBrainLogger:
    """Build the three model cards and append millisecond Telugu log lines."""

    def __init__(
        self,
        max_entries: int = MAX_LOG_ENTRIES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.clock = clock or (lambda: datetime.now(tz=IST))
        self.entries: deque[dict[str, Any]] = deque(maxlen=self.max_entries)

    def clear(self) -> None:
        self.entries.clear()

    def _now(self, now: Any = None) -> datetime:
        if now is None:
            try:
                return _as_ist(self.clock())
            except Exception:
                return datetime.now(tz=IST)
        return _as_ist(now)

    def _append(
        self,
        *,
        moment: datetime,
        offset_ms: int,
        tag: str,
        category: str,
        message: str,
        level: str = "info",
    ) -> dict[str, Any]:
        stamped = moment + timedelta(milliseconds=offset_ms)
        entry = {
            "ts": millisecond_stamp(stamped),
            "timestamp": stamped.isoformat(timespec="milliseconds"),
            "tag": tag,
            "category": category,
            "message": message,
            "level": level,
        }
        self.entries.append(entry)
        return entry

    def build_thoughts(
        self,
        feed: Mapping[str, Any] | None,
        symbol: str = "NIFTY",
        *,
        plan: Mapping[str, Any] | None = None,
        oscillator: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        feed = feed or {}
        symbol = _symbol(feed, symbol)
        plan = plan or {}
        if not plan:
            brain = feed.get("model_brain") or {}
            plan = brain.get(symbol) or {} if isinstance(brain, Mapping) else {}
        spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
        spectrum = spectrum if isinstance(spectrum, Mapping) else {}
        spot_block = (feed.get("spot") or {}).get(symbol) or {}
        spot = _f(spot_block.get("ltp") if isinstance(spot_block, Mapping) else 0)
        if spot <= 0:
            spot = _f(spectrum.get("spot"))
        futures = spectrum.get("futures") or {}
        depth = spectrum.get("depth") or {}
        pcr_block = spectrum.get("pcr") or {}
        greeks = spectrum.get("greeks") or {}
        vix = spectrum.get("vix") or {}
        breadth = spectrum.get("breadth") or {}
        option = _option(feed, symbol)
        option_depth = option.get("depth") if isinstance(option.get("depth"), Mapping) else {}
        base_paths = plan.get("paths_up")
        base_samples = plan.get("samples")
        if base_paths is None:
            raw = _scanner_value(feed, symbol, "kronos_base_paths")
            match = re.search(r"(\d+)\s*/\s*(\d+)", raw)
            if match:
                base_paths, base_samples = int(match.group(1)), int(match.group(2))
        base_conf = _f(oscillator.get("base_score")) if oscillator and oscillator.get("base_score") is not None else None
        if base_conf is None:
            base_conf = (
                _clamp(_f(base_paths) / _f(base_samples) * 100.0)
                if _f(base_samples) > 0 else None
            )
        small_conf = (
            _f(oscillator.get("small_score"))
            if oscillator and oscillator.get("small_score") is not None else None
        )
        mini_conf = (
            _f(oscillator.get("mini_score"))
            if oscillator and oscillator.get("mini_score") is not None else None
        )
        target, support = _base_target(plan)
        target = target or (_f(plan.get("spot")) or spot or None)
        pcr = _f(pcr_block.get("pcr")) if pcr_block.get("pcr") is not None else None
        doi_pcr = _f(pcr_block.get("doi_pcr"))
        iv = _f(greeks.get("iv"))
        theta = _f(greeks.get("theta"))
        # Prefer the watched contract's own book; fall back to the verified
        # full-spectrum five-level book and label the provenance.
        if _f(option.get("obi_5")) > 0:
            obi = _f(option.get("obi_5"))
            obi_source = "OPTION_5L"
        elif option_depth.get("obi_5") is not None:
            obi = _f(option_depth.get("obi_5"))
            obi_source = "OPTION_5L"
        else:
            obi = _f(depth.get("obi_5")) if depth.get("obi_5") is not None else None
            obi_source = "FUTURES_5L"
        if _f(option.get("tbq_tsq")) > 0:
            tbq_tsq = _f(option.get("tbq_tsq"))
        elif option_depth.get("tbq_tsq") is not None:
            tbq_tsq = _f(option_depth.get("tbq_tsq"))
        else:
            tbq_tsq = _f(depth.get("tbq_tsq")) if depth.get("tbq_tsq") is not None else None
        strike = option.get("strike") or plan.get("strike")
        side = str(option.get("side") or plan.get("action") or "CE").upper()
        option_ltp = _f(option.get("ltp") if option else 0)
        vwap = _f(option.get("atp_vwap") if option else 0)

        if base_conf is not None:
            support_text = (
                f"నిఫ్టీ {support:,.0f} సపోర్ట్ దగ్గర ఆపరేటర్లు కొంటున్నట్టు SMC చూపిస్తోంది. "
                if support else ""
            )
            target_text = f"{target:,.0f} వరకు వెళ్లే అవకాశం" if target else "పైకి వెళ్లే అవకాశం"
            base_think = (
                f"100 దారులలో {int(_f(base_paths))} దారులు పైకి చూపిస్తున్నాయి. "
                f"{support_text}{target_text} {base_conf:.0f}% ఉంది."
            )
        else:
            base_think = (
                "Kronos-base ఇంకా 512 క్యాండిల్స్ + ఫ్యూచర్స్ Δvtt ఇన్‌పుట్ కోసం వెయిట్ చేస్తోంది. "
                "సపోర్ట్/రెసిస్టెన్స్ మ్యాప్ రెడీ అయిన తర్వాత ట్రెండ్ చెబుతుంది."
            )
        if pcr is not None or iv > 0:
            chain_text = f"Live PCR {pcr:.2f} వద్ద " if pcr is not None else "Option chain "
            if pcr is not None:
                chain_text += "బలంగా ఉంది. " if pcr >= 1.0 else "అమ్మకాల ఒత్తిడిలో ఉంది. "
            oi_text = (
                f"5m ΔOI PCR {doi_pcr:+.2f}. "
                if pcr_block.get("doi_pcr") is not None else ""
            )
            greek_text = (
                f"IV {iv:.2f}% మరియు Θ {theta:+.2f}. "
                if iv > 0 else ""
            )
            small_think = chain_text + oi_text + greek_text + (
                f"VIX {_f(vix.get('ltp')):.2f} · Breadth "
                f"{int(_f(breadth.get('aligned')))}/{int(_f(breadth.get('total')))}."
            )
        else:
            small_think = (
                "11-స్ట్రైక్స్ చైన్ మరియు Greeks ఇంకా సింక్ కాలేదు. "
                "PCR, IV, Θ స్పష్టంగా వచ్చాక రిస్క్ గేట్ నిర్ణయం వస్తుంది."
            )
        if obi is not None or tbq_tsq is not None:
            # Never narrate "NIFTY 0": strike_symbol is the watched contract.
            strike_text = _option_symbol(feed, symbol, strike, side)
            buyer_text = (
                f"{strike_text} లో సెల్లర్ల కంటే బయ్యర్లు {_f(tbq_tsq):.2f} రెట్లు "
                f"({f'TBQ/TSQ {_f(tbq_tsq):.2f}x' if tbq_tsq is not None else 'TBQ/TSQ లేదు'}). "
            )
            price_text = (
                f"LTP ₹{option_ltp:,.2f} · VWAP ₹{vwap:,.2f} "
                f"({'పైన' if option_ltp > vwap and vwap > 0 else 'కింద'}). "
                if option_ltp > 0 and vwap > 0 else ""
            )
            mini_think = (
                f"{buyer_text}{price_text}OBI: {_f(obi):.2f}x. "
                + ("ఎంట్రీ ఎడ్జ్ కన్ఫర్మ్ అవుతోంది." if (_f(obi) >= 1.35 or _f(tbq_tsq) >= 1.2) else "ఇంకా ఎడ్జ్ కోసం వెయిట్.")
            )
        else:
            mini_think = (
                "1-నిమిషం ఆప్షన్ చార్ట్ మరియు 5-లెవల్ Bids/Asks ఇంకా రాలేదు. "
                "OBI_5 మరియు TBQ/TSQ వచ్చాక స్నైపర్ ట్రిగ్గర్ చెబుతుంది."
            )
        return [
            {
                "role": "base",
                "name": "Kronos-base (102M)",
                "title_te": "Kronos-base (102M · 5m/15m పెద్ద ట్రెండ్ బ్రెయిన్)",
                "params": "102M · 512 candles · 100 paths",
                "reading_te": READING_BASE_TE,
                "thinking_te": base_think,
                "decision_state": _base_decision(base_conf),
                "decision_badge": _base_decision(base_conf),
                "confidence_pct": None if base_conf is None else round(base_conf, 1),
                "gate_passed": bool(base_conf is not None and base_conf >= 78.0),
                "paths_up": base_paths,
                "samples": base_samples,
            },
            {
                "role": "small",
                "name": "Kronos-small (25M)",
                "title_te": "Kronos-small (25M · ఆప్షన్ చైన్ & రిస్క్ జడ్జ్)",
                "params": "25M · 11 strikes · PCR / Greeks / VIX",
                "reading_te": READING_SMALL_TE,
                "thinking_te": small_think,
                "decision_state": _small_decision(pcr, iv),
                "decision_badge": _small_decision(pcr, iv),
                "confidence_pct": None if small_conf is None else round(small_conf, 1),
                "gate_passed": bool(_small_decision(pcr, iv).startswith("🟢")),
                "pcr": pcr,
                "iv": iv if iv > 0 else None,
            },
            {
                "role": "mini",
                "name": "Kronos-mini (4M)",
                "title_te": "Kronos-mini (4M · 1m ఆప్షన్ & ఆర్డర్-బుక్ స్నైపర్)",
                "params": "4M · 1m option · OBI_5 / TBQ-TSQ",
                "reading_te": READING_MINI_TE,
                "thinking_te": mini_think,
                "decision_state": _mini_decision(obi, tbq_tsq, option_ltp, vwap),
                "decision_badge": _mini_decision(obi, tbq_tsq, option_ltp, vwap),
                "confidence_pct": None if mini_conf is None else round(mini_conf, 1),
                "gate_passed": _mini_decision(obi, tbq_tsq, option_ltp, vwap).startswith("🎯"),
                "obi_5": obi,
                "obi_source": obi_source,
                "tbq_tsq": tbq_tsq,
                "option_ltp": option_ltp or None,
                "vwap": vwap or None,
                "strike": strike,
                "side": side,
            },
        ]

    def record_cycle(
        self,
        feed: Mapping[str, Any] | None = None,
        symbol: str = "NIFTY",
        *,
        plan: Mapping[str, Any] | None = None,
        oscillator: Mapping[str, Any] | None = None,
        scan_ms: float | None = None,
        sanitize_ms: float | None = None,
        latency_ms: float | None = None,
        futures_volume_ratio: float | None = None,
        now: Any = None,
    ) -> dict[str, Any]:
        """Generate cards plus one immutable batch of millisecond log entries."""
        feed = feed or {}
        symbol = _symbol(feed, symbol)
        moment = self._now(now)
        thoughts = self.build_thoughts(feed, symbol, plan=plan, oscillator=oscillator)
        oscillator = oscillator or {}
        entry_batch: list[dict[str, Any]] = []
        spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
        spectrum = spectrum if isinstance(spectrum, Mapping) else {}
        spot_block = (feed.get("spot") or {}).get(symbol) or {}
        spot = _f(spot_block.get("ltp") if isinstance(spot_block, Mapping) else 0) or _f(spectrum.get("spot"))
        depth = spectrum.get("depth") or {}
        option = _option(feed, symbol)
        strike = (option.get("strike_price") or option.get("strike")
                  or (plan or {}).get("strike") or "—")
        side = str(option.get("side") or option.get("option_type")
                   or (plan or {}).get("action") or "CE").upper()
        strike_text = _option_symbol(feed, symbol, strike, side)
        verify_ms = (
            _f(sanitize_ms) if sanitize_ms is not None
            else _f(spectrum.get("sanitize_ms"))
        )
        wire_ms = _f(latency_ms)
        # An empty option book is zero, not the verified book; fall back to the
        # five-level depth that the arbitrator actually evaluated.
        tick_obi = _f(option.get("obi_5"))
        if tick_obi <= 0:
            tick_obi = _f((option.get("depth") or {}).get("obi_5"))
        if tick_obi <= 0:
            tick_obi = _f(depth.get("obi_5"))
        tick_text = (
            f"Upstox V3 నుండి టిక్ వచ్చింది: {symbol} {spot:,.2f} | "
            f"Fut Vol: {futures_volume_ratio:.1f}x | "
            f"{strike_text} OBI: {tick_obi:.2f}x "
            f"(అన్నీ {verify_ms:.1f}ms లో వెరిఫై అయ్యాయి)"
            if futures_volume_ratio is not None else
            f"Upstox V3 నుండి టిక్ వచ్చింది: {symbol} {spot:,.2f} | "
            f"{strike_text} OBI: {tick_obi:.2f}x "
            f"(అన్నీ {verify_ms:.1f}ms లో వెరిఫై అయ్యాయి)"
        )
        entry_batch.append(self._append(
            moment=moment, offset_ms=0,
            tag=f"⚡ DATA {wire_ms:.0f}ms",
            category="[DATA SYNC]", message=tick_text,
        ))
        scan_text = (
            f"3 మోడల్స్ స్కాన్ పూర్తయింది ({_f(scan_ms):.1f}ms) -> "
            f"Upside: {_f(oscillator.get('bullish_upside_prob_pct')):.1f}% | "
            f"Downside: {_f(oscillator.get('bearish_downside_prob_pct')):.1f}%"
        )
        for thought, category, tag in zip(
            thoughts,
            ("[KRONOS-102M]", "[KRONOS-25M]", "[KRONOS-4M]"),
            ("🧠 102M", "🧠 25M", "🧠 4M"),
        ):
            message = f"{scan_text} · {thought['thinking_te']} · {thought['decision_state']}"
            entry_batch.append(self._append(
                moment=moment,
                offset_ms=2 + len(entry_batch),
                tag=tag,
                category=category,
                message=message,
                level="success" if thought["gate_passed"] else "muted",
            ))
        border = str(oscillator.get("border_status_te") or "")
        if border:
            hit = bool(
                oscillator.get("bullish_border_reached")
                or oscillator.get("bearish_border_reached")
            )
            entry_batch.append(self._append(
                moment=moment, offset_ms=8,
                tag="🎯 BORDER HIT" if hit else "🎚️ BORDER",
                category="[BORDER ALERT]", message=border,
                level="success" if hit else "warning",
            ))
        mini = thoughts[2]
        if mini["gate_passed"]:
            entry_batch.append(self._append(
                moment=moment, offset_ms=10,
                tag="⚡ EXEC",
                category="[EXECUTION]",
                message=(
                    f"NIFTY {int(_f(mini.get('strike')))} {mini.get('side')} ట్రేడ్ ప్లాన్ "
                    "యాక్టివేట్ అయింది! 5/5 నియమాలు పాస్ అయ్యాయి."
                ),
                level="success",
            ))
        return {
            "thoughts": thoughts,
            "captured_at": moment.isoformat(timespec="milliseconds"),
            "new_entries": entry_batch,
            "log": self.snapshot(),
        }

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.entries]


def build_narration_snapshot(
    feed: Mapping[str, Any] | None,
    symbol: str = "NIFTY",
    *,
    plan: Mapping[str, Any] | None = None,
    oscillator: Mapping[str, Any] | None = None,
    now: Any = None,
    logger: ProgrammaticBrainLogger | None = None,
) -> dict[str, Any]:
    """Convenience helper for the dashboard/live-session payload."""
    logger = logger or ProgrammaticBrainLogger()
    return logger.record_cycle(
        feed, symbol, plan=plan, oscillator=oscillator, now=now,
    )


SPLIT_STREAM_KEYS = ("scalper", "intraday", "model")
SPLIT_STREAM_TITLES = {
    "scalper": "⚡ ₹35K Scalper Live Telugu Log",
    "intraday": "🎯 ₹65K Intraday Sniper & SMC Telugu Log",
    "model": "🧠 3-Models & Human-Lead Reasoning Log",
}
SPLIT_SCOPE = {
    "scalper": "scalper_logs",
    "intraday": "intraday_sniper_logs",
    "model": "model_thoughts",
}


class SplitTeluguNarrationLogger:
    """Three independent, second-by-second actionable Telugu streams.

    The object is intentionally I/O-light: ``emit`` appends to an in-memory
    deque and hands the same record to the asynchronous ``FirebaseLogArchiver``.
    It never performs Firebase I/O on the caller's thread.
    """

    def __init__(
        self,
        *,
        archiver: Any = None,
        capacity: int = 800,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.archiver = archiver
        self.clock = clock or (lambda: datetime.now(tz=IST))
        self.streams: dict[str, deque[dict[str, Any]]] = {
            key: deque(maxlen=max(50, int(capacity))) for key in SPLIT_STREAM_KEYS
        }

    def _now(self, value: Any = None) -> datetime:
        if value is None:
            return _as_ist(self.clock())
        return _as_ist(value)

    def emit(
        self,
        stream: str,
        text: str,
        *,
        meta: Mapping[str, Any] | None = None,
        ts: Any = None,
        kind: str = "scan",
    ) -> dict[str, Any]:
        key = str(stream or "").strip().lower()
        if key not in self.streams:
            raise ValueError(f"stream must be one of {SPLIT_STREAM_KEYS}")
        moment = self._now(ts)
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("log text cannot be empty")
        record = {
            "stream": key,
            "title": SPLIT_STREAM_TITLES[key],
            "ts": moment.isoformat(timespec="milliseconds"),
            "time_ist": moment.strftime("%H:%M:%S"),
            "date": moment.strftime("%Y-%m-%d"),
            "kind": str(kind or "scan"),
            "text": clean,
            "meta": dict(meta or {}),
        }
        self.streams[key].append(record)
        if self.archiver is not None:
            self.archiver.enqueue(
                SPLIT_SCOPE[key], record, date=record["date"], ts=record["ts"]
            )
        return dict(record)

    def record_scalp(
        self,
        *,
        spot: float,
        spread: float,
        obi_5: float,
        trigger: str,
        entry: float | None = None,
        stop: float | None = None,
        target: float | None = None,
        extra_te: str = "",
        ts: Any = None,
    ) -> dict[str, Any]:
        levels = ""
        if entry:
            levels = f" Entry ₹{entry:,.2f} · SL ₹{(stop or 0):,.2f} · T1 ₹{(target or 0):,.2f}."
        text = (
            f"⚡ స్కాల్ప్ స్కాన్: NIFTY {spot:,.2f} · స్ప్రెడ్ {spread:.2f} · "
            f"OBI {obi_5:.2f} · సిగ్నల్ {trigger}.{levels} {extra_te}".strip()
        )
        return self.emit("scalper", text, meta={"spot": spot, "spread": spread,
                                                   "obi_5": obi_5, "trigger": trigger},
                         ts=ts, kind="scan" if not entry else "entry")

    def record_intraday(
        self,
        *,
        spot: float,
        zone: str,
        p10: float | None,
        p90: float | None,
        side: str,
        trigger: str,
        entry: float | None = None,
        stop: float | None = None,
        target1: float | None = None,
        target2: float | None = None,
        ts: Any = None,
    ) -> dict[str, Any]:
        corridor = (
            f"p10 ₹{p10:,.2f} · p90 ₹{p90:,.2f}" if p10 and p90 else "p10/p90 waiting"
        )
        levels = (
            f" Entry ₹{entry:,.2f} · SL ₹{(stop or 0):,.2f} · T1 ₹{(target1 or 0):,.2f} "
            f"· T2 ₹{(target2 or 0):,.2f}." if entry else ""
        )
        text = (
            f"🎯 ఇంట్రాడే SMC: NIFTY {spot:,.2f} · {zone} · {corridor} · "
            f"{side} సెటప్ {trigger}.{levels}"
        )
        return self.emit("intraday", text, meta={"spot": spot, "zone": zone,
                                                   "side": side, "trigger": trigger},
                         ts=ts, kind="setup" if not entry else "entry")

    def record_model(
        self,
        *,
        model: str,
        thought_te: str,
        decision: str,
        lead_seconds: float | None = None,
        gate_passed: bool = False,
        ts: Any = None,
    ) -> dict[str, Any]:
        lead = (
            f" · మానవ క్యాండిల్ క్లోజ్ కంటే {lead_seconds:+.1f}s ముందు"
            if lead_seconds is not None else ""
        )
        text = f"🧠 {model}: {thought_te.strip()} · నిర్ణయం: {decision}{lead}"
        return self.emit("model", text, meta={"model": model, "decision": decision,
                                               "lead_seconds": lead_seconds,
                                               "gate_passed": bool(gate_passed)},
                         ts=ts, kind="decision" if gate_passed else "thought")

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {key: [dict(row) for row in rows] for key, rows in self.streams.items()}

    def clear(self, stream: str | None = None) -> None:
        if stream is None:
            for rows in self.streams.values():
                rows.clear()
            return
        self.streams[str(stream)].clear()


__all__ = [
    "IST", "MAX_LOG_ENTRIES", "CATEGORIES",
    "READING_BASE_TE", "READING_SMALL_TE", "READING_MINI_TE",
    "ProgrammaticBrainLogger", "SplitTeluguNarrationLogger", "SPLIT_SCOPE",
    "SPLIT_STREAM_KEYS", "SPLIT_STREAM_TITLES", "TeluguBrainNarrationLogger", "millisecond_stamp",
    "build_narration_snapshot",
]
TeluguBrainNarrationLogger = ProgrammaticBrainLogger
