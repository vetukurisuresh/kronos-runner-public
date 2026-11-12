# KRONOS Autonomous Cloud Takeover — Live Engineering Checklist

**Window:** 13:20 IST → 15:30 IST market close (12 Nov 2026)
**Execution policy:** 0% local machine. Streaming, modelling and verification run on the GitHub Actions runner and are published to the public Pages terminal.

Status: `[x]` implemented **and** verified by a check I actually ran · `[~]` implemented, verification blocked by data/time · `[ ]` not done.

---

## VERIFIED LIVE EVIDENCE (14:00 IST, runner `43181458586` = shift-2)

Tunnel: `https://hero-christ-adopt-parish.trycloudflare.com` (published to
Firebase `/system/live_stream_url.json` at 13:58:03 IST, `LIVE_STREAMING`, shift-2).

```
GET /api/live-ticks?symbol=NIFTY      indices: NIFTY 25,268.55 · BANKNIFTY 61,491.05 · SENSEX 81,949.46
GET /api/live-ticks?symbol=BANKNIFTY  spot 61,481.95 · 512 × 5m · 512 × 1m · socket 10.23 ticks/s
GET /api/live-ticks?symbol=SENSEX     spot 81,948.04 · 512 × 5m · 512 × 1m · option 304 × 5m
data_gate: LIVE / VERIFIED_LIVE       (all three symbols)
```

- **Three-index stream:** NIFTY 50, BANKNIFTY and SENSEX all arrive on one socket at ~10 ticks/s.
- **SENSEX BFO option chain resolved:** `SENSEX 81900 CE`, BSE_FO instrument `821729`, delta **0.5543** (inside the 0.54–0.66 band), IV **35.43%** (percent, not `0.35`).
- **1-step ITM rule proven:** spot 81,948.04 → strike 81,900 (one 100-point grid step *below* spot for a CE).
- **Live SENSEX paper scalp is open:** entry ₹186.10 → ₹206.95 (+₹4,078.50 live), 180 qty (9 × 20 lot), T1 35% booked at ₹212.20 for +₹1,618.20 realised, stop trailed to ₹187.60 at 13:50:20 IST.
- **New 4-point gate is speaking:** `master_arbitrator.entry_ready=false`, `readiness.reason=HOLDING_ON_SPOT_5M_QUORUM_FLOW` — the desk holds instead of firing blind, with the exact blocking point named.
- **Proof ledger purged:** `proof_ledger` now returns only today's row(s) — the orphan `OPEN` history is gone.
- **Public terminal rebuilt:** `https://vetukurisuresh.github.io/kronos-runner-public/` returns HTTP 200 (332,843 bytes) and renders `id="proSensexLtp"` plus `data-pro-index="SENSEX"`.

---

## MODULE 1 — Cloud-runner execution, zero local load

- [x] Local `scripts/upstox_live_session.py` stopped — three orphaned PIDs terminated (4072, 6724, 7908) as a Windows Startup shortcut re-launched each one.
- [x] Local `dashboard/server.py` stopped (PID 15596).
- [x] Respawn root cause identified: `%APPDATA%\...\Startup\KRONOS_Autonomous_Desk.vbs` (created 10:39 today) launches the session with a countdown (`--minutes 152 → 124 → 120 → 116`). It lives **outside** the repository, so it was not edited; the session was killed again on each reappearance.
- [x] All streaming now runs on the runner: Upstox V3 socket → sanitizer → SSE server → Cloudflare quick tunnel.
- [x] Tunnel auto-discovery verified: the runner published its URL to Firebase and the Pages terminal reads that record.
- [x] Local work limited to Python syntax checks and the pytest suite — no local model server, no local headless browser.

## MODULE 2 — Three-index live coverage

- [x] Single `INDEX_SPECS` table in `config/settings.py`: NIFTY 50 (`NSE_INDEX|Nifty 50`, lot 75, step 50), BANKNIFTY (`NSE_INDEX|Nifty Bank`, lot 30, step 100), SENSEX (`BSE_INDEX|SENSEX`, lot 20, step 100, BFO). Accessors: `index_spec / index_lot_size / index_strike_step / index_instrument_key`.
- [x] Socket universe derived from it (`market/upstox_tick_engine.INDEX_KEYS`, `market/live_ticks.INDEX_KEYS`).
- [x] Session universe derived from it (`scripts/upstox_live_session.KEYS`); SENSEX futures resolved from the BSE master (`load_bse_instruments`).
- [x] `register_universe()` subscribes ATM−1/ATM/ATM+1/ATM+2 CE & PE per index on that index's own grid, with integer strike labels.
- [x] SSE fan-out serves `NIFTY`, `BANKNIFTY`, `SENSEX` (`dashboard/server.STREAM_SYMBOLS`).
- [x] Header shows three live tickers (`bigSpot`, `proBankLtp`, `proSensexLtp`) with day change %.
- [x] Toolbar carries `[NIFTY 50] [BANKNIFTY] [SENSEX]` pills; the click handler is symbol-agnostic.
- [x] Live proof: all three LTPs in one payload (above).

## MODULE 3 — Smart ITM strike + candle-formation gate

- [x] `market/option_picker.pick_itm_strike()` — CE one grid step **below** spot, PE one step **above**; Preferences the 0.54–0.66 delta band nearest 0.60; falls back to the structural strike with `delta_source="STRUCTURAL"` when the chain has no Greeks.
- [x] Unit check: spot 25,356 + CE → `NIFTY 25300 CE`. Live check: spot 81,948 + CE → `SENSEX 81900 CE` @ Δ 0.5543.
- [x] Session trades the picker's strike (`strike` from `pick_itm_strike` feeds `selection_snapshot` and the subscription).
- [x] `strategy/intra_candle_sniper.option_candle_readiness()` — counts only real prints (`high > low > 0`, `close > 0`), requires ≥ 30, and blocks `ready`. Reason becomes `HOLDING_ON… n/30 candles loaded`.
- [x] `strategy/master_signal_arbitrator.entry_readiness_gate()` — the 4-point gate (Spot 5m ≥150 bars + VWAP/prior-high structure · ITM option ≥30 formed candles · 2-of-3 quorum ≥62% with OBI ≥1.20x and TBQ/TSQ ≥1.25x · single position). Exposed as `master_arbitrator.readiness`; `trade_approved` requires it.
- [x] Post-entry lifecycle untouched and still frozen at execution: small SL 6–7% → +1R breakeven → T1 35% → T2 35% → 30% 5m-EMA9 runner.

## MODULE 4 — Self-driven defect To-Do

- [x] **1 Declutter UI** — model cards + proof board stay inside the toggleable `📊 Models & Proof` drawer; the chart panes carry no dark condition boxes, so the 50/50 chart pair keeps the height.
- [x] **2 Smart Scalper ₹35K** — `NEXT SCALP QTY` falls back to the live lot (75 / 30 / 20) funded by the ₹35K sleeve instead of `0 Qty`; cooldown 90s → **15s** (`house_money_compounder.COOLDOWN_SECONDS`, `adaptive_scalper.COOLDOWN_SECONDS`), and the UI strings say 15s.
- [x] **3 Reversal & SMC Sniper ₹65K** — candidate rows render the arbitrator's real 0–100 edge scores, the 5-point checklist binds `✅ PASS / ⏳ WAIT` to live sniper checks, Greek Taylor shows option ₹ T1/p65 and T2/p90.
- [x] **4 Full-Spectrum & IV** — `iv_percent()` normalises once: decimal `0.1718` → `17.18%`; `option_chart` carries `iv` (percent) + `iv_raw` + `iv_unit`. Live: SENSEX IV reads **35.43%**. Breadth `aligned/total` present in the audit sample.
- [x] **5 Models & proof** — deterministic live quorum fallback (`_ensure_live_quorum`); `5m ΔOI PCR` clamped to `[-5, +5]` at its single source (`strategy/full_spectrum_alpha.chain_metrics`); ENTRY/SL/T1/T2 hidden while flat; proof board publishes **today only** (orphans purged — verified); proof timer shows the entry's own `HH:MM 5m close` instead of `⏳ 15:00 close`.

## MODULE 5 — Continuous cloud loop until 15:30 IST

- [x] Committed + pushed to `vetukurisuresh/kronos-trading-brain` (`73e1229`).
- [x] Mirrored + pushed to `vetukurisuresh/kronos-runner-public` (`697d4f2`) on top of the runner's latest published commit.
- [x] `Deploy dashboard to GitHub Pages` re-run and succeeded; `pages-build-deployment` published; the live URL serves the SENSEX build.
- [x] `02-live-market-edge.yml` shift-2 dispatched (`43181458586`), passed the IST gate, and owns the tunnel.
- [x] Old shift-1 (`43163158133`, pre-SENSEX code) cancelled after shift-2 published, so one runner is authoritative.
- [~] Polling until 15:30: this agent cannot stay resident after the turn. The **scheduled** workflows keep the desk alive; the next cron is 15:05 UTC (20:35 IST) for shift 2 of the following session.

### Honest limitations

- This is one engineering pass, not a background daemon. Nothing continues polling after the turn ends.
- BANKNIFTY's rendered watch contract can still land on an older scalp-resolver strike (`60900 CE`, Δ 0.4756) while NIFTY/SENSEX land in the ITM band — the scalp-contract path (`select_scalp_contract`) is a separate resolver and was not re-pointed at the ITM picker in this pass.
- `test_broker_grade_brain` has 2 failures that also fail at `HEAD` (verified on a clean worktree); they are unrelated to this change set.
- `state/live_feed.json` is written by the local session; while that session ran, feed-dependent tests read a half-written snapshot. On the committed fixture the suite is **308 passed, 4 skipped, 2 pre-existing failures**.
