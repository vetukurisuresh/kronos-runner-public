#!/usr/bin/env python3
"""Story-Mode Telugu narration for the terminal.

The old log read like a machine dump ("SYNC — Spot 24,900 ... ATP 58.2 ...").
This module writes like a professional friend explaining the market: market
phases, why a trade was taken in two plain sentences, and what the stop is
doing to protect the capital. It keeps the last 20 entries so the SSE payload
always carries the full recent story.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

PHASES = (
    ("preopen", "\U0001F305 ప్రీ-ఓపెన్"),
    ("open", "\U0001F305 ఉదయం ఓపెన్ (9:15–10:00)"),
    ("trend", "\U0001F4C8 ట్రెండ్ టైమ్ (10:00–12:00)"),
    ("midday", "\u2615 మధ్యాహ్నం (12:00–14:00)"),
    ("power", "\u26A1 పవర్ అవర్ (14:00–15:15)"),
    ("close", "\U0001F514 క్లోజింగ్ టైమ్"),
)


def phase_for(moment: datetime | None = None) -> tuple[str, str]:
    """Return (key, Telugu label) for the IST market phase."""
    moment = moment or datetime.now(tz=IST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    minutes = moment.hour * 60 + moment.minute
    if minutes < 9 * 60 + 15:
        return PHASES[0]
    if minutes < 10 * 60:
        return PHASES[1]
    if minutes < 12 * 60:
        return PHASES[2]
    if minutes < 14 * 60:
        return PHASES[3]
    if minutes < 15 * 60 + 15:
        return PHASES[4]
    return PHASES[5]


def rupees(value: float | int | None) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "-" if amount < 0 else ""
    return f"{sign}\u20b9{abs(amount):,.0f}"


def _side_word(side: str) -> str:
    return "CE (కాల్)" if str(side).upper() == "CE" else "PE (పుట్)"


class StoryTeller:
    """Builds beginner-friendly Telugu entries and keeps the last ``limit``."""

    def __init__(self, limit: int = 20, clock=None) -> None:
        self.entries: deque[dict] = deque(maxlen=max(int(limit), 1))
        self.clock = clock or (lambda: datetime.now(tz=IST))
        self.opened_at = time.time()

    # -- internal ----------------------------------------------------------
    def _say(self, emoji: str, title: str, text: str, kind: str = "wait",
             meta: dict | None = None) -> dict:
        now = self.clock()
        phase_key, phase_label = phase_for(now)
        entry = {
            "t": now.strftime("%H:%M"),
            "iso": now.isoformat(timespec="seconds"),
            "phase": phase_key,
            "phase_label": phase_label,
            "emoji": emoji,
            "kind": kind,            # wait | trade | win | loss | guard | summary
            "title": title,
            "text": text,
            "meta": meta or {},
        }
        self.entries.append(entry)
        return entry

    # -- 1. opening --------------------------------------------------------
    def opening(self, *, spot: float, support: float, resistance: float,
                paths_up: int | None, samples: int | None, side: str) -> dict:
        agreement = ""
        if paths_up is not None and samples:
            agreement = (
                f"KRONOS మోడల్ {samples} భవిష్యత్తుల్లో {paths_up} పైకి అంటోంది — "
                f"{'అంటే మెజారిటీ బుల్లిష్' if paths_up >= samples / 2 else 'అంటే మెజారిటీ బేరిష్'}, "
                f"కానీ ఇంకా ధృవీకరణ రాలేదు."
            )
        text = (
            f"ఉదయం 9:15 — మార్కెట్ ఓపెన్ అయింది. నిఫ్టీ ఇప్పుడు {spot:,.2f} దగ్గర ఉంది; "
            f"సపోర్ట్ {support:,.2f}, రెసిస్టెన్స్ {resistance:,.2f}. {agreement} "
            "మొదటి 45 నిమిషాలు దిశ నిర్ణయం కోసం గమనిస్తున్నాం, ఎలాంటి ట్రేడ్ తీసుకోము."
        )
        return self._say("\U0001F305", "మార్కెట్ ఓపెన్", text, "wait",
                         {"spot": spot, "support": support, "resistance": resistance,
                          "paths_up": paths_up, "samples": samples, "side": side})

    # -- 2. waiting --------------------------------------------------------
    def waiting(self, *, spot: float, support: float, resistance: float, reason: str,
                paths_up: int | None, samples: int | None, side: str) -> dict:
        text = (
            f"నిఫ్టీ {spot:,.2f} దగ్గర సపోర్ట్ {support:,.2f} మరియు రెసిస్టెన్స్ "
            f"{resistance:,.2f} మధ్య ఆడుతోంది. {reason} కాబట్టి ఇప్పుడు "
            "ఎలాంటి ట్రేడ్ తీసుకోకుండా క్యాపిటల్ కాపాడుకుంటున్నాం."
        )
        return self._say("\u23F3", "ఇంకా ట్రేడ్ లేదు", text, "wait",
                         {"spot": spot, "support": support, "resistance": resistance,
                          "paths_up": paths_up, "samples": samples, "side": side})

    # -- 3. trade taken ----------------------------------------------------
    def trade_taken(self, *, symbol: str, entry: float, stop: float, target1: float,
                    target2: float, risk: float, paths_up: int | None, samples: int | None,
                    heavy: int, side: str, qty: int | None = None) -> dict:
        confidence = (
            f"మోడల్ {samples} భవిష్యత్తుల్లో {paths_up} పైకి చూపించింది"
            if paths_up is not None and samples else "ట్రెండ్ ఫిల్టర్ మరియు హెవీవెయిట్లు కలిసాయి"
        )
        qty_text = f"{int(qty)} Qty " if qty else ""
        text = (
            f"{symbol} ని {entry:,.2f} దగ్గర {qty_text}కొన్నాం — {confidence}, "
            f"టాప్-5 హెవీవెయిట్లలో {heavy}/5 మా దిశలో ఉన్నాయి. "
            f"స్టాప్ {stop:,.2f} (రిస్క్ ₹{risk:,.2f} పాయింట్లు), మొదటి టార్గెట్ {target1:,.2f}, "
            f"చివరి టార్గెట్ {target2:,.2f}. ఇది పేపర్ ట్రేడ్ — నిజమైన డబ్బు వెళ్లదు."
        )
        return self._say("\U0001F7E2", f"{_side_word(side)} కొన్నాం", text, "trade",
                         {"symbol": symbol, "entry": entry, "stop": stop,
                          "target1": target1, "target2": target2, "heavy": heavy})

    # -- 4. stop management ------------------------------------------------
    def gear_moved(self, *, gear: int, old_stop: float, new_stop: float, ltp: float,
                   pnl: float) -> dict:
        if new_stop >= ltp * 0.995:
            state_text = "స్టాప్ దాదాపు ధర దగ్గరకు వచ్చింది — లాభాన్ని లాక్ చేస్తున్నాం."
        elif old_stop < new_stop:
            state_text = "స్టాప్ క్రిందకి మాత్రమే కదులుతుంది, ఎప్పుడూ పైకి జరుగుతుంది; నష్టం పెరగకుండా కాపాడుతోంది."
        else:
            state_text = "స్టాప్ ఇంకా అదే చోట ఉంది."
        text = (
            f"Gear {gear} కి మారింది. స్టాప్ {old_stop:,.2f} నుంచి {new_stop:,.2f} కి "
            f"జరిగింది (LTP {ltp:,.2f}, P&L {rupees(pnl)}). {state_text}"
        )
        return self._say("\U0001F6E1\uFE0F", f"స్టాప్ అప్‌డేట్ · Gear {gear}", text, "guard",
                         {"gear": gear, "old_stop": old_stop, "new_stop": new_stop,
                          "ltp": ltp, "pnl": pnl})

    # -- 5. milestones -----------------------------------------------------
    def target_hit(self, name: str, price: float, pnl: float) -> dict:
        text = (
            f"{name} టార్గెట్ ₹{price:,.2f} దాదాపు తాకింది. ఇప్పటి వరకు "
            f"లాభం {rupees(pnl)}. ఇక్కడ కొంత బుక్ చేసి, మిగతాది స్టాప్‌తో "
            "పరుగెత్తిస్తే లాభాన్ని ఎక్కువగా ఇంటికి తీసుకెళ్లవచ్చు."
        )
        return self._say("\U0001F3AF", f"{name} టార్గెట్ దగ్గర", text, "win",
                         {"target": name, "price": price, "pnl": pnl})

    def stop_hit(self, *, price: float, pnl: float) -> dict:
        text = (
            f"స్టాప్ ₹{price:,.2f} దగ్గర ట్రేడ్ ముగిసింది. నష్టం {rupees(pnl)} — "
            "ఇది ముందే నిర్ణయించిన రిస్క్ మాత్రమే, ఖాతా సురక్షితం. "
            "తర్వాతి సెటప్ కోసం ఇప్పుడు ప్రశాంతంగా గమనిస్తున్నాం."
        )
        return self._say("\U0001F534", "స్టాప్ హిట్", text, "loss",
                         {"price": price, "pnl": pnl})

    # -- 6. periodic teacher ----------------------------------------------
    def market_update(self, *, spot: float, high: float, low: float, atr: float,
                      paths_up: int | None, samples: int | None, pnl: float,
                      side: str) -> dict:
        direction = "\u2191" if spot >= (high + low) / 2 else "\u2193"
        text = (
            f"నిఫ్టీ {spot:,.2f} ({direction}), రోజు హై {high:,.2f} – లో {low:,.2f}, "
            f"ATR {atr:,.2f}. మన పొజిషన్ P&L {rupees(pnl)}. "
            f"KRONOS {paths_up}/{samples} పైకి — ట్రెండ్ {'కలిస్తే' if (side == 'CE') == (spot >= (high + low) / 2) else 'జాగ్రత్తగా'} "
            "ఉంటే స్టాప్‌ను మాత్రమే కదిలిస్తాం, కొత్త ఎంట్రీ ఇప్పుడు అవసరం లేదు."
        )
        return self._say("\U0001F4CA", "మార్కెట్ అప్‌డేట్", text, "guard",
                         {"spot": spot, "high": high, "low": low, "atr": atr, "pnl": pnl})

    def session_summary(self, *, spot: float, pnl: float, trades: int,
                        high: float, low: float) -> dict:
        text = (
            f"ఈ సెషన్‌లో {trades} ట్రేడ్(లు), మొత్తం P&L {rupees(pnl)}, "
            f"రోజు హై {high:,.2f} – లో {low:,.2f}. "
            "రేపు మార్కెట్ ఓపెన్ అయ్యాక మళ్లీ కలుద్దాం — క్యాపిటల్ కాపాడుకోవడమే మొదటి విజయం."
        )
        return self._say("\U0001F31F", "రోజు ముగింపు", text, "summary",
                         {"spot": spot, "pnl": pnl, "trades": trades, "high": high, "low": low})

    # -- export ------------------------------------------------------------
    def snapshot(self) -> list[dict]:
        return list(self.entries)

    def last(self) -> dict | None:
        return self.entries[-1] if self.entries else None
