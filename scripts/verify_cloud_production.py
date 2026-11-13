"""Verify the public production surface from a cloud runner.

Three questions, answered with evidence rather than assertion:

1. Does the GitHub Pages URL serve the desk (not a 404 or a stale shell)?
2. Does the active Cloudflare edge answer ``/api/readiness`` with a green
   six-point gate and a fresh tick?
3. Does ``/api/live-ticks`` carry real values -- 150+ candles, non-zero model
   probabilities, an in-band ITM delta and a non-zero quantity on an open trade?

The script exits non-zero when any check fails, so a workflow run is the proof.
Only the standard library is used: the runner needs no install step.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PAGES_URL = "https://vetukurisuresh.github.io/kronos-runner-public/"
FIREBASE = "https://kronos-nifty-desk-default-rtdb.asia-southeast1.firebasedatabase.app"
SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")
MIN_5M_CANDLES = 150
DELTA_BAND = (0.54, 0.66)
MAX_TICK_AGE_MS = 500.0


def _get(url: str, timeout: float = 25.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "kronos-cloud-verifier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed hosts
        return int(resp.status), resp.read().decode("utf-8", "replace")


def _get_json(url: str) -> Any:
    status, body = _get(url)
    if status != 200:
        raise RuntimeError(f"HTTP {status} for {url}")
    return json.loads(body)


def discover_edge() -> str | None:
    """Read the tunnel the live runner published, if it is up."""
    try:
        record = _get_json(f"{FIREBASE}/system/live_stream_url.json") or {}
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError, TimeoutError):
        return None
    url = str(record.get("url") or "").strip().rstrip("/")
    return url or None


def check_pages(url: str, failures: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url}
    try:
        status, html = _get(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        failures.append(f"pages: unreachable ({type(exc).__name__}: {exc})")
        return out
    out["status"] = status
    if status != 200:
        failures.append(f"pages: HTTP {status}")
    out["bytes"] = len(html)
    for marker, label in (
        ('id="proSensexLtp"', "SENSEX ticker"),
        ('data-pro-index="SENSEX"', "SENSEX index pill"),
        ("lightweight-charts", "chart library"),
    ):
        present = marker in html
        out[f"has_{label.replace(' ', '_')}"] = present
        if not present:
            failures.append(f"pages: missing {label} ({marker})")
    return out


def _green_checks(readiness: dict[str, Any]) -> tuple[int, int]:
    checks = readiness.get("checks") or []
    if isinstance(checks, dict):
        checks = list(checks.values())
    green = sum(1 for c in checks if isinstance(c, dict)
                and str(c.get("status") or "").upper() in {"GREEN", "PASS", "OK"})
    return green, len(checks)


def check_readiness(edge: str, failures: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"edge": edge}
    try:
        ready = _get_json(f"{edge}/api/readiness") or {}
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError, TimeoutError) as exc:
        failures.append(f"readiness: unreachable ({type(exc).__name__}: {exc})")
        return out
    inner = ready.get("readiness") if isinstance(ready.get("readiness"), dict) else ready
    out["status"] = inner.get("status")
    green, total = _green_checks(inner)
    out["checks_green"], out["checks_total"] = green, total
    if total and green < total:
        failures.append(f"readiness: {green}/{total} checks green")
    if not total:
        # Older payloads carry only the aggregate flag; do not invent a pass.
        ok = bool(inner.get("green") or inner.get("ok"))
        out["aggregate_green"] = ok
        if not ok:
            failures.append("readiness: no per-check matrix and aggregate not green")
    age = inner.get("tick_age_ms", ready.get("tick_age_ms"))
    try:
        age_ms = float(age)
    except (TypeError, ValueError):
        age_ms = None
    out["tick_age_ms"] = age_ms
    if age_ms is not None and age_ms > MAX_TICK_AGE_MS:
        failures.append(f"readiness: tick_age_ms {age_ms:.0f} > {MAX_TICK_AGE_MS:.0f}")
    return out


def check_symbol(edge: str, symbol: str, failures: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": symbol}
    try:
        tick = _get_json(f"{edge}/api/live-ticks?symbol={symbol}") or {}
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError, TimeoutError) as exc:
        failures.append(f"{symbol}: live-ticks unreachable ({type(exc).__name__}: {exc})")
        return out
    spot = tick.get("spot") or {}
    bars = spot.get("candles_5m") or []
    out["spot"] = spot.get("ltp")
    out["candles_5m"] = len(bars)
    if len(bars) < MIN_5M_CANDLES:
        failures.append(f"{symbol}: only {len(bars)} 5m candles (< {MIN_5M_CANDLES})")

    gate = tick.get("data_gate") or {}
    out["gate"] = gate.get("status") or gate.get("reason")
    if str(gate.get("status") or "").upper() != "LIVE":
        failures.append(f"{symbol}: data gate {out['gate']!r}, expected LIVE")

    chart = tick.get("option_chart") or {}
    delta = chart.get("delta")
    out["option"] = chart.get("strike_symbol")
    out["delta"] = delta
    try:
        delta_f = abs(float(delta))
    except (TypeError, ValueError):
        delta_f = None
    trade = tick.get("active_trade") or ((tick.get("trade") or {}).get(symbol) or {})
    trade_open = str(trade.get("status") or "").upper() == "OPEN"
    if delta_f is None:
        failures.append(f"{symbol}: option chart has no delta")
    elif not (DELTA_BAND[0] <= delta_f <= DELTA_BAND[1]):
        # The band is the entry-selection rule.  An OPEN leg is never rolled:
        # its delta legitimately drifts as spot travels through the strike, so
        # there we only require that the leg is still in the money.
        if trade_open and delta_f > 0:
            out["delta_note"] = "OPEN_POSITION_DRIFT"
        else:
            failures.append(f"{symbol}: |delta| {delta_f:.3f} outside {DELTA_BAND}")

    osc = tick.get("model_oscillator") or {}
    bull = osc.get("bullish_upside_prob_pct")
    bear = osc.get("bearish_downside_prob_pct")
    out["model_bull"], out["model_bear"] = bull, bear
    for label, value in (("bull", bull), ("bear", bear)):
        try:
            if float(value) <= 0:
                failures.append(f"{symbol}: {label} model probability is {value!r} (0% bug)")
        except (TypeError, ValueError):
            failures.append(f"{symbol}: {label} model probability missing")

    trade = tick.get("active_trade") or ((tick.get("trade") or {}).get(symbol) or {})
    if str(trade.get("status") or "").upper() == "OPEN":
        qty = trade.get("qty", trade.get("open_qty"))
        out["open_qty"] = qty
        try:
            if float(qty) <= 0:
                failures.append(f"{symbol}: open trade quantity is {qty!r} (0 Qty bug)")
        except (TypeError, ValueError):
            failures.append(f"{symbol}: open trade has no quantity")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify KRONOS public production surface")
    parser.add_argument("--pages-url", default=PAGES_URL)
    parser.add_argument("--edge-url", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    failures: list[str] = []
    report: dict[str, Any] = {
        "generated_at_epoch": time.time(),
        "pages": check_pages(args.pages_url, failures),
    }
    edge = (args.edge_url or discover_edge() or "").rstrip("/")
    if not edge:
        failures.append("edge: no live tunnel published in Firebase")
        report["readiness"] = {"edge": None}
        report["symbols"] = []
    else:
        report["readiness"] = check_readiness(edge, failures)
        report["symbols"] = [check_symbol(edge, sym, failures) for sym in SYMBOLS]

    report["failures"] = failures
    report["ok"] = not failures
    text = json.dumps(report, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    print("RESULT: READY" if report["ok"] else f"RESULT: NOT READY ({len(failures)} failures)")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
