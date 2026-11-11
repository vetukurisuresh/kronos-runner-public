"""Sub-second programmatic speed and Upstox-to-model pipe telemetry.

The live session calls :class:`ProgrammaticTelemetry` on every socket batch.  It
keeps two high-precision rolling windows (1 s and 5 s) measured with
``time.perf_counter_ns`` and never manufactures a rate when the socket is
silent.  The dashboard consumes the resulting JSON as a read-only deck.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
FAST_WINDOW_SEC = 1.0
SLOW_WINDOW_SEC = 5.0
FEED_STALE_MS = 5_000
MAX_SAMPLES = 4_096


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


def _percentile(values: Iterable[float], pct: float) -> float:
    rows = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not rows:
        return 0.0
    index = max(0, min(len(rows) - 1, int(math.ceil(len(rows) * pct)) - 1))
    return rows[index]


def _epoch_ms(value: Any) -> float | None:
    number = _f(value)
    if number <= 0:
        return None
    return number * 1000.0 if number < 100_000_000_000 else number


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def _now_ist(now: Any = None) -> datetime:
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


def _feed_age_ms(feed: Mapping[str, Any] | None, now: Any = None) -> int | None:
    feed = feed or {}
    epoch = _f(feed.get("updated_epoch"))
    if epoch <= 0:
        return None
    if epoch > 100_000_000_000:
        epoch /= 1000.0
    return int(max(0.0, _now_ist(now).timestamp() - epoch) * 1000.0)


@dataclass(frozen=True)
class DataPipeSpec:
    key: str
    stream: str
    reader: str
    short: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "index": int(self.key.split("_")[-1]),
            "stream": self.stream,
            "reader": self.reader,
            "model_reader": self.reader,
            "short": self.short,
        }


DATA_PIPE_SPECS: tuple[DataPipeSpec, ...] = (
    DataPipeSpec(
        "pipe_1",
        "Spot 1m/5m/15m OHLC (LTT)",
        "Kronos-base (102M) & SMC Engine",
        "SPOT OHLC",
    ),
    DataPipeSpec(
        "pipe_2",
        "Futures Volume (Δvtt) & Turnover (Amount)",
        "Kronos-base 6-Feature Tokenizer",
        "FUTURES FLOW",
    ),
    DataPipeSpec(
        "pipe_3",
        "11-Strike Option Chain (CE/PE OI, 5m ΔOI, PCR)",
        "Kronos-small (25M) Regime Judge",
        "11-STRIKE CHAIN",
    ),
    DataPipeSpec(
        "pipe_4",
        "Live Option Greeks (Delta, Gamma, Theta, Vega, IV)",
        "Kronos-small Greek Gate & Target Mapper",
        "GREEKS / IV",
    ),
    DataPipeSpec(
        "pipe_5",
        "5-Level Order Book Depth (OBI_5 & TBQ/TSQ)",
        "Kronos-mini (4M) 1m Execution Sniper",
        "5-LEVEL DEPTH",
    ),
    DataPipeSpec(
        "pipe_6",
        "India VIX & Top-5 Heavyweights Flow",
        "Parallel Tsunami & Lead-Lag Engine",
        "VIX / BREADTH",
    ),
)
DATA_PIPE_MAP = {spec.key: spec for spec in DATA_PIPE_SPECS}


def _symbol(feed: Mapping[str, Any], symbol: str | None = None) -> str:
    if symbol:
        return str(symbol).upper()
    for key in ("spot", "full_spectrum", "option_chain"):
        block = feed.get(key)
        if isinstance(block, Mapping) and block:
            return "NIFTY" if "NIFTY" in block else str(next(iter(block)))
    return "NIFTY"


def _sample_spot(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    spot = (feed.get("spot") or {}).get(symbol) or {}
    if not isinstance(spot, Mapping):
        spot = {}
    return spot


def _sample_spectrum(feed: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    spectrum = (feed.get("full_spectrum") or {}).get(symbol) or {}
    return spectrum if isinstance(spectrum, Mapping) else {}


def _levels(rows: Any) -> list[Any]:
    return list(rows or []) if isinstance(rows, (list, tuple)) else []


def _pipe_status(fresh: bool, valid: bool, present: bool) -> str:
    if not fresh:
        return "STALE"
    if valid:
        return "SYNCED_LIVE"
    if present:
        return "DEGRADED"
    return "STALE"


def build_data_pipe_health(
    feed: Mapping[str, Any] | None,
    *,
    symbol: str = "NIFTY",
    now: Any = None,
) -> list[dict[str, Any]]:
    """Map the six real Upstox streams to their exact model readers.

    Status is a data-health claim, not a model opinion: a stale heartbeat makes
    every pipe ``STALE`` even when an old sample still has non-zero values.
    """
    feed = feed or {}
    symbol = _symbol(feed, symbol)
    age = _feed_age_ms(feed, now)
    feed_status = feed.get("feed_status") if isinstance(feed.get("feed_status"), Mapping) else {}
    offline = str(feed_status.get("status") or "") in {"FEED_OFFLINE_OR_STALE", "MARKET_CLOSED"}
    fresh = bool(
        age is not None
        and age <= FEED_STALE_MS
        and not feed.get("fixture")
        and not offline
    )
    spot = _sample_spot(feed, symbol)
    spectrum = _sample_spectrum(feed, symbol)
    futures = spectrum.get("futures") or {}
    depth = spectrum.get("depth") or {}
    pcr = spectrum.get("pcr") or {}
    greeks = spectrum.get("greeks") or {}
    vix = spectrum.get("vix") or {}
    breadth = spectrum.get("breadth") or {}
    bids, asks = _levels(depth.get("bids")), _levels(depth.get("asks"))
    candles_1m = _levels(spot.get("candles_1m"))
    candles_5m = _levels(spot.get("candles_5m"))
    candles_15m = _levels(spot.get("candles_15m"))
    if not candles_1m:
        candles_1m = [0] * int(_f((feed.get("buffer_1m") or {}).get(symbol)))
    if not candles_5m:
        candles_5m = [0] * int(_f((feed.get("buffer_5m") or {}).get(symbol)))
    if not candles_15m:
        candles_15m = [0] * int(_f((feed.get("buffer_15m") or {}).get(symbol)))

    prices = feed.get("prices") if isinstance(feed.get("prices"), Mapping) else {}
    spot_ltp = _f(spot.get("ltp") or prices.get("NSE_INDEX|Nifty 50"))
    spot_valid = bool(spot_ltp > 0 and candles_1m and candles_5m and candles_15m)
    spot_present = bool(spot_ltp > 0 or candles_1m or candles_5m or candles_15m)
    fut_vol = _f(futures.get("volume"))
    fut_amount = _f(futures.get("amount"))
    flow_valid = bool(fut_vol > 0 and fut_amount > 0)
    flow_present = bool(fut_vol > 0 or fut_amount > 0)
    strike_count = int(_f(pcr.get("strike_count")))
    valid_strikes = int(_f(pcr.get("valid_strikes")))
    chain_valid = bool(
        strike_count == 11
        and valid_strikes >= 11
        and _f(pcr.get("ce_oi")) > 0
        and _f(pcr.get("pe_oi")) > 0
    )
    chain_present = bool(strike_count or _f(pcr.get("ce_oi")) or _f(pcr.get("pe_oi")))
    greek_count = sum(
        1 for name in ("delta", "gamma", "theta", "vega")
        if _f(greeks.get(name)) != 0
    )
    greeks_valid = bool(_f(greeks.get("iv")) > 0 and greek_count >= 3)
    greeks_present = bool(_f(greeks.get("iv")) > 0 or greek_count > 0)
    depth_valid = bool(
        len(bids) >= 5
        and len(asks) >= 5
        and _f(depth.get("obi_5")) > 0
        and _f(depth.get("tbq")) > 0
        and _f(depth.get("tsq")) > 0
    )
    depth_present = bool(bids or asks or _f(depth.get("obi_5")) > 0)
    breadth_total = int(_f(breadth.get("total")))
    vix_valid = bool(_f(vix.get("ltp")) > 0 and breadth_total >= 5)
    vix_present = bool(_f(vix.get("ltp")) > 0 or breadth_total > 0)

    values = (
        (spot_valid, spot_present,
         f"NIFTY {spot_ltp:,.2f} · 1m {len(candles_1m)} / 5m {len(candles_5m)} / 15m {len(candles_15m)}"),
        (flow_valid, flow_present,
         f"Δvtt {fut_vol:,.0f} · Amount ₹{fut_amount:,.0f} · Basis {_f(futures.get('basis')):+.2f}"),
        (chain_valid, chain_present,
         f"{strike_count}/11 strikes · PCR {_f(pcr.get('pcr')):.2f} · ΔOI PCR {_f(pcr.get('doi_pcr')):+.2f}"),
        (greeks_valid, greeks_present,
         f"IV {_f(greeks.get('iv')):.2f}% · Δ {_f(greeks.get('delta')):.3f} · Θ {_f(greeks.get('theta')):.2f}"),
        (depth_valid, depth_present,
         f"{len(bids)} bids / {len(asks)} asks · OBI {_f(depth.get('obi_5')):.2f} · TBQ/TSQ {_f(depth.get('tbq_tsq')):.2f}x"),
        (vix_valid, vix_present,
         f"VIX {_f(vix.get('ltp')):.2f} · Breadth {int(_f(breadth.get('aligned')))}/{breadth_total}"),
    )
    rows: list[dict[str, Any]] = []
    for spec, (valid, present, sample) in zip(DATA_PIPE_SPECS, values):
        rows.append({
            **spec.as_dict(),
            "status": _pipe_status(fresh, valid, present),
            "age_ms": age,
            "sample": sample,
            "valid": bool(valid),
            "present": bool(present),
            "source": "offline-fixture" if feed.get("fixture") else "upstox-v3",
        })
    return rows


def pipe_health_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out = {"synced_live": 0, "degraded": 0, "stale": 0, "total": 0}
    for row in rows:
        out["total"] += 1
        status = str(row.get("status") or "").upper()
        if status == "SYNCED_LIVE":
            out["synced_live"] += 1
        elif status == "DEGRADED":
            out["degraded"] += 1
        else:
            out["stale"] += 1
    return out


class ProgrammaticTelemetry:
    """Rolling, clock-injectable telemetry for the live tick path."""

    def __init__(
        self,
        *,
        clock_ns: Any = time.perf_counter_ns,
        clock_epoch_ms: Any = lambda: time.time() * 1000.0,
        max_samples: int = MAX_SAMPLES,
    ) -> None:
        self.clock_ns = clock_ns
        self.clock_epoch_ms = clock_epoch_ms
        self._ticks: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._scans: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._last_tick_ns = 0
        self._last_scan_ms = 0.0

    def _prune(self, now_ns: int) -> None:
        cutoff = int(now_ns - SLOW_WINDOW_SEC * 1_000_000_000)
        while self._ticks and self._ticks[0]["t_ns"] < cutoff:
            self._ticks.popleft()
        while self._scans and self._scans[0]["t_ns"] < cutoff:
            self._scans.popleft()

    def record_tick(
        self,
        *,
        now_ns: int | None = None,
        server_epoch_ms: float | None = None,
        ltt_ms: float | None = None,
        payload_bytes: int = 0,
        sanitize_us: float = 0.0,
        frame_size: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record one accepted V3 frame or batch sample.

        ``frame_size`` is accepted as an alias for ``payload_bytes`` for callers
        that measure the raw websocket frame directly.
        """
        moment_ns = int(now_ns if now_ns is not None else self.clock_ns())
        epoch_ms = _f(server_epoch_ms, _f(self.clock_epoch_ms()))
        if not epoch_ms:
            epoch_ms = time.time() * 1000.0
        if frame_size is not None:
            payload_bytes = frame_size
        elif kwargs.get("payload_size") is not None:
            payload_bytes = int(_f(kwargs.get("payload_size")))
        latency_ms = None
        ltt = _epoch_ms(ltt_ms)
        if ltt is not None:
            latency_ms = max(0.0, epoch_ms - ltt)
        sample = {
            "t_ns": moment_ns,
            "server_epoch_ms": round(epoch_ms, 3),
            "ltt_ms": ltt,
            "payload_bytes": max(0, int(payload_bytes or 0)),
            "sanitize_us": max(0.0, _f(sanitize_us)),
            "latency_ms": latency_ms,
        }
        self._ticks.append(sample)
        self._last_tick_ns = moment_ns
        self._prune(moment_ns)
        return sample

    # Natural alternative used by instrumentation wrappers.
    record_frame = record_tick

    def record_scan(
        self,
        latency_ms: float | None = None,
        *,
        scan_ms: float | None = None,
        started_ns: int | None = None,
        ended_ns: int | None = None,
        now_ns: int | None = None,
        decisions: int = 1,
    ) -> dict[str, Any]:
        moment_ns = int(now_ns if now_ns is not None else self.clock_ns())
        if latency_ms is None:
            latency_ms = scan_ms
        if latency_ms is None and started_ns is not None:
            end = int(ended_ns if ended_ns is not None else moment_ns)
            latency_ms = max(0.0, (end - int(started_ns)) / 1_000_000.0)
        sample = {
            "t_ns": moment_ns,
            "latency_ms": max(0.0, _f(latency_ms)),
            "decisions": max(1, int(decisions)),
        }
        self._scans.append(sample)
        self._last_scan_ms = sample["latency_ms"]
        self._prune(moment_ns)
        return sample

    def _stats(self, now_ns: int, window_sec: float) -> dict[str, Any]:
        cutoff = int(now_ns - window_sec * 1_000_000_000)
        ticks = [row for row in self._ticks if row["t_ns"] >= cutoff]
        scans = [row for row in self._scans if row["t_ns"] >= cutoff]
        latencies = [row["latency_ms"] for row in ticks if row["latency_ms"] is not None]
        sanitize = [row["sanitize_us"] for row in ticks if row["sanitize_us"] > 0]
        return {
            "ticks": ticks,
            "scans": scans,
            "tick_count": len(ticks),
            "payload_bytes": sum(row["payload_bytes"] for row in ticks),
            "latency_p50": _percentile(latencies, 0.50),
            "latency_p95": _percentile(latencies, 0.95),
            "sanitize_p95": _percentile(sanitize, 0.95),
            "sanitize_avg": (
                sum(sanitize) / len(sanitize) if sanitize else 0.0
            ),
            "scan_p50": _percentile([row["latency_ms"] for row in scans], 0.50),
            "scan_p95": _percentile([row["latency_ms"] for row in scans], 0.95),
            "decisions": sum(row["decisions"] for row in scans),
        }

    def snapshot(
        self,
        feed: Mapping[str, Any] | None = None,
        *,
        symbol: str = "NIFTY",
        now: Any = None,
        now_ns: int | None = None,
        scan_latency_ms: float | None = None,
    ) -> dict[str, Any]:
        feed = feed or {}
        moment_ns = int(now_ns if now_ns is not None else self.clock_ns())
        if self._last_tick_ns and moment_ns < self._last_tick_ns:
            moment_ns = self._last_tick_ns
        if self._ticks and moment_ns > 10**15 and self._ticks[-1]["t_ns"] < 10**12:
            moment_ns = self._ticks[-1]["t_ns"]
        self._prune(moment_ns)
        fast = self._stats(moment_ns, FAST_WINDOW_SEC)
        slow = self._stats(moment_ns, SLOW_WINDOW_SEC)
        existing = feed.get("programmatic") if isinstance(feed.get("programmatic"), Mapping) else {}
        prior = existing.get("speed_telemetry") if isinstance(existing.get("speed_telemetry"), Mapping) else {}
        ticks_per_sec = fast["tick_count"] or _f(feed.get("tick_rate") or prior.get("ticks_per_sec"))
        payload_kbps = (fast["payload_bytes"] / 1024.0) if fast["tick_count"] else _f(prior.get("payload_kbps"))
        latency = fast["latency_p50"] if fast["tick_count"] else _f(prior.get("exchange_to_server_latency_ms"))
        sanitize = slow["sanitize_p95"] if slow["tick_count"] else _f(prior.get("python_sanitize_us"))
        if not sanitize:
            sanitizer = feed.get("sanitizer") if isinstance(feed.get("sanitizer"), Mapping) else {}
            sanitize = _f(sanitizer.get("sanitize_latency_p95_us"))
        if scan_latency_ms is not None:
            scan_ms = _f(scan_latency_ms)
        elif slow["scan_p50"]:
            scan_ms = slow["scan_p50"]
        else:
            scan_ms = _f(prior.get("model_scan_latency_ms") or feed.get("model_scan_latency_ms"))
        if scan_ms <= 0 and self._last_scan_ms:
            scan_ms = self._last_scan_ms
        decisions_hz = float(fast["decisions"]) if fast["scans"] else _f(prior.get("decisions_per_sec_hz"))
        if scan_ms < 25:
            grade = "⚡ ULTRA-FAST (<25ms)"
        elif scan_ms <= 80:
            grade = "🟢 OPTIMAL (25–80ms)"
        else:
            grade = "⚠️ LAGGING (>80ms)"
        if scan_ms <= 0:
            grade = "⚠️ LAGGING (>80ms)"
        speed_score = _clamp(100.0 - (scan_ms / 80.0 * 100.0), 0.0, 100.0) if scan_ms > 0 else 0.0
        pipes = build_data_pipe_health(feed, symbol=symbol, now=now)
        status = pipe_health_summary(pipes)
        return {
            "ok": bool(status["synced_live"] == len(DATA_PIPE_SPECS)),
            "generated_at": _iso(_now_ist(now)),
            "source": "offline-fixture" if feed.get("fixture") else "upstox-v3",
            "speed_telemetry": {
                "ticks_per_sec": round(float(ticks_per_sec), 1),
                "ticks_per_sec_5s": round(slow["tick_count"] / SLOW_WINDOW_SEC, 1),
                "payload_kbps": round(float(payload_kbps), 1),
                "payload_kbps_5s": round(slow["payload_bytes"] / 1024.0 / SLOW_WINDOW_SEC, 1),
                "exchange_to_server_latency_ms": round(float(latency), 2),
                "exchange_to_server_latency_p95_ms": round(float(fast["latency_p95"]), 2),
                "python_sanitize_us": round(float(sanitize), 1),
                "python_sanitize_avg_us": round(float(slow["sanitize_avg"]), 1),
                "model_scan_latency_ms": round(float(scan_ms), 2),
                "model_scan_p95_ms": round(float(slow["scan_p95"]), 2),
                "decisions_per_sec_hz": round(float(decisions_hz), 2),
                "speed_grade": grade,
                "speed_score": round(float(speed_score), 1),
                "sample_window_ms": int(SLOW_WINDOW_SEC * 1000),
                "samples": slow["tick_count"],
            },
            "data_pipe_health": pipes,
            "data_health_summary": status,
        }


def build_programmatic_telemetry(
    feed: Mapping[str, Any] | None,
    *,
    symbol: str = "NIFTY",
    now: Any = None,
    telemetry: ProgrammaticTelemetry | None = None,
    scan_latency_ms: float | None = None,
) -> dict[str, Any]:
    """Build a complete deck payload from live rolling samples or feed fallback."""
    return (telemetry or ProgrammaticTelemetry()).snapshot(
        feed, symbol=symbol, now=now, scan_latency_ms=scan_latency_ms,
    )


# Short aliases used by the live session and tests.
TelemetryMonitor = ProgrammaticTelemetry
build_telemetry = build_programmatic_telemetry

__all__ = [
    "DATA_PIPE_SPECS", "DATA_PIPE_MAP", "DataPipeSpec",
    "ProgrammaticTelemetry", "TelemetryMonitor",
    "build_data_pipe_health", "pipe_health_summary",
    "build_programmatic_telemetry", "build_telemetry",
]
