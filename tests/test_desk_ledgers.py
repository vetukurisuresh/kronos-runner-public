from market.desk_ledgers import (
    DESK_CAPITAL_INR,
    DESKS,
    build_desks,
    edge_score,
    index_momentum_ranking,
    pick_index_for_desk,
)


def _bars(ranges):
    return [{"high": 100 + r, "low": 100} for r in ranges]


def test_three_desks_each_own_one_lakh_ledger():
    payload = build_desks({}, {})
    assert set(payload["desks"]) == {"directional_100k", "scalping_100k", "zero_to_hero_100k"}
    for desk in payload["desks"].values():
        assert desk["capital_inr"] == DESK_CAPITAL_INR == 100_000.0
        assert desk["equity_inr"] == 100_000.0


def test_desk_pnl_is_isolated_per_desk():
    trades = {
        "directional_100k": [{"status": "DONE", "booked_pnl_inr": 1500}],
        "scalping_100k": [{"status": "RUNNING", "live_pnl_inr": -400}],
    }
    desks = build_desks({}, trades)["desks"]
    assert desks["directional_100k"]["equity_inr"] == 101_500.0
    assert desks["scalping_100k"]["equity_inr"] == 99_600.0
    assert desks["zero_to_hero_100k"]["equity_inr"] == 100_000.0


def test_edge_score_is_bounded_and_zero_without_inputs():
    assert edge_score({})["score"] == 0.0
    strong = edge_score({
        "candles_5m": _bars([1] * 20 + [5, 5, 5]),
        "futures_volume_ratio": 3.0,
        "obi_5": 2.5,
        "tbq_tsq": 2.5,
    })
    assert 0.0 < strong["score"] <= 100.0
    assert strong["score"] >= 90.0


def test_ranking_routes_desk_to_strongest_index():
    feed = {
        "spot": {
            "NIFTY": {"candles_5m": _bars([1] * 23)},
            "BANKNIFTY": {"candles_5m": _bars([1] * 20 + [6, 6, 6])},
            "SENSEX": {"candles_5m": []},
        },
        "full_spectrum": {"BANKNIFTY": {"depth": {"obi_5": 2.0, "tbq_tsq": 2.0}}},
    }
    ranking = index_momentum_ranking(feed)
    assert [row["index"] for row in ranking][0] == "BANKNIFTY"
    assert pick_index_for_desk(ranking)["index"] == "BANKNIFTY"
    assert build_desks(feed, {})["desks"]["directional_100k"]["routed_index"] == "BANKNIFTY"


def test_no_live_edge_routes_nothing():
    assert pick_index_for_desk(index_momentum_ranking({})) is None


def test_desk_specs_have_exit_splits_that_total_100():
    for key, spec in DESKS.items():
        exits = spec["exits"]
        assert exits["t1_pct"] + exits["t2_pct"] + exits["runner_pct"] == 100, key
