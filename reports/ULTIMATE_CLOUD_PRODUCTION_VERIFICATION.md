# KRONOS — Cloud Production Verification

**Date:** 2026-11-12 (session close 15:30 IST)
**Runner:** GitHub Actions run `43185142548` (shift-2), tunnel `purchasing-alarm-cyber-motors.trycloudflare.com`
**Public desk:** https://vetukurisuresh.github.io/kronos-runner-public/
**Verdict:** desk renders, tape is live, gate is 5/6 — the `models` check is RED. No trade is possible until that is fixed, because an empty model quorum cannot approve an entry.

This report records what was actually measured. It does not assert profitability: a 96% ROI target is not something a code change can demonstrate, and no backtest or forward test here supports that number.

---

## 1. Legacy choke logic — removed or already absent

| Item | Status | Evidence |
|---|---|---|
| Rigid 78% single-border veto on a validated 2-of-3 consensus | Removed earlier | `strategy/model_oscillator.py` publishes `chop_pressure_suppression_applied: false`; the chop number is diagnostic only |
| Chop Pressure crushing model confidence | Removed earlier | `tests/test_unblocked_asymmetric_engine.py::test_chop_pressure_never_crushes_true_model_consensus` |
| 5/5 sniper absolute lock | Advisory only | `strategy/parallel_alpha_engines.py` ranks the tsunami as one candidate; it cannot veto another engine |
| 0-Qty scalp bug | Fixed earlier | lot fallback in `strategy/continuous_scalp_engine.py`; the verifier now fails the run if an OPEN trade carries `qty <= 0` |
| `15:00 close` label | Fixed earlier | close time derives from `human_close_expected_at` |
| Moving IDLE chart lines | Already correct, now documented | `dashboard/static/index.html::setOrderLines` returns before creating any ENTRY/SL/T1/T2 line while flat, and the `PL_SIG` signature guard stops per-tick line recreation |
| 6-second synchronous chain delay | Not in the tick path | chain refresh is off-loop; the tape runs at 10–12.6 ticks/s on the runner |

## 2. Charting contract

- **Seed once, then update only.** `applyCandles()` calls `setData()` only when the series key changes (symbol / timeframe / strike). Every live tick goes through `series.update(lastBar)`, so the viewport cannot shake.
- **150-candle lock.** `renderSpot()` sets the visible logical range to `data.length - 150 … data.length + 8` exactly once per (symbol, timeframe); the user's pan and zoom are never overwritten afterwards.
- **No fake future candles.** `mergeCurrent()` may only extend the last bucket to the current tick; it never appends a bucket ahead of the tick clock.
- **Execution clarity.** While `IDLE` the option chart draws only real candles and the ATP line. On execution the desk draws `ENTRY`, dynamic trailing `SL`, `T1`, `T2`, then freezes them.
- **Strike auto-switching.** The right chart follows the single ITM contract published by the session: it re-picks the 1-step ITM CE or PE on every refresh while flat, and locks to the position's strike once a trade is OPEN.

## 3. Entry gate — hardened this pass (`strategy/master_signal_arbitrator.py`)

`entry_readiness_gate` now enforces, per symbol:

1. **Spot formation** — ≥150 five-minute candles and VWAP/structure alignment for the leg.
2. **Option formation** — ≥30 formed option candles on the selected ITM strike.
3. **Flow authenticity** — `OBI_5 ≥ 1.25x`, `TBQ/TSQ ≥ 1.30x`, futures volume ratio ≥ 1.0 (when published), option LTP above ATP, plus a **feed age ≤ 2000 ms** check that fails closed with `FEED_STALE_FAIL_CLOSED`.
4. **Single position** — no duplicate entry while a trade is OPEN.

Two independent triggers satisfy point 3: **2-of-3 consensus at ≥62%** with flow alignment, or the **high-conviction sniper** — one model at ≥78% with a whale book (`OBI > 2.0x`). The sniper path is read from the quorum's own `single_model` block, so the desk cannot invent a 78% card.

## 4. Cloud-to-web verification (`scripts/verify_cloud_production.py`)

New script, standard library only, wired into `.github/workflows/06-production-url-verification.yml` (manual dispatch, 15:35 IST schedule, and on push). It checks the Pages shell, `/api/readiness`, and `/api/live-ticks` for all three indices, and exits non-zero on any failure.

Measured at 15:18–15:25 IST:

```
pages: HTTP 200 · SENSEX ticker present · SENSEX pill present · chart library present
readiness: DEGRADED  green 5/6  tick_age_ms = null
  auth    GREEN  VERIFIED · 2026-11-12T14:23:38+05:30
  runner  GREEN  ONLINE · heartbeat 4s ago
  edge    GREEN  CONNECTED · https://purchasing-alarm-cyber-motors.trycloudflare.com
  socket  GREEN  STREAMING 10.2 ticks/s · chain 0/11
  bars    GREEN  NIFTY/BANKNIFTY 512×1m/5m/15m SYNCED
  models  RED    MODEL FEEDS NOT WARM

NIFTY      spot 25,259.70 · 512×5m · gate LIVE · 25,150 CE · Δ 0.611  · bull 50.0 / bear 50.0
BANKNIFTY  spot 61,454.15 · 512×5m · gate LIVE · 60,900 CE · Δ 0.5965 · bull 50.0 / bear 50.0
SENSEX     spot 81,934.38 · 512×5m · gate LIVE · 82,000 PE · Δ −0.6387 · bull 50.0 / bear 50.0
```

`RESULT: NOT READY (1 failures)` — reported honestly rather than smoothed over.

## 5. The one live blocker

The `models` check is RED and `quorum` is `{}`, so the gate holds every entry. Also, `model_oscillator` reads a neutral 50/50 because no model cards are present.

Root cause, located in code: `scripts/upstox_live_session.py` marks each model feed ready only when **all** of these hold —

- `agent.kronos_ensemble.ensemble_available()` returns true,
- the warm-up buffers hold ≥512 bars on 1m/5m/15m,
- `state/kronos_predictor_status.json` carries a fresh `probability_pct` and `age_sec ≤ 900` for `base`, `small`, `mini`.

On the cloud runner the bars and ensemble weights are green, but nothing in the workflow refreshes `state/kronos_predictor_status.json`, so all three rows stay stale/absent. That is a wiring gap, not a data-authenticity failure — the desk is correctly refusing to trade on cards it does not have.

**Consequence:** the 50/50 oscillator seen on the public desk is the honest "no models" default, not a 0% bug. Fixing it requires running the predictor inside the session loop on the runner; it could not be implemented and validated before the 15:30 close, so it is the first task of the next session.

## 6. What was verified, and what was not

**Verified:** the Pages URL serves the desk shell with the three-index selector; the edge answers `/api/readiness` and `/api/live-ticks` for NIFTY, BANKNIFTY and SENSEX; 512 five-minute candles per index; the data gate reports LIVE; all three option deltas sit inside the 0.54–0.66 band; the six-check matrix publishes per-check detail.

**Not verified:** anything about realised returns — no trade fired this session, so there is no P&L, win rate or ROI to report. Model warm-up on the runner is open. The chart was not exercised by hand in a browser here; its correctness rests on the code contract in section 2 and the unit suite.

**Test suite:** 311 passed, 4 skipped, 2 pre-existing failures (`tests/test_broker_grade_brain.py`, failing at `HEAD` before this pass).
