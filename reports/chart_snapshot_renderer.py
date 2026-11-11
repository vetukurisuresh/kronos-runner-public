"""High-resolution annotated trade replay charts for the pro journal.

The renderer is fed the self-contained candle window stored in
``state/journal_history.json`` so a report can be rebuilt months later without
the live socket or the broker.  Charts are PNG (450 DPI capable) with crisp
entry/SL/T1/exit callouts, an ATP line, volume and the KRONOS p10-p90 cone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from state.trade_journal_store import _candles, _f, _inr, chart_labels  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "reports" / "charts"
BG = "#08111A"
PANEL = "#0C1823"
GRID = "#1B2C3A"
TEXT = "#E8F1FA"
MUTED = "#8FA9BE"
GREEN = "#00C853"
RED = "#FF1744"
BLUE = "#2979FF"
GOLD = "#FFC400"
PURPLE = "#B388FF"
CYAN = "#00E5FF"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.facecolor": PANEL,
    "figure.facecolor": BG,
    "savefig.facecolor": BG,
    "text.color": TEXT,
    "axes.labelcolor": TEXT,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.edgecolor": GRID,
    "grid.color": GRID,
})


def _hhmm(value: Any) -> str:
    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1][:5]
    match = __import__("re").search(r"(\d{1,2}):(\d{2})", text)
    return f"{match.group(1).zfill(2)}:{match.group(2)}" if match else ""


def _draw_candles(ax, candles: list[dict[str, Any]]) -> None:
    width = 0.62
    for index, candle in enumerate(candles):
        opening, high, low, close = (
            candle["open"], candle["high"], candle["low"], candle["close"]
        )
        color = GREEN if close >= opening else RED
        ax.vlines(index, low, high, color=color, linewidth=0.9, zorder=4)
        body_low, body_high = sorted((opening, close))
        height = max(body_high - body_low, 0.01)
        ax.add_patch(Rectangle(
            (index - width / 2, body_low), width, height,
            facecolor=color, edgecolor=color, linewidth=0.5, zorder=5,
        ))


def _atp_series(candles: list[dict[str, Any]], fallback: float) -> list[float]:
    series: list[float] = []
    value = fallback
    total_volume = 0.0
    total_value = 0.0
    for candle in candles:
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3.0
        volume = max(candle.get("volume", 0.0), 0.0)
        total_volume += volume
        total_value += typical * volume
        value = (total_value / total_volume) if total_volume > 0 else fallback or typical
        series.append(value)
    return series


def _callout(ax, x: float, y: float, text: str, color: str, *, xytext, ha: str = "left") -> None:
    ax.annotate(
        text, xy=(x, y), xytext=xytext, textcoords="data",
        fontsize=7.4, fontweight="bold", color="#04121C", ha=ha, va="center",
        bbox=dict(boxstyle="round,pad=0.38", facecolor=color, edgecolor="white", linewidth=0.6),
        arrowprops=dict(arrowstyle="-|>", color=color, linewidth=1.0, shrinkA=2, shrinkB=2),
        zorder=20,
    )


def render_trade_chart(
    record: dict[str, Any],
    out_path: Path | str | None = None,
    *,
    dpi: int | None = None,
    width: float = 14.0,
    height: float = 6.3,
) -> Path:
    """Render one trade to an ultra-crisp annotated PNG and return its path."""
    dpi = int(dpi or os.environ.get("KRONOS_JOURNAL_DPI") or 300)
    charts = record.get("charts") or {}
    candles = _candles(charts.get("option_candles"), 45)
    spot = _candles(charts.get("spot_candles"), 30)
    labels = record.get("chart_labels") or chart_labels(record)
    entry = _f(record.get("entry_price"))
    sl = _f(record.get("initial_sl_price"))
    t1 = _f(record.get("t1_target_price"))
    final = _f(record.get("final_exit_price"))
    trailing = _f(record.get("final_trailed_sl_price"))
    if not final:
        final = _f((charts.get("option_candles") or [{}])[-1].get("close"))
    atp_fallback = _f(charts.get("atp"))
    p10, median, p90 = _f(charts.get("p10")), _f(charts.get("median")), _f(charts.get("p90"))

    fig = plt.figure(figsize=(width, height), dpi=dpi)
    grid = fig.add_gridspec(
        3, 1, height_ratios=[4.6, 1.35, 1.0], hspace=0.08,
        left=0.055, right=0.985, top=0.86, bottom=0.075,
    )
    ax = fig.add_subplot(grid[0])
    ax_spot = fig.add_subplot(grid[1], sharex=ax)
    ax_vol = fig.add_subplot(grid[2], sharex=ax)

    count = len(candles) if candles else 10
    last_x = max(count - 1, 1)
    if candles:
        _draw_candles(ax, candles)
        atp = _atp_series(candles, atp_fallback)
        ax.plot(range(count), atp, color=BLUE, linewidth=1.5, label="ATP (VWAP)", zorder=8)
        for index, candle in enumerate(candles):
            volume = max(candle.get("volume", 0.0), 0.0)
            ax_vol.bar(
                index, volume, width=0.62,
                color=GREEN if candle["close"] >= candle["open"] else RED, alpha=0.85,
            )
        prices = [value for candle in candles for value in (candle["high"], candle["low"])]
    else:
        prices = [price for price in (entry, sl, t1, final) if price > 0] or [100.0]
        ax.text(
            0.5, 0.52, "Candle snapshot not captured for this legacy trade\n"
            "Entry / SL / T1 / exit levels remain exact",
            transform=ax.transAxes, ha="center", va="center",
            color=MUTED, fontsize=9, fontweight="bold",
        )
        ax_vol.text(0.5, 0.5, "Volume snapshot unavailable", transform=ax_vol.transAxes,
                    ha="center", va="center", color=MUTED, fontsize=7)

    level_min = min([price for price in (entry, sl, t1, final, trailing, *prices) if price > 0] or [0])
    level_max = max([price for price in (entry, sl, t1, final, trailing, *prices) if price > 0] or [1])
    span = max(level_max - level_min, 1.0)
    ax.set_ylim(level_min - span * 0.16, level_max + span * 0.22)
    ax.set_xlim(-1.2, last_x + 2.8)
    ax.grid(True, axis="y", alpha=0.35, linewidth=0.5)
    ax.set_ylabel("Option Premium (₹)", fontsize=8, color=MUTED)
    ax.tick_params(labelbottom=False, labelsize=7)

    # Exact trade levels with readable callouts.  Each line spans the duration.
    levels = [
        ("ENTRY", entry, BLUE, "-", 1.5),
        ("INITIAL SL", sl, RED, "--", 1.2),
        ("T1", t1, GREEN, ":", 1.4),
        ("FINAL EXIT", final, PURPLE, "-", 1.6),
    ]
    for name, value, color, style, linewidth in levels:
        if value <= 0:
            continue
        ax.hlines(value, 0, last_x, color=color, linestyle=style, linewidth=linewidth,
                  alpha=0.95, zorder=10)
        ax.text(last_x + 0.15, value, f"{name} ₹{value:,.2f}", color=color, fontsize=7,
                fontweight="bold", va="center")
    if trailing > 0 and abs(trailing - sl) > 0.01:
        ax.hlines(trailing, 0, last_x, color=GOLD, linestyle="-.", linewidth=1.0, alpha=0.9)
        ax.text(last_x + 0.15, trailing, f"TRAIL ₹{trailing:,.2f}", color=GOLD,
                fontsize=6.6, va="center")

    # Callout anchors follow the actual T1/exit candles when available.
    t1_x = min(last_x, max(1, int(count * 0.62)))
    exit_x = max(2, last_x - 1)
    if candles:
        t1_time = _hhmm(record.get("t1_hit_time"))
        if t1_time:
            match = next((i for i, c in enumerate(candles)
                          if _hhmm(c.get("time")) == t1_time), None)
            if match is not None:
                t1_x = match
    top_y = ax.get_ylim()[1]
    bottom_y = ax.get_ylim()[0]
    _callout(
        ax, 0, entry, labels[0], BLUE,
        xytext=(0.4, entry + span * 0.10 if entry < top_y - span * 0.2 else entry - span * 0.13),
    )
    _callout(
        ax, 0, sl, labels[1], RED,
        xytext=(0.4, sl - span * 0.12 if sl > bottom_y + span * 0.2 else sl + span * 0.12),
    )
    _callout(
        ax, t1_x, t1 if t1 > 0 else entry, labels[2],
        GREEN if record.get("t1_hit") else "#0E7A46",
        xytext=(t1_x + 0.3, min(top_y - span * 0.08, max(t1, entry) + span * 0.17)),
    )
    _callout(
        ax, exit_x, final if final > 0 else entry, labels[3], PURPLE,
        xytext=(max(0.4, exit_x - 3.4), max(bottom_y + span * 0.10, final - span * 0.17)),
    )

    if p10 > 0 and p90 > p10:
        ax.axhspan(p10, p90, color=GOLD, alpha=0.045, zorder=0)
    ax.legend(loc="upper left", fontsize=6.8, facecolor=PANEL, edgecolor=GRID,
              labelcolor=TEXT, framealpha=0.9)

    # Spot 5m replay + probability cone.
    if spot:
        ax_spot.plot(range(len(spot)), [row["close"] for row in spot], color=CYAN,
                     linewidth=1.2, label="Spot 5m")
        if p10 > 0 and p90 > p10:
            ax_spot.axhspan(p10, p90, color=GOLD, alpha=0.10)
            ax_spot.axhline(median, color=GOLD, linestyle="--", linewidth=0.9)
            ax_spot.axhline(p10, color=GOLD, linestyle=":", linewidth=0.7)
            ax_spot.axhline(p90, color=GOLD, linestyle=":", linewidth=0.7)
        ax_spot.grid(True, alpha=0.25, linewidth=0.4)
        ax_spot.set_ylabel("Spot / Cone", fontsize=7, color=MUTED)
        ax_spot.tick_params(labelbottom=False, labelsize=6.5)
        ax_spot.legend(loc="upper left", fontsize=6, facecolor=PANEL, edgecolor=GRID,
                       labelcolor=TEXT)
    else:
        ax_spot.text(0.5, 0.5, "KRONOS p10–median–p90 cone snapshot unavailable",
                     transform=ax_spot.transAxes, ha="center", va="center",
                     color=MUTED, fontsize=6.5)
        ax_spot.set_yticks([])

    ax_vol.grid(True, alpha=0.20, linewidth=0.4)
    ax_vol.set_ylabel("Volume", fontsize=6.6, color=MUTED)
    if candles:
        step = max(1, count // 8)
        ticks = list(range(0, count, step))
        ax_vol.set_xticks(ticks)
        ax_vol.set_xticklabels([_hhmm(candles[i].get("time")) for i in ticks],
                               fontsize=6.4, rotation=0)
    else:
        ax_vol.set_xticks([])
    for axis in (ax, ax_spot, ax_vol):
        for spine in axis.spines.values():
            spine.set_color(GRID)

    net = _f(record.get("net_pnl_inr"))
    roi = _f(record.get("roi_pct"))
    banner = (
        f"{record.get('option_strike') or '—'} · TOTAL TRADE NET P&L: "
        f"{_inr(net, True)} ({roi:+.1f}% ROI) · Strategy: "
        f"{str(record.get('strategy_tag') or '').replace('_', ' ').title()}"
    )
    fig.text(0.985, 0.955, banner, ha="right", va="top", fontsize=9.2,
             fontweight="bold", color=GOLD,
             bbox=dict(boxstyle="round,pad=0.45", facecolor="#122131",
                       edgecolor=GOLD, linewidth=0.7))
    trade_ref = str(record.get("trade_id") or "")
    if len(trade_ref) > 22:
        trade_ref = trade_ref[:8]
    title = (
        f"KRONOS PRO TRADE REPLAY  ·  #{trade_ref}  ·  "
        f"{record.get('date_ist') or ''}  ·  {record.get('mode') or 'PAPER'}"
    )
    fig.text(0.055, 0.955, title, ha="left", va="top", fontsize=9.5,
             fontweight="bold", color=TEXT)
    fig.text(0.055, 0.918,
             f"{labels[0]}   |   {labels[1]}   |   {labels[2]}   |   {labels[3]}",
             ha="left", va="top", fontsize=6.8, color=MUTED)

    target = Path(out_path) if out_path else (
        CHART_DIR / f"{str(record.get('trade_id') or 'trade')[:48]}.png"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=dpi, facecolor=BG, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return target


def render_equity_curve(
    records: Iterable[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    out_path: Path | str | None = None,
    *,
    dpi: int = 220,
) -> Path:
    """Cumulative equity line plus daily net P&L bars for page 1."""
    rows = list(records)
    summary = summary or {}
    days = summary.get("days") or []
    if rows:
        labels = [f"#{i + 1}" for i in range(len(rows))]
        running = 0.0
        cumulative = []
        for row in rows:
            running = round(running + _f(row.get("net_pnl_inr")), 2)
            cumulative.append(running)
    else:
        labels, cumulative = ["No trades"], [0.0]
    if not days:
        days = [{"date": "—", "net": 0.0}]

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(12.2, 3.5), dpi=dpi,
        gridspec_kw={"height_ratios": [2.2, 1.1], "hspace": 0.55},
    )
    fig.patch.set_facecolor("#FFFFFF")
    for axis in (ax, ax2):
        axis.set_facecolor("#F7FAFD")
        axis.grid(True, color="#D9E4EE", linewidth=0.7)
        axis.tick_params(colors="#324A5E", labelsize=7)
        for spine in axis.spines.values():
            spine.set_color("#C9D8E4")

    positive = cumulative[-1] >= 0 if cumulative else True
    line_color = "#0A8F5B" if positive else "#C62828"
    ax.plot(range(len(cumulative)), cumulative, color=line_color, linewidth=2.1,
            marker="o", markersize=3.4, markerfacecolor=line_color)
    ax.fill_between(range(len(cumulative)), cumulative, 0, color=line_color, alpha=0.10)
    ax.axhline(0, color="#78909C", linewidth=0.8)
    ax.set_title("Cumulative Net Equity Curve (₹)", loc="left", fontsize=9.5,
                 fontweight="bold", color="#0B2239")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=6.4, rotation=0)
    ax.set_ylabel("₹", fontsize=7, color="#324A5E")

    day_labels = [str(row.get("date"))[-5:] for row in days]
    day_values = [_f(row.get("net")) for row in days]
    bars = ax2.bar(range(len(day_values)), day_values, width=0.55,
                   color=["#0A8F5B" if value >= 0 else "#C62828" for value in day_values])
    ax2.axhline(0, color="#78909C", linewidth=0.8)
    ax2.set_title("Day-by-Day Net Earnings (₹)", loc="left", fontsize=9.5,
                  fontweight="bold", color="#0B2239")
    ax2.set_xticks(range(len(day_labels)))
    ax2.set_xticklabels(day_labels, fontsize=6.8)
    for bar, value in zip(bars, day_values):
        ax2.annotate(f"{value:+,.0f}", (bar.get_x() + bar.get_width() / 2, value),
                     ha="center", va="bottom" if value >= 0 else "top",
                     fontsize=6.2, color="#0B2239", fontweight="bold")

    target = Path(out_path) if out_path else CHART_DIR / "equity_curve.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=dpi, facecolor="#FFFFFF", bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return target


__all__ = ["CHART_DIR", "render_equity_curve", "render_trade_chart"]
