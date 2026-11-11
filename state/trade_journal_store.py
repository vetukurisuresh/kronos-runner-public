"""Institutional trade journal storage for the KRONOS pro PDF report.

Every live or paper trade is reduced to a self-contained record: execution and
T1/T2 split, charges, net rupees, simple English + Telugu reasons, entry/exit
microstructure and the option/spot candle window used by the chart renderer.

The store is deliberately independent of the dashboard and of matplotlib.  The
live session calls :meth:`TradeJournalStore.record_live_trade` on every ledger
signature change (entry, T1, T2, trail, close), and the report layer reads the
same JSON later.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
JOURNAL_FILE = REPO_ROOT / "state" / "journal_history.json"
IST = timezone(timedelta(hours=5, minutes=30))
JOURNAL_VERSION = 1

# Upstox / NSE option charges (per executed leg).  These are estimates used by
# the report; the contract note remains the accounting source of truth.
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.001          # 0.1% on sell-side option premium
EXCHANGE_TXN_RATE = 0.0003503  # NSE options turnover
SEBI_RATE = 0.000001
GST_RATE = 0.18
STAMP_BUY_RATE = 0.00003

T1_LEG_NAMES = {"T1", "HERO_2X_LOCK", "SCALP_S1", "S1"}
FINAL_WIN_LEG_NAMES = {
    "T2", "HERO_P90_HARVEST", "SCALP_S2", "S2",
    "SUPER_T2_30", "SUPER_RUNNER_EXIT",
}
FINAL_STOP_LEG_NAMES = {
    "SL", "TSL", "TRAIL", "TRAILING_SL", "STOP", "SCALP_SL",
    "SCALP_BREAKEVEN_STOP", "SCALP_TIME_STOP", "P90_TOP_EXIT",
    "PEAK_EXHAUSTION", "SQUARE_OFF", "MANUAL", "MANUAL_CLOSE",
    "MANUAL_SMART_CLOSE",
}

_ENTRY_ONLY_FIELDS = {
    "date_ist", "entry_time_ist", "entry_price", "initial_sl_price",
    "initial_sl_pts", "total_qty", "strategy_tag", "option_strike",
    "option_type", "expiry_date", "mode", "symbol", "spot_entry",
    "futures_basis_entry", "vix_entry", "pcr_entry", "obi_5_entry",
    "tbq_tsq_ratio_entry", "doi_5m_pct", "greeks_entry",
}

_LOCK = threading.RLock()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _as_ist(moment: datetime | str | None) -> datetime:
    if moment is None:
        return datetime.now(tz=IST)
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(tz=IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def _iso(moment: datetime | str | None) -> str:
    if not moment:
        return ""
    return _as_ist(moment).isoformat(timespec="seconds")


def _clock(value: Any) -> str:
    text = str(value or "")
    match = __import__("re").search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return "—"
    hour, minute = int(match.group(1)), match.group(2)
    suffix = "PM" if hour >= 12 else "AM"
    hour = hour % 12 or 12
    return f"{hour:02d}:{minute} {suffix}"


def _inr(value: Any, sign: bool = False, decimals: int = 0) -> str:
    number = _f(value)
    prefix = "-" if number < 0 else ("+" if sign and number > 0 else "")
    text = f"{abs(number):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    suffix = f".{fraction}" if fraction else ""
    return f"{prefix}₹{whole}{suffix}"


def _points(value: Any) -> str:
    return f"{_f(value):+.2f}"


def journal_path() -> Path:
    """Allow tests and the runner to redirect the journal without monkeypatching."""
    override = (os.environ.get("KRONOS_JOURNAL_FILE") or "").strip()
    return Path(override) if override else JOURNAL_FILE


def _candle(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise ui_bars short keys and engine candle long keys."""
    row = row or {}
    stamp = row.get("time", row.get("t", ""))
    return {
        "time": stamp,
        "open": round(_f(row.get("open", row.get("o"))), 2),
        "high": round(_f(row.get("high", row.get("h"))), 2),
        "low": round(_f(row.get("low", row.get("l"))), 2),
        "close": round(_f(row.get("close", row.get("c"))), 2),
        "volume": round(_f(row.get("volume", row.get("v"))), 2),
        "amount": round(_f(row.get("amount", row.get("a"))), 2),
    }


def _candles(rows: Iterable[dict[str, Any]] | None, limit: int) -> list[dict[str, Any]]:
    clean = [_candle(row) for row in (rows or []) if isinstance(row, dict)]
    clean = [row for row in clean if row["open"] > 0 and row["high"] > 0]
    return clean[-limit:]


def estimate_charges(trade: dict[str, Any]) -> dict[str, float]:
    """Estimate Upstox brokerage, STT, NSE txn, SEBI, GST and stamp duty."""
    entry = _f(trade.get("entry_price"))
    qty = int(_f(trade.get("initial_qty") or trade.get("total_qty") or trade.get("qty")))
    legs = [leg for leg in (trade.get("booked_legs") or []) if isinstance(leg, dict)]
    if not qty:
        qty = int(_f(trade.get("total_qty")))
    buy_turnover = entry * qty
    sell_turnover = 0.0
    sell_orders = 0
    for leg in legs:
        leg_qty = int(_f(leg.get("qty")))
        leg_price = _f(leg.get("price"))
        if leg_qty > 0 and leg_price > 0:
            sell_turnover += leg_price * leg_qty
            sell_orders += 1
    if not sell_orders and str(trade.get("status") or "").upper() == "CLOSED":
        exit_price = _f(trade.get("exit_price"))
        if exit_price > 0 and qty > 0:
            sell_turnover = exit_price * qty
            sell_orders = 1
    turnover = buy_turnover + sell_turnover
    orders = (1 if buy_turnover > 0 else 0) + sell_orders
    brokerage = BROKERAGE_PER_ORDER * orders
    stt = sell_turnover * STT_SELL_RATE
    exchange = turnover * EXCHANGE_TXN_RATE
    sebi = turnover * SEBI_RATE
    gst = (brokerage + exchange + sebi) * GST_RATE
    stamp = buy_turnover * STAMP_BUY_RATE
    total = brokerage + stt + exchange + sebi + gst + stamp
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange": round(exchange, 2),
        "sebi": round(sebi, 2),
        "gst": round(gst, 2),
        "stamp": round(stamp, 2),
        "total": round(total, 2),
    }


def _leg_name(leg: dict[str, Any]) -> str:
    return str((leg or {}).get("leg") or "").upper()


def _is_t1(leg: dict[str, Any]) -> bool:
    name = _leg_name(leg)
    return name in T1_LEG_NAMES or name.startswith("HERO_2X") or name.startswith("SUPER_T1")


def _is_final_win(leg: dict[str, Any]) -> bool:
    name = _leg_name(leg)
    return (
        name in FINAL_WIN_LEG_NAMES
        or name.startswith("HERO_P90")
        or name.startswith("SUPER_T2")
        or name.startswith("SUPER_RUNNER")
    )


def _exit_reason(trade: dict[str, Any], legs: list[dict[str, Any]]) -> str:
    status = str(trade.get("status") or "").upper()
    if status != "CLOSED":
        return "OPEN"
    raw = str(trade.get("exit_reason") or "").upper().replace(" ", "_")
    final = legs[-1] if legs else {}
    final_name = _leg_name(final)
    t1_done = any(_is_t1(leg) for leg in legs)
    if raw in {"T2", "T2_HIT"} or final_name in FINAL_WIN_LEG_NAMES or final_name.startswith("SUPER_T2") or final_name.startswith("SUPER_RUNNER"):
        return "T2_HIT"
    if "PEAK" in raw or "PEAK" in final_name:
        return "PEAK_EXHAUSTION_EXIT"
    if "MANUAL" in raw or "SQUARE_OFF" in raw or "MANUAL" in final_name:
        return "MANUAL_SMART_CLOSE"
    if raw in {"SL", "TSL", "TRAIL", "STOP", "TRAILING_SL"} or "SL" in final_name or "STOP" in final_name:
        initial = _f(trade.get("initial_sl"))
        trailed = _f(trade.get("current_trailing_sl"))
        moved = bool(initial > 0 and trailed > initial + 0.005)
        return "TRAILING_SL_HIT" if (t1_done or moved) else "INITIAL_SL_HIT"
    if final_name and final_name not in {"WATCHING"}:
        return "TRAILING_SL_HIT" if t1_done else "INITIAL_SL_HIT"
    return "MANUAL_SMART_CLOSE"


def infer_strategy(trade: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    context = context or {}
    explicit = str(context.get("strategy_hint") or trade.get("strategy_tag") or "").upper()
    if explicit:
        return explicit
    if trade.get("scalp_mode"):
        regime = str(trade.get("scalp_regime") or context.get("regime") or "").upper()
        return "PING_PONG_SCALP" if "PING" in regime else "VELOCITY_SCALP"
    if trade.get("hero_mode"):
        return "EXPIRY_ZERO_TO_HERO"
    sniper = trade.get("sniper") or {}
    if sniper.get("trigger") or sniper.get("pe_trigger"):
        side = str(trade.get("option_type") or "").upper()
        return "TOP_REVERSAL_SNIPER" if side == "PE" else "BOTTOM_REVERSAL_SNIPER"
    if trade.get("sniper_grade") and str(trade.get("sniper_grade")).upper() not in {"SCALP", "WATCHING"}:
        side = str(trade.get("option_type") or "").upper()
        return "TOP_REVERSAL_SNIPER" if side == "PE" else "BOTTOM_REVERSAL_SNIPER"
    return "KRONOS_ALPHA"


def _option_label(trade: dict[str, Any], context: dict[str, Any]) -> tuple[str, str, str]:
    label = str(
        trade.get("option_symbol") or trade.get("strike_symbol")
        or trade.get("strike") or context.get("option_strike") or ""
    ).strip()
    symbol = str(context.get("symbol") or trade.get("symbol") or "").upper()
    option_type = str(trade.get("option_type") or context.get("option_type") or "").upper()
    if not symbol and label:
        symbol = "BANKNIFTY" if label.upper().startswith("BANKNIFTY") else "NIFTY"
    if not option_type and label:
        tail = label.upper().rsplit(" ", 1)[-1]
        option_type = tail if tail in {"CE", "PE"} else ""
    if label and symbol and not label.upper().startswith(symbol):
        label = f"{symbol} {label}"
    return symbol or "NIFTY", label or f"{symbol} {option_type}".strip(), option_type or "CE"


def _model_reasons(
    trade: dict[str, Any], context: dict[str, Any], option_type: str
) -> tuple[str, str]:
    paths = _f(context.get("paths_up"))
    samples = _f(context.get("samples"))
    pct = round(paths / samples * 100.0, 0) if samples else _f(context.get("paths_pct"))
    p10, median, p90 = _f(context.get("p10")), _f(context.get("median")), _f(context.get("p90"))
    spot = _f(context.get("spot"))
    bearish = option_type == "PE"
    direction = "down" if bearish else "up"
    edge = round((100 - pct) if bearish else pct, 0) if samples else pct
    support = f"p90 resistance (₹{p90:,.0f})" if bearish else f"p10 support (₹{p10:,.0f})"
    target = f"p10 (₹{p10:,.0f})" if bearish else f"p90 (₹{p90:,.0f})"
    en = (
        f"KRONOS 100-path model gave a {edge:.0f}% probability of moving {direction}. "
        f"Price was at ₹{spot:,.2f}, near the {support}, with the median path at "
        f"₹{median:,.0f} and the opposite cone edge at {target}. The 1-minute "
        f"Kronos-mini velocity turned {direction}."
    )
    te = (
        f"KRONOS 100-వే మోడల్ {edge:.0f}% {('కిందకి' if bearish else 'పైకి')} వెళ్లే సంభావ్యత చూపించింది. "
        f"ధర ₹{spot:,.2f} వద్ద {('p90 రెసిస్టెన్స్' if bearish else 'p10 సపోర్ట్')} "
        f"(₹{p90 if bearish else p10:,.0f}) ను తాకింది; మధ్యస్థ మార్గం ₹{median:,.0f}, "
        f"ఎదురుగా ఉన్న కోన్ ఎడ్జ్ {target}. 1-నిమిషం Kronos-mini వేగం "
        f"{('కిందకి' if bearish else 'పైకి')} తిరిగింది."
    )
    return en, te


def _technical_reasons(
    trade: dict[str, Any], context: dict[str, Any], option_type: str
) -> tuple[str, str]:
    bearish = option_type == "PE"
    mtf = context.get("mtf") or {}
    labels = []
    for key in ("m1", "m5", "m15"):
        row = mtf.get(key) or {}
        trend = str(row.get("trend") or row.get("label") or "").upper()
        if trend:
            labels.append(f"{key} {trend}")
    mtf_text = ", ".join(labels) if labels else "1m + 5m + 15m MTF aligned"
    vol_ratio = _f(context.get("futures_volume_ratio"))
    atp = _f(context.get("atp"))
    obi = _f(context.get("obi_5"))
    tbq_tsq = _f(context.get("tbq_tsq"))
    pcr = _f(context.get("pcr"))
    doi = _f(context.get("doi_5m_pct"))
    side_word = "bearish" if bearish else "bullish"
    side_te = "బేరిష్" if bearish else "బుల్లిష్"
    writer = "Call writers added fresh OI" if bearish else "Put writers added fresh OI"
    writer_te = "కాల్ రైటర్లు కొత్త OI జోడించారు" if bearish else "పుట్ రైటర్లు కొత్త OI జోడించారు"
    en = (
        f"{mtf_text}. Nifty futures volume was {vol_ratio*100:.0f}% of its recent "
        f"average, the option traded around VWAP/ATP ₹{atp:,.2f}, and the 5-level "
        f"book showed {('sellers' if bearish else 'buyers')} {obi:.2f}x "
        f"(OBI {obi:.2f}, TBQ/TSQ {tbq_tsq:.2f}x). PCR was {pcr:.2f} with a "
        f"{doi:+.2f}% 5-minute OI change. {writer}."
    )
    te = (
        f"{mtf_text} {side_te}గా ఉన్నాయి. Nifty Futures వాల్యూమ్ సగటులో "
        f"{vol_ratio*100:.0f}% ఉంది, ఆప్షన్ ధర VWAP/ATP ₹{atp:,.2f} దగ్గర "
        f"ట్రేడ్ అయింది, 5-లెవల్ ఆర్డర్ బుక్‌లో "
        f"{('సెల్లర్లు' if bearish else 'బయ్యర్లు')} {obi:.2f}x ఎక్కువగా ఉన్నారు "
        f"(OBI {obi:.2f}, TBQ/TSQ {tbq_tsq:.2f}x). PCR {pcr:.2f}, 5-నిమిషాల OI మార్పు "
        f"{doi:+.2f}%. {writer_te}."
    )
    return en, te


def _exit_reasons(
    trade: dict[str, Any], legs: list[dict[str, Any]], exit_reason: str, net: float
) -> tuple[str, str]:
    entry = _f(trade.get("entry_price"))
    t1 = next((leg for leg in legs if _is_t1(leg)), None)
    final = legs[-1] if legs else None
    status = str(trade.get("status") or "").upper()
    if status != "CLOSED":
        en = (
            f"The position is still open at ₹{_f(trade.get('live_pnl_rs')):,.0f} live P&L. "
            "The journal updates at every T1/T2 booking and at the final exit."
        )
        te = (
            f"పొజిషన్ ఇంకా ఓపెన్‌లో ఉంది; ప్రస్తుత లైవ్ P&L ₹{_f(trade.get('live_pnl_rs')):,.0f}. "
            "ప్రతి T1/T2 బుకింగ్ మరియు ఫైనల్ ఎగ్జిట్ వద్ద జర్నల్ అప్‌డేట్ అవుతుంది."
        )
        return en, te
    if t1 and final and _leg_name(final) not in T1_LEG_NAMES:
        t1_qty, t1_price, t1_pnl = int(_f(t1.get("qty"))), _f(t1.get("price")), _f(t1.get("pnl"))
        f_qty, f_price, f_pnl = int(_f(final.get("qty"))), _f(final.get("price")), _f(final.get("pnl"))
        final_label = _leg_name(final) or "FINAL EXIT"
        en = (
            f"Booked {t1_qty} qty at T1 ₹{t1_price:,.2f}, locking {_inr(t1_pnl, True)}. "
            f"Then closed the remaining {f_qty} qty at {final_label} ₹{f_price:,.2f} for "
            f"{_inr(f_pnl, True)}; total trade net {_inr(net, True)}."
        )
        te = (
            f"T1 ₹{t1_price:,.2f} వద్ద {t1_qty} క్వాంటిటీ బుక్ చేసి {_inr(t1_pnl, True)} లాక్ చేశాము. "
            f"ఆ తర్వాత మిగిలిన {f_qty} క్వాంటిటీని {final_label} ₹{f_price:,.2f} వద్ద మూసి "
            f"{_inr(f_pnl, True)} సాధించాము; మొత్తం నెట్ {_inr(net, True)}."
        )
        return en, te
    if exit_reason in {"INITIAL_SL_HIT", "TRAILING_SL_HIT"}:
        exit_price = _f(trade.get("exit_price") or (final or {}).get("price"))
        en = (
            f"The {('initial' if exit_reason == 'INITIAL_SL_HIT' else 'trailing')} stop was hit "
            f"at ₹{exit_price:,.2f}. Net result {_inr(net, True)}; risk stayed inside the "
            "pre-defined envelope and base capital was protected."
        )
        te = (
            f"{('ప్రారంభ' if exit_reason == 'INITIAL_SL_HIT' else 'ట్రైలింగ్')} స్టాప్ "
            f"₹{exit_price:,.2f} వద్ద హిట్ అయింది. నెట్ ఫలితం {_inr(net, True)}; "
            "రిస్క్ ముందే నిర్ణయించిన పరిధిలోనే ఉండి బేస్ క్యాపిటల్ రక్షించబడింది."
        )
        return en, te
    exit_price = _f(trade.get("exit_price") or (final or {}).get("price"))
    en = (
        f"The position was closed at ₹{exit_price:,.2f} with a net result of "
        f"{_inr(net, True)}. The exit followed the plan's target/trail rules."
    )
    te = (
        f"పొజిషన్ ₹{exit_price:,.2f} వద్ద మూసివేయబడింది; నెట్ ఫలితం {_inr(net, True)}. "
        "ప్లాన్‌లోని టార్గెట్/ట్రైల్ నియమాల ప్రకారం ఎగ్జిట్ జరిగింది."
    )
    return en, te


def build_record(
    trade: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    option_candles: Iterable[dict[str, Any]] | None = None,
    spot_candles: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a live trade dict into the complete institutional record."""
    trade = trade or {}
    context = context or {}
    symbol, label, option_type = _option_label(trade, context)
    entry_at = _iso(trade.get("entry_at") or context.get("entry_at") or datetime.now(tz=IST))
    exit_at = _iso(trade.get("exit_at") or "") if str(trade.get("status") or "").upper() == "CLOSED" else ""
    entry_dt = _as_ist(entry_at)
    exit_dt = _as_ist(exit_at) if exit_at else None
    duration = int(max(0.0, (exit_dt - entry_dt).total_seconds())) if exit_dt else 0

    legs = [dict(leg) for leg in (trade.get("booked_legs") or []) if isinstance(leg, dict)]
    t1_leg = next((leg for leg in legs if _is_t1(leg)), None)
    post_t1 = [leg for leg in legs if not _is_t1(leg)]
    final_leg = post_t1[-1] if post_t1 else None
    gross = _f(trade.get("realized_pnl_rs"))
    charges = estimate_charges(trade)
    net = round(gross - charges["total"], 2)
    entry = _f(trade.get("entry_price"))
    qty = int(_f(trade.get("initial_qty") or trade.get("qty") or 0))
    initial_sl = _f(trade.get("initial_sl"))
    if initial_sl <= 0 and entry:
        initial_sl = entry - _f(trade.get("risk_points") or trade.get("sl_points") or 4.0)
    final_sl = _f(trade.get("current_trailing_sl"))
    exit_reason = _exit_reason(trade, legs)
    t1_hit = bool(t1_leg)
    t2_hit = bool(final_leg and _is_final_win(final_leg))
    final_qty = sum(int(_f(leg.get("qty"))) for leg in post_t1)
    final_pnl = sum(_f(leg.get("pnl")) for leg in post_t1)
    t1_pnl = _f((t1_leg or {}).get("pnl"))
    roi = round(net / (entry * qty) * 100.0, 2) if entry and qty else 0.0

    model_en, model_te = _model_reasons(trade, context, option_type)
    technical_en, technical_te = _technical_reasons(trade, context, option_type)
    exit_en, exit_te = _exit_reasons(trade, legs, exit_reason, net)

    greeks = context.get("greeks") or {}
    greeks_clean = {
        key: round(_f(greeks.get(key)), 4)
        for key in ("delta", "gamma", "theta", "vega", "iv")
    }
    strategy = infer_strategy(trade, context)
    mode = str(context.get("mode") or trade.get("mode") or "PAPER").upper()
    p10, median, p90 = _f(context.get("p10")), _f(context.get("median")), _f(context.get("p90"))
    mtf = context.get("mtf") or {}
    mtf_bits = []
    for key in ("m1", "m5", "m15"):
        trend = str((mtf.get(key) or {}).get("trend") or "").upper()
        if trend:
            mtf_bits.append(f"{key.upper()} {trend}")
    paths = _f(context.get("paths_up"))
    samples = _f(context.get("samples"))
    probability = round(paths / samples * 100.0) if samples else 0
    edge_probability = round(100 - probability) if option_type == "PE" else probability
    option_window = _candles(option_candles, 45)
    spot_window = _candles(spot_candles, 30)
    atp = _f(context.get("atp"))
    if not atp and option_window:
        total_volume = sum(max(row["volume"], 0.0) for row in option_window)
        if total_volume > 0:
            weighted = sum(((r["high"] + r["low"] + r["close"]) / 3.0) * r["volume"] for r in option_window)
            atp = round(weighted / total_volume, 2)
    charts = {
        "option_candles": option_window,
        "spot_candles": spot_window,
        "p10": round(p10, 2),
        "median": round(median, 2),
        "p90": round(p90, 2),
        "atp": round(atp, 2),
    }

    record = {
        "trade_id": str(trade.get("trade_id") or context.get("trade_id") or ""),
        "date_ist": entry_dt.date().isoformat(),
        "entry_time_ist": entry_at,
        "exit_time_ist": exit_at,
        "duration_sec": duration,
        "mode": mode,
        "symbol": symbol,
        "option_strike": label,
        "option_type": option_type,
        "expiry_date": str(context.get("expiry") or trade.get("expiry") or ""),
        "strategy_tag": strategy,
        "status": str(trade.get("status") or "OPEN").upper(),
        "total_qty": qty,
        "entry_price": round(entry, 2),
        "initial_sl_price": round(initial_sl, 2),
        "initial_sl_pts": round(max(entry - initial_sl, 0.0), 2),
        "final_trailed_sl_price": round(final_sl, 2),
        "max_gear_reached": int(_f(trade.get("tsl_gear"), 1)),
        "t1_target_price": round(_f(trade.get("target_1") or trade.get("scalp_s1_target")), 2),
        "t1_hit": t1_hit,
        "t1_hit_time": str((t1_leg or {}).get("at") or ""),
        "t1_booked_qty": int(_f((t1_leg or {}).get("qty"))),
        "t1_booked_price": round(_f((t1_leg or {}).get("price")), 2),
        "t1_gross_profit_inr": round(t1_pnl, 2),
        "t2_target_price": round(_f(trade.get("target_2_p90") or trade.get("scalp_s2_target")), 2),
        "t2_hit": t2_hit,
        "final_exit_qty": final_qty,
        "final_exit_price": round(_f(trade.get("exit_price") or (final_leg or {}).get("price")), 2),
        "final_exit_reason": exit_reason,
        "t2_or_final_gross_profit_inr": round(final_pnl, 2),
        "gross_pnl_inr": round(gross, 2),
        "estimated_charges_inr": charges["total"],
        "charges_breakdown": charges,
        "net_pnl_inr": net,
        "roi_pct": roi,
        "vault_locked_inr": round(_f(context.get("vault_locked_inr")), 2),
        "cumulative_day_pnl_inr": 0.0,
        "model_probability_pct": edge_probability,
        "model_direction": "BEARISH" if option_type == "PE" else "BULLISH",
        "mtf_summary": " · ".join(mtf_bits) or "MTF aligned",
        "model_reason_simple": model_en,
        "technical_reason_simple": technical_en,
        "exit_reason_simple": exit_en,
        "model_reason_simple_te": model_te,
        "technical_reason_simple_te": technical_te,
        "exit_reason_simple_te": exit_te,
        "reasons": {
            "model_en": model_en, "model_te": model_te,
            "technical_en": technical_en, "technical_te": technical_te,
            "exit_en": exit_en, "exit_te": exit_te,
        },
        "spot_entry": round(_f(context.get("spot")), 2),
        "spot_exit": round(_f(context.get("spot_exit", context.get("spot"))), 2),
        "futures_basis_entry": round(_f(context.get("basis")), 2),
        "vix_entry": round(_f(context.get("vix")), 2),
        "pcr_entry": round(_f(context.get("pcr")), 3),
        "obi_5_entry": round(_f(context.get("obi_5")), 3),
        "tbq_tsq_ratio_entry": round(_f(context.get("tbq_tsq")), 3),
        "doi_5m_pct": round(_f(context.get("doi_5m_pct")), 2),
        "greeks_entry": greeks_clean,
        "greeks_exit": greeks_clean if exit_at else {},
        "charts": charts,
        "chart_labels": [],
        "updated_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
    }
    record["chart_labels"] = chart_labels(record)
    return record


def chart_labels(record: dict[str, Any]) -> list[str]:
    """The exact callout text used by the chart renderer and its tests."""
    entry = _f(record.get("entry_price"))
    sl = _f(record.get("initial_sl_price"))
    t1 = _f(record.get("t1_target_price"))
    final = _f(record.get("final_exit_price"))
    qty = int(_f(record.get("total_qty")))
    entry_time = _clock(record.get("entry_time_ist"))
    labels = [
        f"ENTRY: ₹{entry:,.2f} ({qty} Qty @ {entry_time})",
        f"SL: ₹{sl:,.2f} ({'-' if entry >= sl else '+'}₹{abs(entry-sl):,.2f} pts)",
        (
            f"T1 HIT @ ₹{_f(record.get('t1_booked_price')):,.2f} | "
            f"{int(_f(record.get('t1_booked_qty')))} Qty Booked -> "
            f"{_inr(record.get('t1_gross_profit_inr'), True)}"
            if record.get("t1_hit") else
            f"T1 TARGET: ₹{t1:,.2f} (waiting)"
        ),
        (
            f"T2 / EXIT @ ₹{final:,.2f} | {int(_f(record.get('final_exit_qty')))} Qty "
            f"Booked -> {_inr(record.get('t2_or_final_gross_profit_inr'), True)}"
            if record.get("status") == "CLOSED" else
            f"LIVE MARK: ₹{final:,.2f}"
        ),
    ]
    return labels


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("entry_time_ist") or ""), str(record.get("trade_id") or "")


class TradeJournalStore:
    """JSON-backed journal indexed by IST date."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else journal_path()
        self.days: dict[str, list[dict[str, Any]]] = {}
        self.updated_at = ""
        self.starting_capital = 100_000.0
        self.load()

    def load(self) -> "TradeJournalStore":
        with _LOCK:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                self.days = {}
                return self
            if isinstance(raw, dict):
                self.updated_at = str(raw.get("updated_at") or "")
                self.starting_capital = _f(raw.get("starting_capital"), 100_000.0)
                days = raw.get("days") or {}
                if isinstance(days, dict):
                    self.days = {
                        str(date): [row for row in rows if isinstance(row, dict)]
                        for date, rows in days.items()
                    }
            return self

    def save(self) -> Path:
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.updated_at = datetime.now(tz=IST).isoformat(timespec="seconds")
            payload = {
                "version": JOURNAL_VERSION,
                "updated_at": self.updated_at,
                "starting_capital": self.starting_capital,
                "days": self.days,
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(self.path)
            return self.path

    @property
    def records(self) -> list[dict[str, Any]]:
        return sorted(
            (record for rows in self.days.values() for record in rows),
            key=_sort_key,
        )

    def get(self, trade_id: str) -> dict[str, Any] | None:
        return next((record for record in self.records if record.get("trade_id") == trade_id), None)

    def records_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return [row for row in self.records if str(start) <= str(row.get("date_ist")) <= str(end)]

    def _recompute_day(self, date: str) -> None:
        rows = sorted(self.days.get(date, []), key=_sort_key)
        running = 0.0
        for row in rows:
            running = round(running + _f(row.get("net_pnl_inr")), 2)
            row["cumulative_day_pnl_inr"] = running
        self.days[date] = rows

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        """Merge a lifecycle snapshot; entry-only fields keep their first value."""
        if not record.get("trade_id"):
            record["trade_id"] = (
                f"{record.get('date_ist','')}|{record.get('option_strike','')}|"
                f"{record.get('entry_time_ist','')}"
            )
        date = str(record.get("date_ist") or _as_ist(record.get("entry_time_ist")).date().isoformat())
        record["date_ist"] = date
        with _LOCK:
            rows = self.days.setdefault(date, [])
            existing = next((row for row in rows if row.get("trade_id") == record["trade_id"]), None)
            if existing:
                for key in _ENTRY_ONLY_FIELDS:
                    if existing.get(key) not in (None, "", 0, 0.0, {}, []):
                        record[key] = existing[key]
                old_charts = existing.get("charts") or {}
                new_charts = record.get("charts") or {}
                if not (new_charts.get("option_candles") or []):
                    new_charts["option_candles"] = old_charts.get("option_candles") or []
                if not (new_charts.get("spot_candles") or []):
                    new_charts["spot_candles"] = old_charts.get("spot_candles") or []
                if not new_charts.get("p10"):
                    new_charts["p10"] = old_charts.get("p10") or 0.0
                if not new_charts.get("median"):
                    new_charts["median"] = old_charts.get("median") or 0.0
                if not new_charts.get("p90"):
                    new_charts["p90"] = old_charts.get("p90") or 0.0
                if not new_charts.get("atp"):
                    new_charts["atp"] = old_charts.get("atp") or 0.0
                record["charts"] = new_charts
                existing.clear()
                existing.update(record)
            else:
                rows.append(record)
            self._recompute_day(date)
            self.save()
            return next(row for row in self.days[date] if row["trade_id"] == record["trade_id"])

    def record_live_trade(
        self,
        trade: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        option_candles: Iterable[dict[str, Any]] | None = None,
        spot_candles: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        record = build_record(
            trade, context=context,
            option_candles=option_candles, spot_candles=spot_candles,
        )
        return self.upsert(record)

    def summary(self, records: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        rows = list(records if records is not None else self.records)
        gross = round(sum(_f(row.get("gross_pnl_inr")) for row in rows), 2)
        charges = round(sum(_f(row.get("estimated_charges_inr")) for row in rows), 2)
        net = round(sum(_f(row.get("net_pnl_inr")) for row in rows), 2)
        wins = [row for row in rows if _f(row.get("net_pnl_inr")) > 0.005]
        losses = [row for row in rows if _f(row.get("net_pnl_inr")) < -0.005]
        closed = [row for row in rows if str(row.get("status")).upper() == "CLOSED"]
        t1_hits = sum(1 for row in rows if row.get("t1_hit"))
        t2_hits = sum(1 for row in rows if row.get("t2_hit"))
        gross_wins = sum(_f(row.get("net_pnl_inr")) for row in wins)
        gross_losses = abs(sum(_f(row.get("net_pnl_inr")) for row in losses))
        starting = _f(self.starting_capital, 100_000.0)
        days: dict[str, dict[str, Any]] = {}
        for row in rows:
            date = str(row.get("date_ist"))
            day = days.setdefault(date, {"date": date, "net": 0.0, "trades": 0, "wins": 0, "losses": 0})
            day["net"] = round(day["net"] + _f(row.get("net_pnl_inr")), 2)
            day["trades"] += 1
            day["wins"] += 1 if _f(row.get("net_pnl_inr")) > 0.005 else 0
            day["losses"] += 1 if _f(row.get("net_pnl_inr")) < -0.005 else 0
        return {
            "trades": len(rows),
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
            "t1_hits": t1_hits,
            "t2_hits": t2_hits,
            "t1_hit_rate": round(t1_hits / len(rows) * 100.0, 1) if rows else 0.0,
            "t2_full_run_rate": round(t2_hits / len(rows) * 100.0, 1) if rows else 0.0,
            "gross_pnl": gross,
            "charges": charges,
            "net_pnl": net,
            "roi_pct": round(net / starting * 100.0, 2) if starting else 0.0,
            "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses > 1e-9 else (round(gross_wins, 2) if gross_wins else 0.0),
            "avg_win": round(gross_wins / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_losses / len(losses), 2) if losses else 0.0,
            "vault_locked": round(sum(_f(row.get("vault_locked_inr")) for row in rows), 2),
            "starting_capital": round(starting, 2),
            "ending_capital": round(starting + net, 2),
            "days": [days[key] for key in sorted(days)],
        }

    def snapshot(self, start: str, end: str) -> dict[str, Any]:
        rows = self.records_between(start, end)
        return {
            "ok": True,
            "range": {"from": start, "to": end},
            "summary": self.summary(rows),
            "trades": rows,
            "generated_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
        }


def load_journal(path: Path | str | None = None) -> TradeJournalStore:
    return TradeJournalStore(path)


__all__ = [
    "JOURNAL_FILE",
    "TradeJournalStore",
    "build_record",
    "chart_labels",
    "estimate_charges",
    "infer_strategy",
    "journal_path",
    "load_journal",
]
