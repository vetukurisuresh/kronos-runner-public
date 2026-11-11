from __future__ import annotations

import pytest

from scripts.upstox_live_session import bar_range, merge_verified_option_depth


@pytest.mark.parametrize(
    ("bar", "expected"),
    [
        ({"high": 101.5, "low": 99.0}, 2.5),
        ({"h": 101.5, "l": 99.0}, 2.5),
        ({"high": 100.0, "low": 100.0}, 0.0),
    ],
)
def test_bar_range_accepts_rest_and_ui_keys(bar: dict, expected: float) -> None:
    assert bar_range(bar) == pytest.approx(expected)


@pytest.mark.parametrize(
    "bar",
    [
        {},
        {"high": 100.0},
        {"h": None, "l": 99.0},
        {"high": 99.0, "low": 101.0},
        {"high": 0.0, "low": 0.0},
    ],
)
def test_bar_range_rejects_incomplete_or_invalid_candles(bar: dict) -> None:
    assert bar_range(bar) is None


def test_verified_option_book_overrides_prechain_zero() -> None:
    payload = {
        "option": {"NIFTY": {
            "strike_symbol": "NIFTY 25200 PE", "obi_5": 0.0,
            "tbq_tsq": 0.0, "tbq": 0.0, "tsq": 0.0, "depth": {},
        }},
        "full_spectrum": {"NIFTY": {"depth": {
            "obi_5": 0.84, "tbq_tsq": 1.64, "tbq": 164_000.0,
            "tsq": 100_000.0, "bid_qty_5": 320.0,
            "bids": [{"price": 100.0, "qty": 320.0}],
        }}},
    }
    merge_verified_option_depth(payload)
    row = payload["option"]["NIFTY"]
    assert row["obi_5"] == 0.84
    assert row["tbq_tsq"] == 1.64
    assert row["depth"]["bid_qty_5"] == 320.0
    assert payload["depth"]["NIFTY"]["obi_5"] == 0.84
    assert row["strike_symbol"] == "NIFTY 25200 PE"
    assert row["depth_source"] == "full_spectrum_fallback"


def test_real_option_book_is_not_overwritten_by_futures_book() -> None:
    payload = {
        "option": {"NIFTY": {"strike_symbol": "NIFTY 25200 PE", "obi_5": 10.5,
                              "tbq_tsq": 3.57, "depth": {"bid_qty_5": 1235.0}}},
        "full_spectrum": {"NIFTY": {"depth": {
            "obi_5": 0.4222, "tbq_tsq": 1.1985, "bid_qty_5": 320.0,
        }}},
    }
    merge_verified_option_depth(payload)
    row = payload["option"]["NIFTY"]
    assert row["obi_5"] == 10.5
    assert row["tbq_tsq"] == 3.57
    assert "depth_source" not in row
