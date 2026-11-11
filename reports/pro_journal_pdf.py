"""Magazine-quality institutional PDF trading journal.

Supports daily, weekly, monthly and custom ranges.  Page 1 is the executive
performance dashboard, page 2 the complete tabular journal, then one dossier
page per trade with an embedded high-resolution annotated chart, and finally
the missed/filtered setups log.

    from state.trade_journal_store import load_journal
    from reports.pro_journal_pdf import build_journal_pdf

    path = build_journal_pdf(load_journal(), "weekly")
"""

from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from reports.chart_snapshot_renderer import render_equity_curve, render_trade_chart
from state.trade_journal_store import (
    IST,
    _as_ist,
    _f,
    _inr,
    load_journal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "reports" / "output"
PDF_DPI = int(os.environ.get("KRONOS_JOURNAL_DPI") or 300)

PAGE_W, PAGE_H = landscape(A4)
MARGIN_X = 22
CONTENT_W = PAGE_W - MARGIN_X * 2

NAVY = colors.HexColor("#0B1F33")
NAVY_2 = colors.HexColor("#122B44")
GOLD = colors.HexColor("#C9A227")
GOLD_LIGHT = colors.HexColor("#FFF3C4")
INK = colors.HexColor("#102434")
MUTED = colors.HexColor("#5D7488")
GREEN = colors.HexColor("#0A8F5B")
GREEN_LIGHT = colors.HexColor("#E7F6EE")
RED = colors.HexColor("#C62828")
RED_LIGHT = colors.HexColor("#FDEAEA")
BLUE = colors.HexColor("#1565C0")
BLUE_LIGHT = colors.HexColor("#E7F0FB")
ORANGE = colors.HexColor("#E65100")
ORANGE_LIGHT = colors.HexColor("#FFF0E2")
PURPLE = colors.HexColor("#6A1B9A")
PURPLE_LIGHT = colors.HexColor("#F4E9FA")
TEAL = colors.HexColor("#00695C")
TEAL_LIGHT = colors.HexColor("#E0F3F1")
GREY_LIGHT = colors.HexColor("#EEF3F7")
LINE = colors.HexColor("#D5E0E9")

FONT = "KRONOS-DejaVu"
FONT_BOLD = "KRONOS-DejaVu-Bold"
FONT_ITALIC = "KRONOS-DejaVu-Oblique"


def _register_fonts() -> None:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    import matplotlib

    root = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont(FONT, str(root / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(root / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_ITALIC, str(root / "DejaVuSans-Oblique.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT_ITALIC)


def _styles() -> dict[str, ParagraphStyle]:
    _register_fonts()
    return {
        "body": ParagraphStyle("body", fontName=FONT, fontSize=7.5, leading=9.6, textColor=INK),
        "small": ParagraphStyle("small", fontName=FONT, fontSize=6.2, leading=7.5, textColor=INK),
        "tiny": ParagraphStyle("tiny", fontName=FONT, fontSize=5.4, leading=6.5, textColor=INK),
        "muted": ParagraphStyle("muted", fontName=FONT, fontSize=6.4, leading=8, textColor=MUTED),
        "h1": ParagraphStyle("h1", fontName=FONT_BOLD, fontSize=18, leading=21, textColor=NAVY),
        "h2": ParagraphStyle("h2", fontName=FONT_BOLD, fontSize=12.5, leading=15, textColor=NAVY),
        "h3": ParagraphStyle("h3", fontName=FONT_BOLD, fontSize=9, leading=11, textColor=NAVY),
        "card_label": ParagraphStyle("card_label", fontName=FONT_BOLD, fontSize=6.3, leading=7.5, textColor=MUTED),
        "card_value": ParagraphStyle("card_value", fontName=FONT_BOLD, fontSize=13, leading=15, textColor=INK),
        "card_value_big": ParagraphStyle("card_value_big", fontName=FONT_BOLD, fontSize=16, leading=18, textColor=INK),
        "card_sub": ParagraphStyle("card_sub", fontName=FONT, fontSize=5.6, leading=6.7, textColor=MUTED),
        "th": ParagraphStyle("th", fontName=FONT_BOLD, fontSize=5.9, leading=6.9, textColor=colors.white, alignment=TA_CENTER),
        "td": ParagraphStyle("td", fontName=FONT, fontSize=5.8, leading=6.9, textColor=INK),
        "td_right": ParagraphStyle("td_right", fontName=FONT, fontSize=5.8, leading=6.9, textColor=INK, alignment=TA_RIGHT),
        "dossier_label": ParagraphStyle("dossier_label", fontName=FONT_BOLD, fontSize=6.2, leading=7.4, textColor=MUTED),
        "dossier_value": ParagraphStyle("dossier_value", fontName=FONT_BOLD, fontSize=6.5, leading=7.7, textColor=INK),
        "reason_head": ParagraphStyle("reason_head", fontName=FONT_BOLD, fontSize=6.8, leading=8, textColor=NAVY),
        "reason": ParagraphStyle("reason", fontName=FONT, fontSize=6.2, leading=7.5, textColor=INK, spaceAfter=2),
    }


def _short(text: Any, limit: int = 160) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _label_date(day: date) -> str:
    return day.strftime("%d %b %Y")


def resolve_period(
    period: str = "daily",
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Resolve a report scope to inclusive ISO dates and a human label."""
    key = str(period or "daily").strip().lower()
    if key not in {"daily", "weekly", "monthly", "custom"}:
        key = "daily"
    today = _as_ist(now).date()
    anchor = _parse_date(from_date) or today
    if key == "daily":
        start = end = anchor
        label = _label_date(start)
    elif key == "weekly":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        label = f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    elif key == "monthly":
        start = date(anchor.year, anchor.month, 1)
        end = date(anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1])
        label = start.strftime("%B %Y")
    else:
        start = anchor
        end = _parse_date(to_date) or start
        if start > end:
            start, end = end, start
        label = f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    return {
        "period": key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": label,
        "filename": f"KRONOS_Pro_Trading_Journal_{key}_{start.isoformat()}_{end.isoformat()}.pdf",
    }


def load_missed_setups(start: str, end: str) -> list[dict[str, Any]]:
    try:
        from state.paper_trade_ledger import PaperLedger

        ledger = PaperLedger.load()
        return [
            row for row in (setup.as_dict() for setup in ledger.missed)
            if start <= str(row.get("date") or "") <= end
        ]
    except Exception:  # noqa: BLE001 - a missing ledger never blocks the report
        return []


def _image(path: Path, max_width: float, max_height: float) -> Image:
    """Fit an image inside a box without distorting its aspect ratio."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as probe:
            width, height = probe.size
    except Exception:  # noqa: BLE001
        width, height = 4, 3
    scale = min(max_width / width, max_height / height)
    return Image(str(path), width=width * scale, height=height * scale)


def _card(label: str, value: str, sub: str, bg: colors.Color, styles: dict,
          *, value_style: str = "card_value") -> Table:
    inner = Table(
        [
            [Paragraph(label, styles["card_label"])],
            [Paragraph(value, styles[value_style])],
            [Paragraph(sub, styles["card_sub"])],
        ],
        colWidths=[178],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return inner


def _header_flow(scope: dict[str, Any], summary: dict[str, Any], styles: dict,
                 generated: str, mode: str) -> list[Any]:
    title = Paragraph(
        '<font color="#C9A227">KRONOS QUANT DESK</font> '
        '<font color="#FFFFFF">— OFFICIAL PRO TRADING JOURNAL</font>',
        ParagraphStyle("banner", fontName=FONT_BOLD, fontSize=15, leading=18,
                       textColor=colors.white),
    )
    subtitle = Paragraph(
        f"<b>{scope['label']}</b> &nbsp;·&nbsp; Account Mode: <b>{mode}</b> "
        f"&nbsp;·&nbsp; Generated: {generated} IST &nbsp;·&nbsp; "
        f"{summary.get('trades', 0)} executed trade(s)",
        ParagraphStyle("sub", fontName=FONT, fontSize=7.2, leading=9,
                       textColor=colors.HexColor("#C9DCEB")),
    )
    banner = Table([[title], [subtitle]], colWidths=[CONTENT_W])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (0, 0), 10),
        ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (0, 1), 9),
        ("LINEBELOW", (0, -1), (-1, -1), 2, GOLD),
    ]))
    return [banner, Spacer(1, 7)]


def _section_title(number: str, title: str, note: str, styles: dict) -> Table:
    left = Paragraph(
        f'<font color="#C9A227">{number}</font> &nbsp; {title}',
        ParagraphStyle("section", fontName=FONT_BOLD, fontSize=11, leading=13, textColor=NAVY),
    )
    right = Paragraph(note, ParagraphStyle("section_note", fontName=FONT, fontSize=6.5,
                                           leading=8, textColor=MUTED, alignment=TA_RIGHT))
    bar = Table([[left, right]], colWidths=[CONTENT_W * 0.66, CONTENT_W * 0.34])
    bar.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, GOLD),
    ]))
    return bar


def _kpi_grid(summary: dict[str, Any], styles: dict) -> Table:
    net = _f(summary.get("net_pnl"))
    net_bg = GREEN_LIGHT if net >= 0 else RED_LIGHT
    net_color = GREEN if net >= 0 else RED
    net_value = ParagraphStyle("net_value", parent=styles["card_value_big"], textColor=net_color)
    cards = [
        _card("STARTING / ENDING CAPITAL",
              f"{_inr(summary.get('starting_capital'))} → {_inr(summary.get('ending_capital'))}",
              "Base account remains the risk anchor", BLUE_LIGHT, styles),
        _card("TOTAL GROSS P&L", _inr(summary.get("gross_pnl"), True),
              "Before brokerage, STT, GST & stamp duty", GREY_LIGHT, styles),
        _card("BROKERAGE & STATUTORY TAXES", _inr(summary.get("charges")),
              "Upstox flat fee + STT + NSE txn + GST", ORANGE_LIGHT, styles),
        _card("NET REALIZED EARNINGS", _inr(net, True),
              f"{_f(summary.get('roi_pct')):+.2f}% ROI on base capital",
              net_bg, styles, value_style="card_value_big"),
        _card("PROFIT VAULT LOCKED", _inr(summary.get("vault_locked")),
              "Untouchable house-money shield", PURPLE_LIGHT, styles),
        _card("TRADES · WINS / LOSSES", f"{summary.get('trades', 0)} · {summary.get('wins', 0)} / {summary.get('losses', 0)}",
              f"Win rate {_f(summary.get('win_rate')):.1f}%", TEAL_LIGHT, styles),
        _card("T1 HIT RATE · T2 FULL-RUN RATE",
              f"{_f(summary.get('t1_hit_rate')):.0f}% · {_f(summary.get('t2_full_run_rate')):.0f}%",
              f"{summary.get('t1_hits', 0)} T1 / {summary.get('t2_hits', 0)} full runs", GOLD_LIGHT, styles),
        _card("PROFIT FACTOR · AVG WIN / LOSS",
              f"{_f(summary.get('profit_factor')):.2f} · {_inr(summary.get('avg_win'))} / {_inr(summary.get('avg_loss'))}",
              "Realized net rupees per closed trade", BLUE_LIGHT, styles),
    ]
    # The net card gets the strongest border.
    for card in cards:
        card.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINE)]))
    cards[3].setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.4, net_color),
        ("BACKGROUND", (0, 0), (-1, -1), net_bg),
    ]))
    grid = Table(
        [
            [cards[0], cards[1], cards[2], cards[3]],
            [cards[4], cards[5], cards[6], cards[7]],
        ],
        colWidths=[CONTENT_W / 4] * 4,
        rowHeights=[62, 62],
    )
    grid.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("RIGHTPADDING", (0, 0), (-2, -1), 5),
    ]))
    return grid


def _master_table(rows: list[dict[str, Any]], styles: dict) -> Table:
    widths = [17, 55, 76, 72, 23, 38, 38, 50, 55, 38, 48, 288]
    header = [
        "#", "DATE & TIME", "CONTRACT & MODE", "STRATEGY", "QTY", "ENTRY ₹",
        "SL ₹", "T1 BOOKED", "T2 / EXIT", "CHARGES ₹", "NET P&L ₹",
        "WHY TAKEN (MODEL + TECH SUMMARY)",
    ]
    data: list[list[Any]] = [[Paragraph(cell, styles["th"]) for cell in header]]
    for index, row in enumerate(rows, start=1):
        net = _f(row.get("net_pnl_inr"))
        charts = row.get("charts") or {}
        why = (
            f"{_f(row.get('model_probability_pct')):.0f}% "
            f"{str(row.get('model_direction') or '').title()} · "
            f"cone ₹{_f(charts.get('p10')):,.0f}–₹{_f(charts.get('p90')):,.0f} · "
            f"{row.get('mtf_summary') or 'MTF aligned'} · "
            f"OBI {_f(row.get('obi_5_entry')):.2f}x · "
            f"TBQ/TSQ {_f(row.get('tbq_tsq_ratio_entry')):.2f}x"
        )
        t1 = (
            f"{int(_f(row.get('t1_booked_qty')))} @ ₹{_f(row.get('t1_booked_price')):,.2f}\n"
            f"{_inr(row.get('t1_gross_profit_inr'), True)}"
            if row.get("t1_hit") else "—"
        )
        final = (
            f"{int(_f(row.get('final_exit_qty')))} @ ₹{_f(row.get('final_exit_price')):,.2f}\n"
            f"{_inr(row.get('t2_or_final_gross_profit_inr'), True)}"
            if str(row.get("status")).upper() == "CLOSED" else "OPEN"
        )
        data.append([
            Paragraph(str(index), styles["td"]),
            Paragraph(f"{row.get('date_ist')}<br/>{str(row.get('entry_time_ist') or '')[11:16]}", styles["td"]),
            Paragraph(f"{row.get('option_strike')}<br/><font color='#5D7488'>{row.get('mode')}</font>", styles["td"]),
            Paragraph(str(row.get("strategy_tag") or "").replace("_", " ").title(), styles["td"]),
            Paragraph(str(int(_f(row.get("total_qty")))), styles["td_right"]),
            Paragraph(f"₹{_f(row.get('entry_price')):,.2f}", styles["td_right"]),
            Paragraph(f"₹{_f(row.get('initial_sl_price')):,.2f}", styles["td_right"]),
            Paragraph(t1.replace("\n", "<br/>"), styles["td_right"]),
            Paragraph(final.replace("\n", "<br/>"), styles["td_right"]),
            Paragraph(_inr(row.get("estimated_charges_inr")), styles["td_right"]),
            Paragraph(
                f"<font color='{'#0A8F5B' if net >= 0 else '#C62828'}'>"
                f"<b>{_inr(net, True)}</b></font>",
                styles["td_right"],
            ),
            Paragraph(why, styles["tiny"]),
        ])
    if len(data) == 1:
        data.append([Paragraph("No executed trades in this period. Capital was protected — the report still records the risk discipline log.", styles["td"])] + [""] * 11)
    table = Table(data, colWidths=widths, repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0),
    ]
    if len(data) > 1:
        commands.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY_LIGHT]))
    if len(data) == 2:
        commands.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(commands))
    return table


def _totals_table(summary: dict[str, Any], styles: dict) -> Table:
    rows = [
        ("T1 TOTAL PROFIT", _inr(summary.get("t1_gross_profit_inr", 0.0), True)),
        ("T2 / FINAL TOTAL PROFIT", _inr(summary.get("t2_gross_profit_inr", 0.0), True)),
        ("TOTAL CHARGES", _inr(summary.get("charges"))),
        ("GRAND TOTAL NET EARNINGS", _inr(summary.get("net_pnl"), True)),
    ]
    data = [[Paragraph(label, styles["td"]), Paragraph(value, styles["td_right"])] for label, value in rows]
    table = Table(data, colWidths=[170, 140])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (0, 3), (-1, 3), GREEN_LIGHT if _f(summary.get("net_pnl")) >= 0 else RED_LIGHT),
        ("TEXTCOLOR", (0, 3), (-1, 3), GREEN if _f(summary.get("net_pnl")) >= 0 else RED),
        ("FONTNAME", (0, 3), (-1, 3), FONT_BOLD),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _audit_table(row: dict[str, Any], styles: dict) -> Table:
    t1 = (
        f"{int(_f(row.get('t1_booked_qty')))} @ ₹{_f(row.get('t1_booked_price')):,.2f} · {_inr(row.get('t1_gross_profit_inr'), True)}"
        if row.get("t1_hit") else "Not hit"
    )
    final = (
        f"{int(_f(row.get('final_exit_qty')))} @ ₹{_f(row.get('final_exit_price')):,.2f} · {_inr(row.get('t2_or_final_gross_profit_inr'), True)}"
        if str(row.get("status")).upper() == "CLOSED" else "Open"
    )
    greeks = row.get("greeks_entry") or {}
    fields = [
        ("ENTRY / EXIT", f"₹{_f(row.get('entry_price')):,.2f} → ₹{_f(row.get('final_exit_price')):,.2f}"),
        ("DURATION / GEAR", f"{int(_f(row.get('duration_sec')))}s · Gear {int(_f(row.get('max_gear_reached')))}"),
        ("INITIAL SL", f"₹{_f(row.get('initial_sl_price')):,.2f} (-₹{_f(row.get('initial_sl_pts')):,.2f} pts)"),
        ("FINAL TRAIL SL", f"₹{_f(row.get('final_trailed_sl_price')):,.2f}"),
        ("T1 SPLIT", t1),
        ("T2 / FINAL SPLIT", final),
        ("GROSS / CHARGES", f"{_inr(row.get('gross_pnl_inr'), True)} / {_inr(row.get('estimated_charges_inr'))}"),
        ("NET / ROI", f"{_inr(row.get('net_pnl_inr'), True)} / {_f(row.get('roi_pct')):+.2f}%"),
        ("GREEKS Δ Γ Θ V", f"{_f(greeks.get('delta')):.3f} · {_f(greeks.get('gamma')):.4f} · {_f(greeks.get('theta')):.2f} · {_f(greeks.get('vega')):.2f}"),
        ("IV / OBI₅ / TBQ-TSQ", f"{_f(greeks.get('iv')):.1f}% · {_f(row.get('obi_5_entry')):.2f} · {_f(row.get('tbq_tsq_ratio_entry')):.2f}x"),
        ("PCR / VIX / ΔOI 5m", f"{_f(row.get('pcr_entry')):.2f} · {_f(row.get('vix_entry')):.2f} · {_f(row.get('doi_5m_pct')):+.2f}%"),
        ("SPOT / BASIS", f"₹{_f(row.get('spot_entry')):,.2f} → ₹{_f(row.get('spot_exit')):,.2f} · {_f(row.get('futures_basis_entry')):+.2f}"),
    ]
    data = []
    for index in range(0, len(fields), 2):
        left = fields[index]
        right = fields[index + 1] if index + 1 < len(fields) else ("", "")
        data.append([
            Paragraph(left[0], styles["dossier_label"]), Paragraph(left[1], styles["dossier_value"]),
            Paragraph(right[0], styles["dossier_label"]), Paragraph(right[1], styles["dossier_value"]),
        ])
    table = Table(data, colWidths=[78, 112, 74, 116])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _reasons_box(row: dict[str, Any], styles: dict) -> Table:
    content = [
        Paragraph("WHY THIS TRADE WAS TAKEN — MODEL &amp; TECHNICAL", styles["reason_head"]),
        Paragraph(f"<b>Model:</b> {row.get('model_reason_simple') or '—'}", styles["reason"]),
        Paragraph(f"<b>Technical:</b> {row.get('technical_reason_simple') or '—'}", styles["reason"]),
        Paragraph("HOW IT EXITED", styles["reason_head"]),
        Paragraph(row.get("exit_reason_simple") or "—", styles["reason"]),
        Paragraph(
            f"Strategy: <b>{str(row.get('strategy_tag') or '').replace('_', ' ').title()}</b> "
            f"· Mode: <b>{row.get('mode')}</b> · Expiry: {row.get('expiry_date') or '—'}",
            styles["tiny"],
        ),
    ]
    box = Table([[content]], colWidths=[392])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FBFCFE")),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return box


def _dossier(row: dict[str, Any], index: int, chart_dir: Path, styles: dict,
             dpi: int) -> list[Any]:
    net = _f(row.get("net_pnl_inr"))
    color = "#0A8F5B" if net >= 0 else "#C62828"
    heading = Table(
        [[
            Paragraph(
                f"<font color='#C9A227'>TRADE #{index}</font> &nbsp; "
                f"<b>{row.get('option_strike')}</b> &nbsp;·&nbsp; "
                f"{str(row.get('strategy_tag') or '').replace('_', ' ').title()} &nbsp;·&nbsp; "
                f"{row.get('date_ist')} {str(row.get('entry_time_ist') or '')[11:16]}",
                ParagraphStyle("dh", fontName=FONT_BOLD, fontSize=10, leading=12, textColor=NAVY),
            ),
            Paragraph(
                f"<font color='{color}'><b>{_inr(net, True)}</b></font> "
                f"<font color='#5D7488'>({_f(row.get('roi_pct')):+.1f}% ROI)</font>",
                ParagraphStyle("dn", fontName=FONT_BOLD, fontSize=10.5, leading=12,
                               textColor=INK, alignment=TA_RIGHT),
            ),
        ]],
        colWidths=[CONTENT_W * 0.7, CONTENT_W * 0.3],
    )
    heading.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
        ("LINEBELOW", (0, 0), (-1, -1), 1.4, GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    boxes = Table(
        [[_audit_table(row, styles), _reasons_box(row, styles)]],
        colWidths=[CONTENT_W * 0.48, CONTENT_W * 0.52],
    )
    boxes.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-2, -1), 6),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    chart_path = render_trade_chart(
        row, chart_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', str(row.get('trade_id')))[:60]}.png",
        dpi=dpi,
    )
    image = _image(chart_path, CONTENT_W, 322)
    return [heading, Spacer(1, 3), boxes, Spacer(1, 3), image, PageBreak()]


def _missed_table(missed: list[dict[str, Any]], styles: dict) -> Table:
    header = ["TIME", "DATE", "STRIKE", "PLANNED ENTRY", "STATUS", "BLOCK REASON"]
    data: list[list[Any]] = [[Paragraph(cell, styles["th"]) for cell in header]]
    for row in missed:
        data.append([
            Paragraph(str(row.get("time_identified") or "")[11:19] or "—", styles["td"]),
            Paragraph(str(row.get("date") or ""), styles["td"]),
            Paragraph(f"{row.get('symbol') or ''} {row.get('strike') or ''} {row.get('option_type') or ''}".strip(), styles["td"]),
            Paragraph(f"₹{_f(row.get('planned_entry')):,.2f}", styles["td_right"]),
            Paragraph(str(row.get("status") or "Watching"), styles["td"]),
            Paragraph(_short(row.get("reason") or "Risk gate did not clear", 110), styles["td"]),
        ])
    if len(data) == 1:
        data.append([Paragraph("No filtered setups in this period. Every identified setup either triggered or never reached the entry gate.", styles["td"])] + [""] * 5)
    table = Table(data, colWidths=[55, 62, 105, 65, 105, CONTENT_W - 392], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _decorate_page(canvas, doc) -> None:  # noqa: ANN001 - reportlab callback
    canvas.saveState()
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(0.8)
    canvas.line(MARGIN_X, 18, PAGE_W - MARGIN_X, 18)
    canvas.setFont(FONT, 6.2)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN_X, 9, "KRONOS QUANT DESK · CONFIDENTIAL · Institutional Pro Trading Journal")
    canvas.drawRightString(PAGE_W - MARGIN_X, 9, f"Page {doc.page}")
    canvas.restoreState()


def build_journal_pdf(
    store,
    period: str = "daily",
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    output_dir: Path | str | None = None,
    chart_dpi: int | None = None,
    missed: list[dict[str, Any]] | None = None,
    now: datetime | str | None = None,
) -> Path:
    """Build the complete multi-page report and return its path."""
    styles = _styles()
    scope = resolve_period(period, from_date, to_date, now=now)
    rows = store.records_between(scope["start"], scope["end"])
    summary = store.summary(rows)
    starting = _f(store.starting_capital, 100_000.0)
    summary["starting_capital"] = starting
    summary["ending_capital"] = round(starting + _f(summary.get("net_pnl")), 2)
    summary["roi_pct"] = round(_f(summary.get("net_pnl")) / starting * 100.0, 2) if starting else 0.0
    summary["t1_gross_profit_inr"] = round(sum(_f(r.get("t1_gross_profit_inr")) for r in rows), 2)
    summary["t2_gross_profit_inr"] = round(sum(_f(r.get("t2_or_final_gross_profit_inr")) for r in rows), 2)

    out_root = Path(output_dir or os.environ.get("KRONOS_JOURNAL_OUTPUT") or OUTPUT_DIR)
    out_root.mkdir(parents=True, exist_ok=True)
    chart_dir = out_root / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    equity_path = render_equity_curve(
        rows, summary, chart_dir / f"equity_{scope['period']}_{scope['start']}_{scope['end']}.png"
    )
    target = out_root / scope["filename"]
    generated = datetime.now(tz=IST).strftime("%d %b %Y %H:%M")
    modes = sorted({str(row.get("mode") or "PAPER") for row in rows}) or ["PAPER"]
    mode_label = " + ".join(modes)

    doc = SimpleDocTemplate(
        str(target), pagesize=landscape(A4),
        leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=20, bottomMargin=26,
        title=f"KRONOS Pro Trading Journal — {scope['label']}",
        author="KRONOS Quant Desk", subject="Institutional trading journal",
    )
    story: list[Any] = []
    story += _header_flow(scope, summary, styles, generated, mode_label)
    story.append(_kpi_grid(summary, styles))
    story.append(Spacer(1, 8))
    story.append(_section_title("01", "EQUITY CURVE &amp; DAY-BY-DAY EARNINGS", "NET OF ALL CHARGES · IST", styles))
    story.append(Spacer(1, 4))
    story.append(_image(equity_path, CONTENT_W, 238))
    story.append(PageBreak())

    story.append(_section_title(
        "02", "MASTER TABULAR TRADE JOURNAL",
        f"{len(rows)} TRADE(S) · T1 / T2 / SL SPLIT · NET EARNINGS", styles,
    ))
    story.append(Spacer(1, 5))
    story.append(_master_table(rows, styles))
    story.append(Spacer(1, 6))
    totals_holder = Table([[Spacer(1, 1), _totals_table(summary, styles)]],
                          colWidths=[CONTENT_W - 315, 315])
    totals_holder.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(totals_holder)
    story.append(PageBreak())

    story.append(_section_title(
        "03", "INDIVIDUAL TRADE DOSSIERS &amp; 8K ANNOTATED CHARTS",
        "ENTRY · SL · T1 · T2/EXIT ON EVERY PICTURE", styles,
    ))
    story.append(Spacer(1, 5))
    if rows:
        for index, row in enumerate(rows, start=1):
            story += _dossier(row, index, chart_dir, styles, int(chart_dpi or PDF_DPI))
    else:
        story.append(Paragraph(
            "No executed trades in this period. The next dossier will appear here "
            "automatically after the live session books its first T1.",
            styles["body"],
        ))
        story.append(PageBreak())

    if missed is None:
        missed = load_missed_setups(scope["start"], scope["end"])
    story.append(_section_title(
        "04", "MISSED / FILTERED SETUPS &amp; RISK DISCIPLINE LOG",
        f"{len(missed)} BLOCKED SETUP(S) · CAPITAL PROTECTED", styles,
    ))
    story.append(Spacer(1, 5))
    story.append(_missed_table(missed, styles))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "This journal is generated from the KRONOS institutional trade store. "
        "Charges are estimates (Upstox flat brokerage, STT, NSE transaction, SEBI, "
        "GST and stamp duty); the broker contract note is authoritative. "
        "All times are IST. Paper and Real trades are labelled individually.",
        styles["muted"],
    ))

    doc.build(story, onFirstPage=_decorate_page, onLaterPages=_decorate_page)
    return target


def generate_journal_pdf(
    period: str = "daily",
    from_date: str | None = None,
    to_date: str | None = None,
    *,
    journal_file: Path | str | None = None,
    output_dir: Path | str | None = None,
    chart_dpi: int | None = None,
) -> Path:
    store = load_journal(journal_file)
    return build_journal_pdf(
        store, period, from_date, to_date,
        output_dir=output_dir, chart_dpi=chart_dpi,
    )


__all__ = [
    "OUTPUT_DIR",
    "build_journal_pdf",
    "generate_journal_pdf",
    "load_missed_setups",
    "resolve_period",
]
