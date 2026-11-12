# KRONOS Autonomous Cloud Takeover — Live Engineering Checklist

**Window:** 13:20 IST → 15:30 IST market close (12 Nov 2026)
**Execution policy:** 0% local machine. All streaming, modelling and verification runs on GitHub Actions runners and is published to the public Pages terminal.

Status legend: `[x]` implemented and verified by a check I actually ran · `[~]` implemented, verification pending a cloud run / live market data · `[ ]` not done.

---

## MODULE 1 — Cloud-runner execution, zero local load

- [x] Local `scripts/upstox_live_session.py` stopped (three orphaned PIDs terminated: 4072, 6724, 7908).
- [x] Local `dashboard/server.py` stopped (PID 15596).
- [x] Root cause of the respawn documented: the Windows Startup shortcut
      `KRONOS_Autonomous_Desk.vbs` re-launches the session with a countdown
      (`--minutes 152 → 124 → 120 → 116`). It lives **outside** the repository,
      so it was not edited; the session is killed again whenever it reappears.
- [x] Tests and syntax checks run locally as *lightweight* checks only (no model
      server, no headless browser). All heavy streaming stays on the runners.
- [x] `02-live-market-edge.yml` owns the Upstox V3 socket, the SSE server and the
      Cloudflare quick tunnel; `deploy-pages.yml` publishes the static terminal.
- [x] `scripts/publish_edge_url.py` publishes the active `EDGE_BASE_URL` to
      Firebase **and** the `edge-live` branch so the public terminal discovers the
      tunnel on first paint.

## MODULE 2 — Three-index live coverage

- [x] `config/settings.py` now owns one `INDEX_SPECS` table: NIFTY 50
      (`NSE_INDEX|Nifty 50`, lot 75, step 50), BANKNIFTY (`NSE_INDEX|Nifty Bank`,
      lot 30, step 100) and SENSEX (`BSE_INDEX|SENSEX`, lot 20, step 100, BFO).
      `index_spec() / index_lot_size() / index_strike_step()` are the only accessors.
- [x] Websocket universe: `market/upstox_tick_engine.py` `INDEX_KEYS` and
      `market/live_ticks.py` `INDEX_KEYS` are generated from that table.
- [x] Session universe: `scripts/upstox_live_session.py` `KEYS` is generated from
      the table; `resolve_futures()` resolves NSE futures from the NSE master and
      SENSEX futures from the BSE master (`load_bse_instruments`).
- [x] Intrinsic universe: `register_universe()` subscribes ATM−1/ATM/ATM+1/ATM+2
      CE & PE per index on that index's own strike grid.
- [x] `dashboard/server.py` SSE fan-out now serves `NIFTY`, `BANKNIFTY`, `SENSEX`.
- [x] Header: third live ticker (`proSensexLtp` / `proSensexDelta`).
- [x] Toolbar: `[NIFTY 50] [BANKNIFTY] [SENSEX]` selector pills; the pill handler
      is no longer hardcoded to two symbols.
- [~] Live 3-index tick proof on the public URL: depends on the runner's Upstox
      token for today (see MODULE 5).

## MODULE 3 — Smart ITM strike + candle-formation gate

- [x] `market/option_picker.py::pick_itm_strike()` — CE selects the strike one
      grid step **below** spot, PE one step **above**; Greek band 0.54–0.66
      (`ITM_DELTA_LOW/HIGH`), nearest-to-0.60 wins, ties fall back to the
      structural strike (`delta_source="STRUCTURAL"`) so a chain without Greeks
      still renders. Verified: spot 25,356 + CE → `NIFTY 25300 CE`.
- [x] `scripts/upstox_live_session.py` uses that picker for the traded contract
      (`strike` feeds `selection_snapshot` and the option subscription).
- [x] `strategy/intra_candle_sniper.py::option_candle_readiness()` — counts only
      real OHLC prints (`high > low > 0`, `close > 0`) and blocks the entry until
      ≥ 30 candles have formed. `ready` now requires it; the desk reports
      `HOLDING_ON` with `n/30 candles loaded` instead of firing blind.
- [x] `strategy/master_signal_arbitrator.py::entry_readiness_gate()` — the strict
      4-point gate: Spot 5m (≥150 bars + VWAP/prior-high structure), ITM option
      chart (≥30 formed candles), 2-of-3 quorum at ≥62% with OBI ≥1.20x and
      TBQ/TSQ ≥1.25x, and a single-position lock. Published as
      `master_arbitrator.readiness`, and `trade_approved` now requires it.
- [x] Post-entry lifecycle unchanged and still frozen at execution:
      SL 6–7% → +1R breakeven → T1 35% → T2 35% → 30% 5m-EMA9 runner.

## MODULE 4 — Self-driven defect To-Do

- [x] **1 Declutter UI** — the model cards + proof board already live in the
      toggleable `📊 Models & Proof` drawer; conditional boxes were removed from
      the chart bodies in the previous pass, so the 50/50 chart pair keeps the
      full height.
- [x] **2 Smart Scalper ₹35K** — `NEXT SCALP QTY` no longer renders `0 Qty`; it
      falls back to the live lot (75 / 30 / 20) funded by the ₹35K sleeve, and the
      cooldown reads 15s (`HOUSE_MONEY.COOLDOWN_SECONDS` 90 → 15,
      `ADAPTIVE_SCALPER.COOLDOWN_SECONDS` 90 → 15).
- [x] **3 Reversal & SMC Sniper ₹65K** — candidate rows render their real
      0–100 edge scores from the arbitrator, the 5-point checklist binds
      `✅ PASS / ⏳ WAIT` to the live sniper checks, and Greek Taylor shows the
      option ₹ targets (T1/p65, T2/p90).
- [x] **4 Full-Spectrum & IV** — IV is normalised once (`iv_percent()`): a
      decimal `0.1718` renders as `17.18%`, never `0.2%`. `option_chart` carries
      `iv` (percent) + `iv_raw`. Breadth `aligned/total` is present in the audit
      sample and flows to the brain log.
- [x] **5 Models & proof** — the quorum has a live deterministic fallback
      (`_ensure_live_quorum`) so cards never show `—%`; `5m ΔOI PCR` is clamped to
      `[-5, +5]` at its single source (`chain_metrics`); `ENTRY/SL/T1/T2` stay
      hidden while flat; the proof board only publishes **today's** rows, which
      purges the orphan `OPEN` history; the proof timer shows the entry's own
      `HH:MM 5m close` instead of a hardcoded `15:00`.

## MODULE 5 — Continuous cloud loop until 15:30 IST

- [~] Commit + push to `vetukurisuresh/kronos-trading-brain` (this change set).
- [~] Mirror the same tree into `vetukurisuresh/kronos-runner-public` (Pages).
- [~] Dispatch / verify `02-live-market-edge.yml` and `Deploy dashboard to GitHub Pages`.
- [~] Poll runner telemetry + the public URL and fix anomalies in-flight.
- [~] Live paper-trade / EOD P&L statement for today.

### Honest limitations

- This checklist is a **single engineering pass**, not a background daemon. The
  agent cannot keep polling after the turn ends; the scheduled workflows
  (`02-live-market-edge.yml` 08:32 / 12:35 IST, `deploy-pages.yml`) are what keep
  the desk alive until 15:30.
- The two `test_broker_grade_brain` failures are pre-existing at `HEAD` and are
  unrelated to this change set (verified on a clean worktree).
- `state/live_feed.json` is written by the local session; while that session was
  running, feed-dependent tests read a half-written snapshot. The suite passes on
  the committed fixture once local execution is stopped.
