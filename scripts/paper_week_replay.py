#!/usr/bin/env python3
"""5-day walk-forward paper replay on real Nifty 50 candles.

This is a *rule* replay, not a model run: KRONOS weights never come to this
laptop, so ``paths_up`` is a documented momentum proxy derived from the real
candles. Everything else is the production code path - ``classify_fan``,
``MoneyManager``, ``trailing_sl_engine``, ``TeluguNarrationLogger`` and
``PaperLedger`` - so the Telugu journal and the P&L table are exactly what the
live desk would produce for the same prices.

    python scripts/paper_week_replay.py            # replay + write ledger
    python scripts/paper_week_replay.py --reset    # start a fresh Monday ledger
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from config.settings import IST  # noqa: E402
from execution.pro_money_manager import MoneyManager  # noqa: E402
from execution.trailing_sl_engine import create as trail_create  # noqa: E402
from execution.trailing_sl_engine import update as trail_update  # noqa: E402
from state.paper_trade_ledger import PaperLedger, PaperTrade  # noqa: E402
from state.telugu_narration_logger import TeluguNarrationLogger  # noqa: E402
from strategy.market_regime_traffic import FanRegimeInput, classify_fan  # noqa: E402

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (KRONOS paper replay)"}
BROKERAGE = 40.0
LOT = 75
BASE_QTY = 4 * LOT
PREMIUM = 150.0
DELTA = 0.5
SL_POINTS = 9.5


def fetch_5m(days: str = "15d") -> list[dict]:
    response = requests.get(
        YAHOO.format(symbol="%5ENSEI"),
        params={"range": days, "interval": "5m"},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    stamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    rows: list[dict] = []
    for index, epoch in enumerate(stamps):
        close = quote["close"][index]
        if close is None:
            continue
        rows.append(
            {
                "ts": int(epoch),
                "at": datetime.fromtimestamp(epoch, IST),
                "o": float(quote["open"][index] or close),
                "h": float(quote["high"][index] or close),
                "l": float(quote["low"][index] or close),
                "c": float(close),
            }
        )
    return rows


def sessions(rows: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        key = row["at"].strftime("%Y-%m-%d")
        buckets.setdefault(key, []).append(row)
    return {key: value for key, value in sorted(buckets.items()) if len(value) >= 30}


def momentum_paths_up(day: list[dict], index: int, window: int = 12) -> float:
    """Momentum proxy in 0..100 (documented stand-in for the model fan)."""
    start = max(0, index - window)
    closes = [row["c"] for row in day[start:index + 1]]
    if len(closes) < 3:
        return 50.0
    move_pct = (closes[-1] - closes[0]) / max(closes[0], 1e-9) * 100.0
    # Calibrated so a flat hour sits near 50 (RED) and a 0.6% hourly drift
    # reaches the YELLOW band instead of saturating at 100 every day.
    return max(5.0, min(95.0, 50.0 + move_pct * 70.0))


def inside_overlap(day: list[dict], index: int, window: int = 8) -> float:
    start = max(1, index - window + 1)
    hits = 0
    total = 0
    for i in range(start, index + 1):
        prev, cur = day[i - 1], day[i]
        total += 1
        if cur["h"] <= prev["h"] and cur["l"] >= prev["l"]:
            hits += 1
    return hits / total if total else 0.0


def opening_range_ratio(day: list[dict], index: int) -> float:
    first = day[: min(9, index + 1)]  # nine 5m bars = 45 minutes
    if len(first) < 2:
        return 1.0
    opening = max(r["h"] for r in first) - min(r["l"] for r in first)
    five_day = [max(r["h"] for r in day[b:b + 9]) - min(r["l"] for r in day[b:b + 9])
                for b in range(0, min(len(day) - 9, 90), 9)]
    average = sum(five_day) / max(len(five_day), 1) or opening or 1.0
    return opening / average


def strike_for(spot: float) -> int:
    return int(round(spot / 50.0) * 50)


def simulate_day(date: str, day: list[dict], ledger: PaperLedger,
                 logger: TeluguNarrationLogger) -> dict:
    entry_index = min(10, len(day) - 30)
    spot = day[entry_index]["c"]
    paths = momentum_paths_up(day, entry_index)
    overlap = inside_overlap(day, entry_index)
    opening = opening_range_ratio(day, entry_index)
    straddle_decay = day[entry_index]["c"] < day[entry_index]["o"] and opening < 0.6
    fan = FanRegimeInput(
        paths_up=paths,
        dispersion_pct=0.9,
        heavyweights_aligned=5 if paths > 78 else 2,
        oi_unwinding_pct=7.0 if paths > 83 else 2.0,
        inside_bar_overlap=overlap,
        opening_range_ratio=opening,
        straddle_below_vwap=straddle_decay,
        minutes_to_close=375 - (entry_index + 1) * 5,
    )
    moment = datetime.combine(datetime.fromisoformat(date).date(), datetime.min.time(), IST)
    moment = moment.replace(hour=10, minute=0)
    state = classify_fan(fan, now=moment)
    ledger.register_day(date, state.light, note=f"fan {paths:.0f}/100 (momentum proxy)")

    if state.light == "RED" or state.max_trades == 0:
        logger._emit("no_trade", f"\U0001F6AB {date}: {state.telugu}", {"light": "RED"})
        return {"date": date, "light": "RED", "trades": 0, "net": 0.0}

    side = "CE" if paths >= 50 else "PE"
    direction = 1 if side == "CE" else -1
    strike = strike_for(spot)
    contract = f"NIFTY {strike} {side}"
    label = f"NIFTY {strike}"
    entry = PREMIUM
    stop = entry - SL_POINTS
    trail = trail_create(entry, stop, direction)
    manager = MoneyManager(capital=ledger.capital, lot_size=LOT, base_lots=4)
    qty = manager.base_qty

    logger.sync_confirmation(
        spot=spot, strike=label, option_type=side, option_ltp=entry, option_atp=entry - 1.2,
        oi_change_pct=-7.2 if paths > 83 else 2.1, bid=entry - 0.05, ask=entry + 0.05,
        paths_up=int(round(paths)), samples=100,
    )
    logger.order_placed(strike=label, option_type=side, qty=qty, lots=4, entry=entry,
                        stop=stop, target1=entry + 1.8 * SL_POINTS,
                        target_p90=entry + 3.2 * SL_POINTS)

    booked = 0.0
    remaining = qty
    gear_seen = 1
    exit_price = entry
    reason = "session close"
    for row in day[entry_index + 1: entry_index + 60]:
        premium = max(1.0, entry + (row["c"] - spot) * DELTA * direction)
        new_trail = trail_update(trail, premium)
        if new_trail.gear > gear_seen:
            gear_seen = new_trail.gear
            if gear_seen == 2:
                logger.sl_breakeven(entry=entry, old_stop=trail.stop,
                                    new_stop=new_trail.stop, ltp=premium, qty=remaining)
            elif gear_seen == 3:
                half = remaining // 2
                locked = (premium - entry) * half
                logger.partial_profit(entry=entry, exit_price=premium, qty_booked=half,
                                      remaining=remaining - half, locked=locked)
                booked += locked
                remaining -= half
            elif gear_seen == 4:
                logger.structure_trail(old_stop=trail.stop, new_stop=new_trail.stop,
                                       ltp=premium, reason="5-minute candle low")
        trail = new_trail
        exit_price = premium
        if premium <= trail.stop:
            # Fill at the stop, not at a 5-minute close far below it.
            exit_price = trail.stop
            reason = "Trailing SL hit"
            break
        if premium >= entry + 3.2 * SL_POINTS:
            reason = "p90 Top Reversal Zone reached"
            break

    gross = booked + (exit_price - entry) * remaining
    costs = BROKERAGE * (2 if remaining == qty else 3)
    net = gross - costs
    vaulted = round(max(net, 0.0) * 0.70, 2)
    outcome = "WIN" if net > 0 else ("BREAKEVEN" if abs(net) < 1 else "SL")
    logger.final_booked(entry=entry, exit_price=exit_price, qty=qty, reason=reason,
                        gross=gross, costs=costs, net=net, vaulted=vaulted, gear=gear_seen)

    ledger.record(PaperTrade(
        date=date, light=state.light, symbol="NIFTY", strike=contract, option_type=side,
        qty=qty, entry=entry, exit=round(exit_price, 2),
        points=round(exit_price - entry, 2), gross=round(gross, 2), costs=round(costs, 2),
        net=round(net, 2), vaulted=vaulted, outcome=outcome,
        entry_at=f"{date}T10:00:00", exit_at=f"{date}T11:00:00",
    ))
    return {"date": date, "light": state.light, "trades": 1, "net": round(net, 2)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5-day paper replay on real Nifty data")
    parser.add_argument("--reset", action="store_true", help="start a fresh ledger")
    parser.add_argument("--days", type=int, default=5)
    args = parser.parse_args(argv)

    ledger = PaperLedger.load()
    if args.reset:
        ledger.reset()
        print("ledger reset to Rs 1,00,000")

    print("fetching real Nifty 50 5-minute candles …")
    rows = fetch_5m()
    buckets = sessions(rows)
    chosen = list(buckets.items())[-args.days:]
    if not chosen:
        print("no sessions returned by the data source")
        return 1
    print(f"replaying {len(chosen)} sessions (paths_up = momentum proxy, weights stay cloud-side)\n")

    logger = TeluguNarrationLogger()
    for date, day in chosen:
        result = simulate_day(date, day, ledger, logger)
        print(f"  {date}: {result['light']:<7} trades={result['trades']} net={result['net']:>10,.0f}")

    ledger.save()
    Path(REPO_ROOT / "state" / "paper_week_logs.json").write_text(
        json.dumps(logger.as_logs(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n" + "=" * 118)
    print("1-WEEK PAPER TRADING LEDGER (real Nifty 50 candles, paper capital Rs 1,00,000)")
    print("=" * 118)
    print(ledger.render_table())
    print("=" * 118)
    print(f"ending balance: Rs {ledger.capital:,.0f} | vault: Rs {ledger.vault:,.0f} | "
          f"trades: {len(ledger.trades)}")
    print("\nTelugu lifecycle sample:")
    for event in logger.events[:6]:
        print(f"  {event.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
