#!/usr/bin/env python3
"""Real-market KRONOS forecast bridge — runs on the GitHub 16 GB runner only.

Fetches live 5-minute candles for NIFTY 50 (``^NSEI``) and BANK NIFTY
(``^NSEBANK``) from Yahoo's chart endpoint (the same feed ``yfinance`` wraps, but
without the brittle dependency), runs the three-model ensemble, walks the last
five sessions forward, and writes a compact JSON artifact for the local
dashboard.

Nothing here runs on a laptop: the models and torch live only on the runner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from agent.kronos_ensemble import (  # noqa: E402
    CONE_TIGHT_RATIO,
    KronosTrioEnsemble,
    decide_trio,
)

SYMBOLS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
DISPLAY = {"NIFTY": "NIFTY 50", "BANKNIFTY": "BANK NIFTY"}
IST = timezone(timedelta(hours=5, minutes=30))
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LOOKBACK = 512
HORIZON = 12


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #


def _parse_chart(payload: dict) -> tuple[list[dict], float]:
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    stamps = result.get("timestamp") or []
    quote_block = (result.get("indicators", {}).get("quote") or [{}])[0]
    candles: list[dict] = []
    for index, stamp in enumerate(stamps):
        try:
            open_ = quote_block["open"][index]
            high = quote_block["high"][index]
            low = quote_block["low"][index]
            close = quote_block["close"][index]
        except (KeyError, IndexError, TypeError):
            continue
        if None in (open_, high, low, close):
            continue
        volume = quote_block.get("volume", [0] * len(stamps))[index] or 0
        candles.append(
            {
                "t": int(stamp),
                "o": round(float(open_), 2),
                "h": round(float(high), 2),
                "l": round(float(low), 2),
                "c": round(float(close), 2),
                "v": int(volume),
            }
        )
    spot = float(meta.get("regularMarketPrice") or (candles[-1]["c"] if candles else 0.0))
    return candles, spot


def fetch_candles(symbol: str, interval: str = "5m", range_: str = "15d") -> tuple[list[dict], float]:
    """Fetch real candles from Yahoo, retrying across both API hosts."""
    params = {"interval": interval, "range": range_, "includePrePost": "false"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    problems: list[str] = []
    for host in YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}"
        for attempt in range(3):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=30)
                if response.status_code != 200:
                    problems.append(f"{host} HTTP {response.status_code}")
                    time.sleep(2 + attempt * 2)
                    continue
                candles, spot = _parse_chart(response.json())
                if len(candles) < 60:
                    problems.append(f"{host} returned only {len(candles)} candles")
                    time.sleep(2)
                    continue
                return candles, spot
            except Exception as exc:  # noqa: BLE001 - network shapes vary
                problems.append(f"{host} {type(exc).__name__}: {exc}")
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"could not fetch {symbol} {interval}: {'; '.join(problems[-4:])}")


def to_frame(candles: list[dict], limit: int = LOOKBACK):
    import pandas as pd

    window = candles[-limit:]
    frame = pd.DataFrame(
        {
            "open": [c["o"] for c in window],
            "high": [c["h"] for c in window],
            "low": [c["l"] for c in window],
            "close": [c["c"] for c in window],
            "volume": [c["v"] for c in window],
            "amount": [c["c"] * max(c["v"], 1) for c in window],
        }
    )
    stamps = pd.Series(pd.to_datetime([c["t"] for c in window], unit="s", utc=True).tz_convert("Asia/Kolkata"))
    return frame, stamps


# --------------------------------------------------------------------------- #
# derived analytics
# --------------------------------------------------------------------------- #


def vwap_series(candles: list[dict]) -> list[float]:
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    out: list[float] = []
    for candle in candles:
        typical = (candle["h"] + candle["l"] + candle["c"]) / 3.0
        volume = max(candle["v"], 1)
        cumulative_pv += typical * volume
        cumulative_volume += volume
        out.append(round(cumulative_pv / cumulative_volume, 2))
    return out


def atr(candles: list[dict], period: int = 14) -> float:
    window = candles[-period - 1:]
    ranges = [window[i]["h"] - window[i]["l"] for i in range(1, len(window))]
    return statistics.fmean(ranges) if ranges else 0.0


def swing_clusters(candles: list[dict], span: int = 4) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    for index in range(span, len(candles) - span):
        neighbourhood = candles[index - span:index + span + 1]
        if candles[index]["h"] >= max(c["h"] for c in neighbourhood):
            highs.append(candles[index]["h"])
        if candles[index]["l"] <= min(c["l"] for c in neighbourhood):
            lows.append(candles[index]["l"])

    def cluster(levels: list[float], tolerance: float) -> list[float]:
        grouped: list[list[float]] = []
        for level in sorted(levels):
            if grouped and abs(level - grouped[-1][-1]) <= tolerance:
                grouped[-1].append(level)
            else:
                grouped.append([level])
        return [round(statistics.fmean(group), 2) for group in grouped]

    tolerance = max(atr(candles) * 0.6, 1.0)
    return cluster(highs, tolerance), cluster(lows, tolerance)


def order_blocks(candles: list[dict], limit: int = 6) -> list[dict]:
    average_range = atr(candles)
    if average_range <= 0:
        return []
    blocks: list[dict] = []
    for index in range(1, len(candles)):
        previous, current = candles[index - 1], candles[index]
        body = abs(current["c"] - current["o"])
        if body < average_range * 0.9:
            continue
        bullish = current["c"] > current["o"] and previous["c"] < previous["o"]
        bearish = current["c"] < current["o"] and previous["c"] > previous["o"]
        if bullish or bearish:
            blocks.append(
                {
                    "type": "bullish" if bullish else "bearish",
                    "top": round(max(previous["o"], previous["c"]), 2),
                    "bottom": round(min(previous["o"], previous["c"]), 2),
                    "t": previous["t"],
                }
            )
    return blocks[-limit:]


def regime_for(spot: float, p50: float, up_pct: float) -> str:
    drift = (p50 - spot) / spot * 100 if spot else 0.0
    if up_pct >= 65 and drift > 0.02:
        return "TRENDING \u25b2"
    if up_pct <= 35 and drift < -0.02:
        return "TRENDING \u25bc"
    return "SIDEWAYS"


def median_candles(frames: list) -> list[dict]:
    candles: list[dict] = []
    for index in range(len(frames[0])):
        candles.append(
            {
                "o": round(statistics.median(float(f["open"].iloc[index]) for f in frames), 2),
                "h": round(statistics.median(float(f["high"].iloc[index]) for f in frames), 2),
                "l": round(statistics.median(float(f["low"].iloc[index]) for f in frames), 2),
                "c": round(statistics.median(float(f["close"].iloc[index]) for f in frames), 2),
            }
        )
    return candles


# --------------------------------------------------------------------------- #
# trade plan + Telugu reasoning
# --------------------------------------------------------------------------- #


def build_plan(symbol: str, spot: float, decision, base) -> dict:
    step = 50 if symbol == "NIFTY" else 100
    up_pct = base.up_pct
    conviction = decision.confidence
    if decision.action == "BUY_CE":
        strike = int(math.ceil((spot + step * 1.5) / step) * step)
        traffic = "GREEN SUPER-TREND" if conviction >= 80 else "YELLOW SNIPER"
        return {
            "action": "BUY_CE",
            "strike": f"{strike} CE",
            "watch_entry": round(spot * 1.0006, 2),
            "stop_loss": round(spot * 0.9965, 2),
            "target1": round(spot * 1.005, 2),
            "target2": round(base.p90, 2),
            "tsl": "Gear 1 (breakeven after T1)",
            "traffic": traffic,
            "bias": "long",
            "win_rate": round(min(96.0, 50 + abs(up_pct - 50) * 1.05), 1),
        }
    if decision.action == "BUY_PE":
        strike = int(math.floor((spot - step * 1.5) / step) * step)
        traffic = "GREEN SUPER-TREND" if conviction >= 80 else "YELLOW SNIPER"
        return {
            "action": "BUY_PE",
            "strike": f"{strike} PE",
            "watch_entry": round(spot * 0.9994, 2),
            "stop_loss": round(spot * 1.0035, 2),
            "target1": round(spot * 0.995, 2),
            "target2": round(base.p10, 2),
            "tsl": "Gear 1 (breakeven after T1)",
            "traffic": traffic,
            "bias": "short",
            "win_rate": round(min(96.0, 50 + abs(up_pct - 50) * 1.05), 1),
        }
    return {
        "action": "NO_TRADE",
        "strike": "-",
        "watch_entry": round(spot, 2),
        "stop_loss": round(spot * 0.9965, 2),
        "target1": round(spot * 1.005, 2),
        "target2": round(base.p90, 2),
        "tsl": "idle",
        "traffic": "RED NO-TRADE",
        "bias": "flat",
        "win_rate": round(max(0.0, 50 - abs(up_pct - 50) * 0.4), 1),
    }


def telugu_logs(symbol: str, spot: float, vwap: float, base, small, mini, decision, plan) -> list[dict]:
    label = DISPLAY[symbol]
    stamp = lambda: datetime.now(tz=IST).isoformat(timespec="seconds")  # noqa: E731
    above = spot >= vwap
    logs: list[dict] = [
        {
            "t": stamp(),
            "kind": "info",
            "text": (
                f"నేను {label} లో {LOOKBACK} క్యాండిల్స్ చదివాను. స్పాట్ {spot:,.2f}, "
                f"VWAP {vwap:,.2f} — ధర VWAP {'పైన' if above else 'కింద'} ఉంది, "
                f"{'కొనుగోలుదారులు' if above else 'అమ్మకందారులు'} నియంత్రణలో ఉన్నారు."
            ),
        },
        {
            "t": stamp(),
            "kind": "info",
            "text": (
                f"బేస్ మోడల్ (102.3M) {base.samples} భవిష్యత్తులను అంచనా వేసింది — "
                f"{base.paths_up} మార్గాలు పైకి ({base.up_pct:.1f}%). "
                f"p10 {base.p10:,.2f} · మీడియన్ {base.p50:,.2f} · p90 {base.p90:,.2f}."
            ),
        },
    ]
    if base.up_pct >= 78:
        logs.append({"t": stamp(), "kind": "info",
                     "text": "పైకి 78% గేట్ దాటింది — బుల్లిష్ బయాస్ స్పష్టంగా ఉంది."})
    elif base.up_pct <= 22:
        logs.append({"t": stamp(), "kind": "info",
                     "text": "కిందకి 22% గేట్ దాటింది — బేరిష్ బయాస్ స్పష్టంగా ఉంది."})
    else:
        logs.append({"t": stamp(), "kind": "no_trade",
                     "text": f"బేస్ {base.up_pct:.1f}% — 22–78 డెడ్ జోన్. నేను ఇక్కడ ట్రేడ్ చేయను."})

    logs.append({
        "t": stamp(), "kind": "info",
        "text": (
            f"స్మాల్ ట్రెండ్ వాలిడేటర్ {small.paths_up}/{small.samples} పైకి; "
            f"మినీ మైక్రో ట్రిగ్గర్ {mini.paths_up}/{mini.samples} పైకి."
        ),
    })

    if decision.approved:
        logs.append({"t": stamp(), "kind": "order",
                     "text": (f"మూడు మోడళ్లు ఏకీభవించాయి, కోన్ వెడల్పు {base.dispersion * 100:.2f}% "
                              f"— టైట్. ట్రేడ్ ఆమోదించబడింది.")})
        logs.append({
            "t": stamp(), "kind": "order",
            "text": (
                f"{plan['strike']} కొనుగోలు. ఎంట్రీ {plan['watch_entry']:,.2f} పైన, "
                f"స్టాప్ లాస్ {plan['stop_loss']:,.2f}, టార్గెట్ 1 {plan['target1']:,.2f}, "
                f"టార్గెట్ 2 {plan['target2']:,.2f}. TSL: {plan['tsl']}."
            ),
        })
    else:
        logs.append({
            "t": stamp(), "kind": "no_trade",
            "text": "గేట్ విఫలమైంది — " + "; ".join(decision.reasons[:2]) + ". నేను వేచి ఉంటాను.",
        })
    return logs


# --------------------------------------------------------------------------- #
# walk-forward replay
# --------------------------------------------------------------------------- #


def outcome_for(action: str, spot: float, future: list[dict]) -> tuple[str, float]:
    if not future:
        return "NO DATA", 0.0
    if action == "NO_TRADE":
        realized = (future[-1]["c"] - spot) / spot * 100
        return "SKIPPED", round(realized, 3)
    long_side = action == "BUY_CE"
    stop = spot * (0.9965 if long_side else 1.0035)
    target1 = spot * (1.005 if long_side else 0.995)
    for candle in future:
        hit_stop = candle["l"] <= stop if long_side else candle["h"] >= stop
        hit_target = candle["h"] >= target1 if long_side else candle["l"] <= target1
        if hit_target and hit_stop:
            return "T1 HIT (ambiguous)", round((target1 - spot) / spot * 100, 3)
        if hit_target:
            return "T1 HIT", round((target1 - spot) / spot * 100, 3)
        if hit_stop:
            return "SL HIT", round((stop - spot) / spot * 100, 3)
    return "OPEN", round((future[-1]["c"] - spot) / spot * 100, 3)


def walk_forward(trio: KronosTrioEnsemble, candles: list[dict], evaluations: int,
                 samples: int) -> list[dict]:
    import pandas as pd

    if evaluations <= 0 or len(candles) < LOOKBACK + 30:
        return []
    stamps = pd.to_datetime([c["t"] for c in candles], unit="s", utc=True).tz_convert("Asia/Kolkata")
    sessions: dict[str, list[int]] = {}
    for index, stamp in enumerate(stamps):
        sessions.setdefault(stamp.strftime("%Y-%m-%d"), []).append(index)
    eligible = [
        (day, indices[int(len(indices) * fraction)])
        for day, indices in sorted(sessions.items())
        for fraction in (0.55, 0.95)
    ]
    usable = [(day, index) for day, index in eligible if index >= LOOKBACK - 1 and index + HORIZON < len(candles)]
    if not usable:
        return []
    stride = max(len(usable) // evaluations, 1)
    selected = usable[-evaluations * stride::stride][:evaluations]
    if len(selected) < evaluations:
        selected = usable[-evaluations:]

    original = dict(trio.samples)
    trio.samples = {role: samples for role in original}
    entries: list[dict] = []
    try:
        for day, index in selected:
            window = candles[: index + 1][-LOOKBACK:]
            frame, stamp_series = to_frame(window, LOOKBACK)
            spot = window[-1]["c"]
            with trio.torch.inference_mode():
                base = trio.forecast("base", frame)
                small = trio.forecast("small", frame)
                mini = trio.forecast("mini", frame)
            decision = decide_trio(base, small, mini)
            future = candles[index + 1: index + 1 + HORIZON]
            outcome, realized = outcome_for(decision.action, spot, future)
            entries.append(
                {
                    "session": day,
                    "at": stamps[index].isoformat(),
                    "spot": round(spot, 2),
                    "action": decision.action,
                    "paths_up": base.paths_up,
                    "samples": base.samples,
                    "confidence": decision.confidence,
                    "outcome": outcome,
                    "realized_pct": realized,
                    "future_candles": [
                        {"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in future
                    ],
                }
            )
    finally:
        trio.samples = original
    return entries


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_symbol_state(trio: KronosTrioEnsemble, symbol: str, candles: list[dict], spot: float,
                       mini_candles: list[dict], mini_source: str) -> dict:
    window = candles[-LOOKBACK:]
    frame, _ = to_frame(window, LOOKBACK)
    mini_frame, _ = to_frame(mini_candles, 120)
    spot = float(window[-1]["c"])

    base = trio.forecast("base", frame)
    small = trio.forecast("small", frame)
    mini = trio.forecast("mini", mini_frame)
    decision = decide_trio(base, small, mini)

    vwap = vwap_series(window)
    resistance, support = swing_clusters(window)
    blocks = order_blocks(window)
    plan = build_plan(symbol, spot, decision, base)
    frames = trio.frames.get("base", [])
    median = median_candles(frames) if frames else []

    return {
        "symbol": symbol,
        "display": DISPLAY[symbol],
        "interval": "5m",
        "spot": round(spot, 2),
        "mini_source": mini_source,
        "candles": window,
        "vwap": vwap[-len(window):],
        "overlays": {
            "support": [level for level in support if level < spot][-3:],
            "resistance": [level for level in resistance if level > spot][:3],
            "order_blocks": blocks,
        },
        "forecast": {
            "paths_up": base.paths_up,
            "samples": base.samples,
            "p10": round(base.p10, 2),
            "median": round(base.p50, 2),
            "p90": round(base.p90, 2),
            "regime": regime_for(spot, base.p50, base.up_pct),
            "dispersion": round(base.dispersion * 100, 3),
            "cone_tight": base.dispersion <= CONE_TIGHT_RATIO,
            "all_paths": [list(path) for path in _paths_from_frames(frames)],
            "median_candles": median,
            "small_up_pct": small.up_pct,
            "mini_up_pct": mini.up_pct,
            "horizon": HORIZON,
        },
        "decision": {
            "action": decision.action,
            "approved": decision.approved,
            "confidence": decision.confidence,
            "reasons": decision.reasons,
        },
        "plan": plan,
        "telugu_logs": telugu_logs(symbol, spot, vwap[-1], base, small, mini, decision, plan),
    }


def _paths_from_frames(frames: list) -> list[list[float]]:
    if not frames:
        return []
    length = len(frames[0])
    return [[round(float(frame["close"].iloc[i]), 2) for i in range(length)] for frame in frames]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real Nifty/BankNifty KRONOS cloud forecast")
    parser.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--replay-samples", type=int, default=6)
    parser.add_argument("--replay-evals", type=int, default=6)
    parser.add_argument("--out", default=str(REPO_ROOT / "state" / "nifty_kronos_state.json"))
    args = parser.parse_args(argv)

    started = time.time()
    symbols = [name.strip().upper() for name in args.symbols.split(",") if name.strip()]

    print(f"fetching real market data for {symbols} …")
    market: dict[str, dict] = {}
    for name in symbols:
        yahoo = SYMBOLS[name]
        candles, spot = fetch_candles(yahoo, "5m", "15d")
        try:
            minute_candles, _ = fetch_candles(yahoo, "1m", "5d")
            mini_source = "1m"
        except RuntimeError as exc:
            print(f"  1m unavailable ({exc}); using 5m tail for the micro model")
            minute_candles, mini_source = candles[-160:], "5m-fallback"
        market[name] = {"candles": candles, "spot": spot, "mini": minute_candles, "mini_source": mini_source}
        print(f"  {name}: {len(candles)} 5m candles, {len(minute_candles)} 1m candles, spot {spot:.2f}")

    print(f"loading the trio (base samples={args.samples}) …")
    trio = KronosTrioEnsemble(samples=args.samples, verbose=True, collect_frames=True).load()

    payload: dict = {
        "generated_at": datetime.now(tz=IST).isoformat(timespec="seconds"),
        "runner": {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_url": (
                f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/"
                f"{os.environ.get('GITHUB_RUN_ID')}"
                if os.environ.get("GITHUB_RUN_ID")
                else None
            ),
            "runner_os": os.environ.get("RUNNER_OS", "unknown"),
        },
        "model": {"name": "Kronos-base", "params": "102.3M", "context": LOOKBACK, "horizon": HORIZON},
        "symbols": {},
    }

    for name in symbols:
        entry = market[name]
        print(f"forecasting {name} …")
        payload["symbols"][name] = build_symbol_state(
            trio, name, entry["candles"], entry["spot"], entry["mini"], entry["mini_source"]
        )
        forecast = payload["symbols"][name]["forecast"]
        print(f"  {name}: paths_up {forecast['paths_up']}/{forecast['samples']} "
              f"regime {forecast['regime']} action {payload['symbols'][name]['decision']['action']}")

    print(f"walk-forward replay ({args.replay_evals} checkpoints, {args.replay_samples} samples) …")
    replay = walk_forward(trio, market[symbols[0]]["candles"], args.replay_evals, args.replay_samples)
    payload["replay"] = replay
    hits = sum(1 for entry in replay if entry["outcome"].startswith("T1 HIT"))
    trades = sum(1 for entry in replay if entry["action"] != "NO_TRADE")
    payload["replay_summary"] = {
        "checkpoints": len(replay),
        "trades": trades,
        "t1_hits": hits,
        "hit_rate": round(100 * hits / trades, 1) if trades else 0.0,
    }
    payload["elapsed_seconds"] = round(time.time() - started, 1)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest = out_path.parent / "latest_cloud_state.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.1f} KB) in {payload['elapsed_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
