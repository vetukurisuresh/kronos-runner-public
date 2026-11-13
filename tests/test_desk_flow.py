"""Desk-flow derivation: ranking, handshake, ledgers, and no invention."""

from __future__ import annotations

from strategy.desk_flow import build_desk_flow, index_edge_scores


def _gate(**over):
    base = {
        "pcr": 1.42, "delta": 0.61, "gamma": 0.0021, "obi_5": 1.85,
        "data_confirmed_bullish": True, "data_confirmed_bearish": False,
        "bull_score": 100.0, "bear_score": 20.0, "writer_trap_explosion": True,
    }
    base.update(over)
    return base


def _payload() -> dict:
    return {
        "model_oscillator": {"directional_probability_pct": 82.0, "directional_side": "BULL"},
        "role_pipeline": {
            "primary": "NIFTY",
            "gate_25m": _gate(),
            "micro_4m": {"obi_5": 1.62, "tbq_tsq": 1.71, "mode": "SHOCKWAVE_VELOCITY_SCALP"},
            "symbols": {"NIFTY": {"gate_25m": _gate(), "micro_4m": {"obi_5": 1.62, "tbq_tsq": 1.71, "mode": "SHOCKWAVE_VELOCITY_SCALP"}},
                        "BANKNIFTY": {"gate_25m": _gate(obi_5=1.30, bull_score=70.0, bear_score=40.0)}},
        },
        "intra_candle": {"NIFTY": {"armed": True}},
        "desks": {
            "directional_100k": {"capital_inr": 100000.0, "equity_inr": 104850.0,
                                 "booked_pnl_inr": 4850.0, "routed_index": "NIFTY",
                                 "routed_edge_score": 88.0,
                                 "trades": [{"id": "TRD-01", "symbol": "NIFTY", "strike": 25300,
                                             "side": "CE", "entry": 216.4, "status": "DONE", "pnl": 4850}]},
            "scalping_100k": {"capital_inr": 100000.0, "equity_inr": 102190.0, "booked_pnl_inr": 2190.0},
            "zero_to_hero_100k": {"capital_inr": 100000.0, "equity_inr": 100000.0},
        },
    }


def test_index_edges_rank_the_strongest_book_first() -> None:
    edges = index_edge_scores(_payload())
    assert edges["NIFTY"]["score"] > edges["BANKNIFTY"]["score"]
    assert edges["SENSEX"]["score"] is None          # no data -> no invented score


def test_directional_handshake_stamps_all_three_models() -> None:
    flow = build_desk_flow(_payload())
    desk = flow["desks"]["directional_100k"]
    assert [node["ok"] for node in desk["nodes"]] == [True, True, True]
    assert [node["mark"] for node in desk["nodes"]] == ["✅", "✅", "✅"]
    assert desk["handshake"] is True
    assert desk["side"] == "CE"


def test_scalping_and_zero_to_hero_use_their_own_gates() -> None:
    flow = build_desk_flow(_payload())
    assert flow["desks"]["scalping_100k"]["handshake"] is True
    assert flow["desks"]["scalping_100k"]["mode"] == "SHOCKWAVE_VELOCITY_SCALP"
    assert flow["desks"]["zero_to_hero_100k"]["handshake"] is True


def test_three_ledgers_hold_one_lakh_each_and_trades_pass_through() -> None:
    flow = build_desk_flow(_payload())
    assert [d["capital_inr"] for d in flow["desks"].values()] == [100000.0] * 3
    assert flow["total_capital_inr"] == 300000.0
    trade = flow["desks"]["directional_100k"]["trades"][0]
    assert trade["id"] == "TRD-01" and trade["pnl"] == 4850


def test_empty_payload_never_fabricates_a_handshake() -> None:
    flow = build_desk_flow({})
    assert flow["ranking"] == []
    assert all(desk["handshake"] is False for desk in flow["desks"].values())
    assert all(node["mark"] == "⏳" for desk in flow["desks"].values() for node in desk["nodes"])


def test_index_momentum_ranking_is_ordered_and_typed() -> None:
    flow = build_desk_flow(_payload())
    ranking = flow["index_momentum_ranking"]
    assert [row["rank"] for row in ranking] == [1, 2]
    assert ranking[0]["symbol"] == "NIFTY"
    assert ranking[0]["score"] > ranking[1]["score"]


def test_eod_reconciler_keeps_three_independent_one_lakh_ledgers() -> None:
    from state.eod_pnl_reconciler import reconcile_desks

    out = reconcile_desks(_payload()["desks"])
    assert sorted(out) == ["directional_100k", "scalping_100k", "zero_to_hero_100k"]
    assert sum(row["capital_inr"] for row in out.values()) == 300000.0
    assert out["directional_100k"]["trades_closed"] == 1
    assert out["directional_100k"]["total_inr"] == 4850.0
