"""Role-specialized pipeline: conservative, real-input-only gates."""

from __future__ import annotations

from strategy.role_pipeline import (
    build_role_pipeline,
    data_gate,
    iv_percent,
    micro_timing,
)


def _rows(ce_change: float = 0.0, strikes: int = 6) -> list[dict]:
    rows = []
    for index in range(strikes):
        rows.append(
            {
                "strike_price": 25000 + index * 50,
                "call_options": {
                    "market_data": {"oi": 120000 + index * 1000, "oi_change": ce_change}
                },
                "put_options": {
                    "market_data": {"oi": 160000 + index * 1000, "oi_change": 12000}
                },
            }
        )
    return rows


def _feed(ce_change: float = 0.0) -> dict:
    return {
        "option_chain": {"NIFTY": _rows(ce_change)},
        "option": {
            "NIFTY": {
                "ltp": 216.4,
                "atp_vwap": 200.0,
                "obi_5": 1.6,
                "tbq_tsq": 2.0,
                "iv": 0.1818,
                "candles_1m": [
                    {"h": 210 + i, "l": 200 + i} for i in range(30)
                ],
            }
        },
        "spot": {"NIFTY": {"ltp": 25200.0, "vwap": 25150.0}},
        "full_spectrum": {
            "NIFTY": {
                "depth": {"obi_5": 1.6, "tbq_tsq": 2.0},
                "greeks": {"delta": 0.61, "gamma": 0.0012, "theta": -19.5, "iv": 0.1818},
                "vix": {"change_pct": -1.4},
                "breadth": {"aligned": 4, "total": 5},
                "futures": {"vtt_ratio": 1.2, "basis": 46.95},
            }
        },
        "model_oscillator": {"bullish_upside_prob_pct": 74.0},
    }


def test_iv_percent_scales_fraction_but_not_percent() -> None:
    assert iv_percent(0.1718) == 17.18
    assert iv_percent(17.18) == 17.18
    assert iv_percent(0) is None


def test_data_gate_confirms_bullish_from_real_inputs() -> None:
    gate = data_gate(_feed(), "NIFTY")
    assert gate["pcr"] > 1.0
    assert gate["bull_score"] == 100.0
    assert gate["data_confirmed_bullish"] is True
    assert gate["data_confirmed_bearish"] is False
    assert gate["greeks_ok"] is True
    assert gate["iv_pct"] == 18.18


def test_writer_trap_needs_one_side_unwinding_and_the_other_building() -> None:
    trapped = data_gate(_feed(ce_change=-30000.0), "NIFTY")
    calm = data_gate(_feed(), "NIFTY")
    assert trapped["ce_doi_pct"] <= -3.0
    assert trapped["writer_trap_explosion"] is True
    assert calm["writer_trap_explosion"] is False


def test_micro_timing_arms_only_with_book_candles_and_premium_band() -> None:
    micro = micro_timing(_feed(), "NIFTY")
    assert micro["candles_1m"] == 30
    assert micro["directional_ok"] is True
    # 216.40 is outside the ₹10–₹38 expiry band, so the blast stays unarmed.
    assert micro["expiry_armed"] is False

    cheap = _feed()
    cheap["option"]["NIFTY"]["ltp"] = 25.0
    cheap["option"]["NIFTY"]["atp_vwap"] = 20.0
    cheap["option"]["NIFTY"]["obi_5"] = 1.8
    cheap["option"]["NIFTY"]["tbq_tsq"] = 1.9
    armed = micro_timing(cheap, "NIFTY")
    assert armed["premium_band_ok"] is True
    assert armed["expiry_armed"] is True


def test_missing_data_is_a_miss_not_an_invented_confirmation() -> None:
    empty = data_gate({}, "NIFTY")
    assert empty["bull_score"] == 0.0
    assert empty["bear_score"] == 0.0
    assert empty["data_confirmed_bullish"] is False
    assert empty["data_confirmed_bearish"] is False
    assert empty["writer_trap_explosion"] is False


def test_badges_render_the_three_specialized_roles() -> None:
    pipeline = build_role_pipeline(_feed(), "NIFTY")
    badges = pipeline["badges"]
    assert badges["d102"].startswith("🧭 102M DIRECTIONAL: 74% BULL")
    assert "25M DATA GATE" in badges["gate25"]
    assert "4M EXPIRY/SNIPER" in badges["sniper4"]
    assert set(pipeline["symbols"]) == {"NIFTY", "BANKNIFTY", "SENSEX"}
