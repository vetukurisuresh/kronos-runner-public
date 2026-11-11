"""Intra-5-minute candle sniper with a human-versus-KRONOS proof ledger.

Human discretionary traders usually wait for the 5m candle to close and then
wait for a confirmation candle.  This detector evaluates the *forming* candle
from second 5 through second 290, so a 2-of-3 model quorum can be executed at
the ignition price without that delay.

The module is deliberately pure: it never calls a broker, a clock or a network
endpoint.  The live session feeds it the canonical payload and the proof ledger
records exactly what was known at entry, then what a candle-close trader paid.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
DEFAULT_OBI_FLOOR = 1.25
DEFAULT_TBQ_FLOOR = 1.25
DEFAULT_MIN_ELAPSED = 5.0
DEFAULT_MAX_ELAPSED = 290.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bar(row: Any) -> dict[str, float]:
    raw = _mapping(row)
    return {
        "t": _f(raw.get("t") if raw.get("t") is not None else raw.get("time")),
        "o": _f(raw.get("o") if raw.get("o") is not None else raw.get("open")),
        "h": _f(raw.get("h") if raw.get("h") is not None else raw.get("high")),
        "l": _f(raw.get("l") if raw.get("l") is not None else raw.get("low")),
        "c": _f(raw.get("c") if raw.get("c") is not None else raw.get("close")),
        "v": _f(raw.get("v") if raw.get("v") is not None else raw.get("volume")),
    }


def _bars(rows: Any) -> list[dict[str, float]]:
    return [bar for bar in (_bar(row) for row in (rows or []) if isinstance(row, Mapping)) if bar["t"] > 0]


def _option(feed: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    options = feed.get("option") or {}
    if isinstance(options, Mapping):
        row = options.get(symbol)
        if isinstance(row, Mapping):
            return row
        for candidate in options.values():
            if isinstance(candidate, Mapping) and candidate.get("strike_price"):
                return candidate
    return {}


def _spot(feed: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    spots = feed.get("spot") or {}
    row = spots.get(symbol) if isinstance(spots, Mapping) else {}
    return row if isinstance(row, Mapping) else {}


def _chain_quote(
    feed: Mapping[str, Any], symbol: str, side: str, spot: float,
    *, preferred_strike: float = 0.0,
) -> dict[str, Any]:
    """Pick the selected contract's chain quote for the quorum direction."""
    chains = feed.get("option_chain") or {}
    rows = chains.get(symbol) if isinstance(chains, Mapping) else []
    if not isinstance(rows, list):
        return {}
    best: tuple[float, Mapping[str, Any]] | None = None
    for raw in rows:
        row = _mapping(raw)
        strike = _f(row.get("strike_price") or row.get("strike"))
        if strike <= 0:
            continue
        distance = (
            abs(strike - preferred_strike) if preferred_strike > 0
            else abs(strike - spot) if spot > 0 else 0.0
        )
        if best is None or distance < best[0]:
            best = (distance, row)
    if best is None:
        return {}
    row = best[1]
    leg = _mapping(row.get("call_options" if side == "CE" else "put_options"))
    market = _mapping(leg.get("market_data"))
    greeks = _mapping(leg.get("option_greeks"))
    strike = _f(row.get("strike_price") or row.get("strike"))
    return {
        "strike": strike,
        "contract": f"{symbol} {strike:.0f} {side}" if strike else f"{symbol} {side}",
        "ltp": _f(market.get("ltp")),
        "bid": _f(market.get("bid_price")),
        "ask": _f(market.get("ask_price")),
        "delta": _f(greeks.get("delta")),
        "gamma": _f(greeks.get("gamma")),
        "theta": _f(greeks.get("theta")),
        "iv": _f(greeks.get("iv")),
    }


def _option_close_at(option: Mapping[str, Any], stamp: float) -> float:
    for raw in option.get("candles_1m") or []:
        bar = _bar(raw)
        if int(bar["t"]) == int(stamp):
            return bar["c"]
    return 0.0


class IntraCandleEarlyDetector:
    """Evaluate a forming 5m candle and preserve an auditable proof trail."""

    def __init__(
        self,
        *,
        min_elapsed_sec: float = DEFAULT_MIN_ELAPSED,
        max_elapsed_sec: float = DEFAULT_MAX_ELAPSED,
        obi_floor: float = DEFAULT_OBI_FLOOR,
        tbq_floor: float = DEFAULT_TBQ_FLOOR,
    ) -> None:
        self.min_elapsed_sec = float(min_elapsed_sec)
        self.max_elapsed_sec = float(max_elapsed_sec)
        self.obi_floor = float(obi_floor)
        self.tbq_floor = float(tbq_floor)
        self.ledger: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    # ------------------------------------------------------------------ proof
    def record_entry(
        self,
        *,
        symbol: str,
        side: str,
        contract: str,
        strike: float,
        entry_price: float,
        spot_entry: float,
        t_kronos: float,
        entry_candle_time: float,
        quorum_count: str = "2/3",
        quorum_confidence: float = 0.0,
        models: Iterable[str] | None = None,
        level: float = 0.0,
        elapsed_sec: float = 0.0,
    ) -> dict[str, Any]:
        existing = next(
            (
                row for row in self.ledger
                if row.get("symbol") == symbol
                and int(row.get("entry_candle_time") or 0) == int(entry_candle_time)
                and row.get("side") == side
            ),
            None,
        )
        if existing:
            return existing
        row = {
            "proof_id": f"{symbol}-{int(entry_candle_time)}-{side}",
            "date": datetime.fromtimestamp(t_kronos, tz=IST).date().isoformat(),
            "time": datetime.fromtimestamp(t_kronos, tz=IST).strftime("%H:%M:%S"),
            "symbol": symbol,
            "contract": contract,
            "strike": round(float(strike), 2),
            "side": side,
            "quorum_count": quorum_count,
            "quorum_confidence": round(float(quorum_confidence), 2),
            "models_confirmed": list(models or []),
            "kronos_entry_price": round(float(entry_price), 2),
            "t_kronos": round(float(t_kronos), 3),
            "t_kronos_iso": datetime.fromtimestamp(t_kronos, tz=IST).isoformat(timespec="seconds"),
            "entry_candle_time": float(entry_candle_time),
            "elapsed_sec": round(float(elapsed_sec), 1),
            "seconds_to_candle_close": round(max(0.0, entry_candle_time + 300.0 - t_kronos), 1),
            "level": round(float(level), 2),
            "spot_entry_price": round(float(spot_entry), 2),
            "human_5m_close_price": None,
            "human_confirm_price": None,
            "human_5m_close_spot": None,
            "human_close_expected_at": float(entry_candle_time + 300.0),
            "human_confirm_expected_at": float(entry_candle_time + 600.0),
            "seconds_ahead_of_human": None,
            "option_price_edge_inr": None,
            "spot_price_edge_pts": None,
            "human_price_source": "PENDING_HUMAN_CANDLE_CLOSE",
            "t1_booked": False,
            "t1_price": None,
            "t2_booked": False,
            "t2_price": None,
            "runner_booked": False,
            "net_profit_rs": 0.0,
            "status": "WAITING_HUMAN_5M_CLOSE",
            "phase": "KRONOS_ENTRY_RECORDED",
        }
        self.ledger.append(row)
        self._seen.add(row["proof_id"])
        return row

    def finalize_proof(
        self,
        proof_id: str,
        *,
        human_5m_close_price: float | None = None,
        human_confirm_price: float | None = None,
        human_5m_close_spot: float | None = None,
        human_t: float | None = None,
        source: str = "recorded_5m_close",
    ) -> dict[str, Any]:
        """Attach the candle-close and confirmation prices to an entry."""
        row = next((item for item in self.ledger if item.get("proof_id") == proof_id), None)
        if row is None:
            raise KeyError(f"unknown proof_id: {proof_id}")
        close_option = _f(human_5m_close_price)
        close_spot = _f(human_5m_close_spot)
        if close_option > 0:
            row["human_5m_close_price"] = round(close_option, 2)
        if close_spot > 0:
            row["human_5m_close_spot"] = round(close_spot, 2)
        if human_confirm_price is not None and _f(human_confirm_price) > 0:
            row["human_confirm_price"] = round(_f(human_confirm_price), 2)
        entry = _f(row.get("kronos_entry_price"))
        close_at = _f(human_t, _f(row.get("human_close_expected_at")))
        row["seconds_ahead_of_human"] = round(max(0.0, close_at - _f(row.get("t_kronos"))), 1)
        if close_option > 0 and entry > 0:
            row["option_price_edge_inr"] = round(close_option - entry, 2)
        entry_spot = _f(row.get("spot_entry_price"))
        if close_spot > 0 and entry_spot > 0:
            edge = close_spot - entry_spot
            row["spot_price_edge_pts"] = round(edge if row.get("side") == "CE" else -edge, 2)
        row["human_price_source"] = str(source)
        if row["human_5m_close_price"] is not None:
            row["status"] = (
                "HUMAN_CONFIRM_RECORDED"
                if row["human_confirm_price"] is not None else "HUMAN_5M_CLOSE_RECORDED"
            )
            row["phase"] = "PROOF_COMPLETE" if row["human_confirm_price"] is not None else "HUMAN_CLOSE_SAVED"
        return row

    def apply_lifecycle(self, trade: Mapping[str, Any], *, proof_id: str | None = None) -> dict[str, Any] | None:
        """Mirror T1/T2/runner fills from the autonomous trade into the proof."""
        trade = trade or {}
        target_id = proof_id
        if not target_id:
            contract = str(trade.get("option_symbol") or trade.get("strike_symbol") or "")
            target_id = next(
                (row["proof_id"] for row in reversed(self.ledger)
                 if contract and row.get("contract") == contract),
                None,
            )
        row = next((item for item in self.ledger if item.get("proof_id") == target_id), None)
        if row is None:
            return None
        legs = list(trade.get("booked_legs") or [])
        t1 = next((leg for leg in legs if str(leg.get("leg") or "").upper() == "T1"), None)
        t2 = next((leg for leg in legs if str(leg.get("leg") or "").upper() == "T2_RUNNER"), None)
        if t1:
            row["t1_booked"] = True
            row["t1_price"] = round(_f(t1.get("price")), 2)
        if t2:
            row["t2_booked"] = True
            row["t2_price"] = round(_f(t2.get("price")), 2)
            row["runner_booked"] = bool(trade.get("runner_active"))
        row["net_profit_rs"] = round(_f(trade.get("realized_pnl_rs")), 2)
        return row

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.ledger]

    # ------------------------------------------------------------------ detect
    def _settle_pending(
        self,
        *,
        symbol: str,
        new_candle_time: float,
        option: Mapping[str, Any],
        spot_bars: list[dict[str, float]],
        current_option_ltp: float,
    ) -> None:
        for row in self.ledger:
            if row.get("symbol") != symbol or row.get("human_5m_close_price") is not None:
                continue
            entry_candle = _f(row.get("entry_candle_time"))
            if new_candle_time <= entry_candle:
                continue
            close_stamp = entry_candle + 240.0
            close_spot = next(
                (bar["c"] for bar in spot_bars if int(bar["t"]) == int(entry_candle)),
                0.0,
            )
            close_option = _option_close_at(option, close_stamp)
            source = "option_1m_close_at_5m_close"
            if close_option <= 0:
                close_option = current_option_ltp
                source = "option_ltp_at_next_candle"
            self.finalize_proof(
                row["proof_id"],
                human_5m_close_price=close_option,
                human_5m_close_spot=close_spot,
                human_t=entry_candle + 300.0,
                source=source,
            )
            if new_candle_time >= entry_candle + 600.0 and row.get("human_confirm_price") is None:
                self.finalize_proof(
                    row["proof_id"],
                    human_5m_close_price=close_option,
                    human_confirm_price=current_option_ltp,
                    human_5m_close_spot=close_spot,
                    human_t=entry_candle + 300.0,
                    source=source + "+confirmation_5m",
                )

    def update(
        self,
        feed: Mapping[str, Any],
        *,
        symbol: str = "NIFTY",
        quorum: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        feed = feed or {}
        quorum = quorum or {}
        spot_block = _spot(feed, symbol)
        option = _option(feed, symbol)
        spot_bars = _bars(spot_block.get("candles_5m"))
        current = _mapping(spot_block.get("current_candle_5m"))
        current_bar = _bar(current)
        if current_bar["t"] <= 0 and spot_bars:
            current_bar = spot_bars[-1]
        candle_time = current_bar["t"]
        moment = _f(now, _f(feed.get("updated_epoch"), 0.0)) or 0.0
        if candle_time > 0 and moment <= 0:
            moment = candle_time + 60.0
        elapsed = max(0.0, moment - candle_time) if candle_time > 0 else 0.0
        in_window = bool(self.min_elapsed_sec <= elapsed <= self.max_elapsed_sec)

        prior = next((bar for bar in reversed(spot_bars) if bar["t"] < candle_time), {})
        prior_high = _f(prior.get("h"))
        prior_low = _f(prior.get("l"))
        spot = _f(spot_block.get("ltp"), current_bar["c"])
        vwap = _f(spot_block.get("vwap") or spot_block.get("atp"), 0.0)
        micro_bars = _bars(spot_block.get("candles_1m"))
        micro_closes = [bar["c"] for bar in micro_bars[-4:] if bar["c"] > 0]
        micro_down = len(micro_closes) >= 2 and micro_closes[-1] < micro_closes[0]
        micro_up = len(micro_closes) >= 2 and micro_closes[-1] > micro_closes[0]
        side = str(quorum.get("side") or "").upper()
        preferred_strike = _f(option.get("strike_price") or option.get("strike"))
        chain = (
            _chain_quote(feed, symbol, side, spot, preferred_strike=preferred_strike)
            if side in {"CE", "PE"} else {}
        )
        option_same_side = str(option.get("option_type") or option.get("side") or "").upper() == side
        option_ltp = _f(chain.get("ltp")) or (_f(option.get("ltp")) if option_same_side else 0.0)
        key_atm = _f(spot_block.get("atm_strike") or option.get("strike_price") or chain.get("strike"))
        depth = _mapping(option.get("depth"))
        obi = _f(option.get("obi_5"), _f(depth.get("obi_5"), 0.0))
        tbq = _f(option.get("tbq_tsq"), _f(depth.get("tbq_tsq"), 0.0))
        book_ok = bool(obi >= self.obi_floor and tbq >= self.tbq_floor)
        below_vwap = bool(vwap > 0 and spot < vwap)
        above_vwap = bool(vwap > 0 and spot > vwap)
        broke_low = bool(prior_low > 0 and spot <= prior_low)
        broke_high = bool(prior_high > 0 and spot >= prior_high)
        if side == "PE":
            aligned = below_vwap or broke_low
            level = prior_low if prior_low > 0 else vwap
            micro_ok = micro_down
        elif side == "CE":
            aligned = above_vwap or broke_high
            level = prior_high if prior_high > 0 else vwap
            micro_ok = micro_up
        else:
            aligned = False
            level = 0.0
            micro_ok = False
        ignition = bool(aligned and book_ok and micro_ok)
        approved = bool(quorum.get("approved"))
        ready = bool(approved and in_window and ignition and option_ltp > 0 and candle_time > 0)

        self._settle_pending(
            symbol=symbol,
            new_candle_time=candle_time,
            option=option,
            spot_bars=spot_bars,
            current_option_ltp=option_ltp,
        )

        proof: dict[str, Any] | None = None
        if ready:
            strike = _f(chain.get("strike"), _f(option.get("strike_price"), key_atm))
            proof = self.record_entry(
                symbol=symbol,
                side=side,
                contract=str(chain.get("contract") or option.get("strike_symbol") or f"{symbol} {strike:.0f} {side}"),
                strike=strike,
                entry_price=option_ltp,
                spot_entry=spot,
                t_kronos=moment,
                entry_candle_time=candle_time,
                quorum_count=str(quorum.get("quorum_count") or "2/3"),
                quorum_confidence=_f(quorum.get("confidence")),
                models=quorum.get("confirmed_roles") or [],
                level=level,
                elapsed_sec=elapsed,
            )
        reason_te = (
            f"5m క్యాండిల్ {elapsed:.0f} సెకన్లలోనే {side} ఇగ్నిషన్; "
            f"OBI {obi:.2f}x, TBQ/TSQ {tbq:.2f}x, ధర {spot:,.2f}."
            if ignition else
            "ఇంకా 5m ఇగ్నిషన్ లేదు; OBI/TBQ మరియు ధర-స్థాయి కలయిక కోసం వెయిటింగ్."
        )
        reason_en = (
            f"Inside forming 5m candle at {elapsed:.0f}s: {side} ignition, "
            f"OBI {obi:.2f}x and TBQ/TSQ {tbq:.2f}x confirmed."
            if ignition else
            "Waiting for the forming 5m candle to sweep a level with accelerating book flow."
        )
        return {
            "ok": True,
            "symbol": symbol,
            "side": side,
            "in_window": in_window,
            "elapsed_sec": round(elapsed, 1),
            "seconds_to_close": round(max(0.0, 300.0 - elapsed), 1) if candle_time else 0.0,
            "ignition": ignition,
            "ready": ready,
            "approved": approved,
            "entry_price": round(option_ltp, 2) if ready else None,
            "trigger_price": round(option_ltp, 2) if option_ltp > 0 else None,
            "spot_trigger": round(spot, 2),
            "level": round(level, 2),
            "vwap": round(vwap, 2),
            "prior_5m_high": round(prior_high, 2),
            "prior_5m_low": round(prior_low, 2),
            "obi_5": round(obi, 3),
            "tbq_tsq": round(tbq, 3),
            "micro_direction": "DOWN" if micro_down else "UP" if micro_up else "FLAT",
            "reason_te": reason_te,
            "reason_en": reason_en,
            "proof": proof,
            "proof_ledger": self.snapshot(),
        }


def build_intra_candle_snapshot(
    feed: Mapping[str, Any],
    *,
    symbol: str = "NIFTY",
    quorum: Mapping[str, Any] | None = None,
    detector: IntraCandleEarlyDetector | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    detector = detector or IntraCandleEarlyDetector()
    return detector.update(feed, symbol=symbol, quorum=quorum, now=now)


__all__ = [
    "DEFAULT_MAX_ELAPSED",
    "DEFAULT_MIN_ELAPSED",
    "DEFAULT_OBI_FLOOR",
    "DEFAULT_TBQ_FLOOR",
    "IntraCandleEarlyDetector",
    "build_intra_candle_snapshot",
]
