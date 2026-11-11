# KRONOS — Today's End-to-End Forensic Audit & Permanent Fix Report

**Date:** 11 November 2026 (Wednesday)
**Market session:** 09:15–15:30 IST
**Audit completed:** post-market, 22:xx IST
**Scope:** Upstox V3 WebSocket ➔ session engine ➔ sanitizer ➔ full-spectrum/chain ➔
3-model collaboration ➔ arbitrator ➔ paper execution ➔ `market/live_ticks.py` ➔
`dashboard/server.py` ➔ `dashboard/static/pro_workspace.js` / `index.html`
**Live-market status at audit time:** **CLOSED**. All runtime validation below is
deterministic/replay validation. The next live session is the final acceptance
test for end-to-end wire latency.

---

## 0. Executive verdict (English)

The user's six complaints were real, and they had **different root causes**.
They were not one “slow dashboard” problem:

1. **Price lag:** the socket itself was fast (sub-millisecond), but the browser
   was fed by a multi-second synchronous chain/model cycle. The last captured
   dashboard heartbeat was **6.536 s** old, while the V3 frame age was a few
   milliseconds.
2. **Blinking gate:** the sanitizer's 3 s cutoff was shorter than the session's
   own 4–6.5 s cycle. A normal heavy cycle therefore looked like a dead feed.
3. **Stuck CE:** the watched option was selected by a weak path vote and was not
   tied to the live directional bias. A bearish tape could stay on a CE chart.
4. **Moving IDLE lines:** the backend published a plan-derived `ENTRY` on every
   tick, and the chart renderer drew `SL/T1/T2` from the same changing plan even
   when no position existed.
5. **Zero orders / zero OBI:** the audited full-spectrum book existed, but the
   selected option row had `0.0` first; `setdefault()` preserved the zero. The
   4M card and narration consumed that stale zero. The 102M card also required
   `[512,6]` verified input while the predictor cache could still be warming.
6. **Probability ceiling:** with `samples=2`, a path vote can only produce
   `0/50/100%`. Dampening then trapped the oscillator around the mid-50s. The
   displayed probability was not a continuous model probability.

The code now contains permanent fixes for all six. The market was already
closed, so this report separates **measured evidence**, **code inspection**, and
**what must still be observed in the next live session**. No price or model
output was fabricated for this report.

## 0.1 Telugu summary (తెలుగు సారాంశం)

నేటి సెషన్‌లో నాలుగు ప్రధాన సమస్యలు నిజంగా ఉన్నాయి: **టిక్ ఆలస్యం**, **UI బ్లింకింగ్**,
**తప్పు CE/PE కాంట్రాక్ట్**, మరియు **కదిలే ENTRY/SL/T1/T2 లైన్లు**. వీటికి కారణం ఒకటే
కాదు; WebSocket వేగంగా ఉన్నా, డాష్‌బోర్డ్‌కు డేటా వెళ్లే దారిలో భారీ chain/model loop
4–6.5 సెకన్లు ఆలస్యం చేసింది. 3 సెకన్ల sanitizer cutoff వల్ల ప్రతి heavy cycle తర్వాత
`BLOCKED/STALE` బ్లింక్ అయ్యింది.

ఇప్పుడు socket tick వచ్చిన వెంటనే `live_fast.json` మరియు fast SSE patch పంపే
event-driven lane చేశాము. భారీ chain/model పని వేరే background cycle లో నడుస్తుంది.
12 సెకన్ల Last-Known-Good window ఉంది; socket బతికి ఉంటే గడిచిన chain వల్ల gate
మధ్యలో BLOCKED అవ్వదు. IDLE లో ENTRY/SL/T1/T2 లేవు—only candles + VWAP/ATP + ఒక
స్థిరమైన trigger zone. Trade ఓపెన్ అయితే fill అయిన ఖచ్చితమైన ధరలు freeze అవుతాయి;
SL మాత్రమే T1/T2 institutional steps లో కదులుతుంది.

Bearish bias ఉంటే ATM PE, bullish bias ఉంటే ATM CE watch చేస్తుంది; active trade
తన contract ను pin చేస్తుంది. `[AUTO] [CE] [PE]` manual watch pin కూడా ఇచ్చాము
(ఇది order routing ను బలవంతంగా మార్చదు). `NIFTY 0 CE` బదులు ఎప్పుడూ నిజమైన
`NIFTY 25200 CE` లాంటి label వస్తుంది.

OBI 0.00x సమస్యకు అసలు కారణం option row లో మొదట 0.0 ఉండి, తర్వాత verified
full-spectrum book `setdefault` వల్ల override కాకపోవడం. ఇప్పుడు verified book
authoritative. 102M కి 512 bars ఇచ్చేలా base lookback 480 ➔ 512 చేశాము.
2-sample path vote మాత్రమే 0/50/100 ఇవ్వగలదు కాబట్టి, model యొక్క p10/p50/p90
quantile CDF నుంచి continuous probability (2%–98%) compute చేస్తున్నాము; 8+ samples
ఉన్నప్పుడే path vote blend అవుతుంది.

**ముఖ్య గమనిక:** మార్కెట్ ముగిసిన తర్వాత audit చేశాము. కాబట్టి latency మరియు
paper lifecycle deterministic/replay పరీక్షలో నిరూపించాము; తదుపరి live session లో
socket-to-browser end-to-end latency ను మళ్లీ measure చేయాలి. ఈ రాత్రికి
“live market లో orders వచ్చాయి” అని చెప్పడం నిజం కాదు.

---

## 1. Evidence used by this audit

| Evidence | Timestamp | Observation |
|---|---:|---|
| `state/live_feed.json` | 14:56:03 IST | NIFTY **25,174.00**, VWAP 25,214.10; watched **NIFTY 25200 CE** ₹239.25; 1m/5m/15m buffers **512/512/512**; socket **True**, **29,423** frames at **6.48 ticks/s** |
| `state/live_feed.json` full spectrum | 14:56:00 IST | Coverage **37/37**, `stale=false`, futures basis **+53.60**, depth book **OBI_5 0.4222**, **TBQ/TSQ 1.1985x**; option row separately **OBI 10.50**, **TBQ/TSQ 3.5747x** |
| `state/kronos_predictor_status.json` | 14:54:05 IST | 3 roles loaded; all used **samples=2**; base **0.0%** (0 up), small **50.0%** (1/2), mini **0.0%** (0/2) |
| Dashboard evidence | 11:03–14:56 | User-captured `HEARTBEAT_AGE_6.536S`; old screenshots show `0 NO_TRADE`, 0.00x OBI and modal gate flicker |
| `reports/model_audit_postfix.json` | 09:33:28 IST | Feed age **3,089 ms** > 3,000 ms; 0/8 audit checks; early-session block |
| `reports/model_audit_phase2.json` | 11:07 IST | Audit 8/8 but model cards still `WAITING_FOR_MODEL_INFERENCE`; proves “512 green ≠ model cache warm” |
| Final runtime proof | 14:56 | Socket 6/6, coverage 37/37, audit 8/8; 3 model roles `LIVE_INFERENCE`; no order, trade state `WATCHING` |

The final feed's `trade` block is **`WATCHING`**, not `OPEN`, for both NIFTY and
BANKNIFTY. Therefore the fixed IDLE chart must have no execution bracket. That
is exactly what the new `chart_levels.levels_state = IDLE_STATIC` enforces.

---

## 2. Data-point delivery matrix (100% trace)

Legend: **Arrived** = present in the socket payload; **Reached full_spectrum** =
used in `strategy/full_spectrum_alpha.build_snapshot`; **Reached model** =
consumed by the 4M/25M/102M or oscillator; **UI** = visible to the browser.

| Upstox V3 field | Arrived today | Reached full_spectrum | Reached model | UI / previous defect | Permanent fix |
|---|---|---|---|---|---|
| Index LTP (`NIFTY`, `BANKNIFTY`) | Yes, 6.48 ticks/s | Yes | Yes | Dashboard lagged 4–6.5 s behind V3 | Event-driven fast frame; every decoded tick notifies the publisher; `fast_overlay` patches the rightmost candle/LTP |
| Index OHLC/volume 1m/5m/15m | Yes; 512 each at close | Yes | Yes | Snapshot rebuilt only on heavy cycle | Fast overlay updates current candle high/low/close; full snapshot still owns history |
| Watched option LTP (`NIFTY 25200 CE`) | Yes, ₹239.25 at 14:56 | Yes | 4M | Stayed on CE while bias was bearish | `choose_watch_side` selects CE/PE; open trade pins exact contract |
| Option 5-level bids/asks | Yes for selected option | Yes, in option row | 4M | `[DATA SYNC]`/4M saw 0.00x while the audit saw a valid book | Canonical merge now overwrites a zero option row with the verified non-zero book; empty depth is replaced only by a non-empty verified book |
| Option OBI_5 | Yes: 10.50 at 14:56 | Yes (option row) | 4M | Pre-chain row default 0.0 survived `setdefault()` | Authoritative non-zero override; test asserts zero → verified book path |
| Option TBQ/TSQ | Yes: 3.5747x | Yes (option row) | 4M | Zero book fed the model | Same authoritative merge for `tbq`, `tsq`, `tbq_tsq` |
| Futures LTP/ATP/volume/amount/OI/basis | Yes; basis **+53.60** | Yes | 102M/25M | Sometimes published `basis -25384` when spot fell back to zero | Session now refuses a zero spot; falls back to socket/engine quote before chain/audit; `basis = fut - spot` only after a real spot |
| Futures depth/OBI | Yes; futures OBI_5 0.4222 | Yes (`full_spectrum.depth`) | Directional engines | Audit's OBI and option OBI were treated as one value | Report and UI now label **futures book** vs **option book**; model 4M card is fed from the option row; no silent substitution |
| 11-strike chain / PCR / max pain / ΔOI | Yes; final audit 37/37 | Yes | 25M/arbitrator | Chain cycle delayed gate | Chain runs in heavy background cycle; gate authority is the fast socket frame |
| Greeks (Δ Γ Θ V ρ IV) | Yes, 6/6 | Yes | 25M/4M | Used to wait for chain | Greeks remain in full-spectrum card; no blocking of price ticks |
| India VIX + heavyweight breadth | Yes; 4/5 aligned at close | Yes | 25M | None | Kept in heavy payload |
| SMC structure | Yes; 1m/5m/15m scanned | Yes | Sniper/oscillator | Structure blocked entries correctly when checklist incomplete | Still fail-closed; not force-green |
| 1m option bars for 4M | Yes; full 512 in engine | Yes | 4M | `0 CE` and 0.00x when option selection failed | Real contract label from `contract_label`; `NIFTY ATM PE/CE` fallback, never `0 CE` |
| 5m/15m spot bars for 102M | 512/512/512 at close | Yes | 102M | Audit green but card said “WAITING FOR 512-BAR MODEL INPUT” | Base lookback corrected **480 → 512**; card language now distinguishes input-ready from inference-pending |
| Model p10/p50/p90 cone | Yes for all 3 roles | N/A | Predictor/oscillator | Path vote only → binary 0/50/100 | Quantile CDF produces continuous 2–98%; path vote blended only at ≥8 samples |
| Probability card / oscillator | 14:54 base 0, small 50, mini 0 | N/A | Arbitrator | 53.9–55.9% ceiling | Continuous probability + new threshold display; no synthetic confidence |

### 2.1 Exact key-mapping bug

The session builds `payload["option"][name]` before it builds the 11-strike
`full_spectrum` snapshot. At that moment the selected contract's depth can be
empty (`0`). The canonical merge used:

```python
option_row.setdefault("obi_5", book.get("obi_5"))
```

`setdefault` does nothing when the key already exists with `0.0`. So the audit
rendered the verified full-spectrum book, while the 4M card and `[DATA SYNC]`
read the zero. The fix in `scripts/upstox_live_session.py` now writes the
verified non-zero values over the zero and replaces an empty option depth only
with a non-empty verified book. This is the **36/37 → 37/37** style key fix,
extended to the actual consumer.

### 2.2 512-bar truth

- At 14:56 the session published `buffer_1m/5m/15m = 512/512/512`.
- The 102M audit requires `shape_5m == [512, 6]` and source `FUT`.
- The predictor's base role previously requested **480** bars (`ROLE_CONFIG`),
  so a green 512 audit could still feed only 480 bars. It now requests **512**.
- A green input buffer does **not** mean inference has completed. The audit now
  says `WAITING_FOR_MODEL_INFERENCE`; narration says
  `512 BARS READY · MODEL INFERENCE PENDING`, not “waiting for 512-bar input.”

---

## 3. Latency bottleneck trace

### 3.1 Observed before

| Stage | Old behaviour | Evidence |
|---|---|---:|
| Upstox V3 frame decode | sub-millisecond to a few ms | V3 frame timestamps; `socket_frame_age_ms` 0–3 ms in healthy snapshots |
| Session main loop | 1 s sleep, but heavy work inside it | `scripts/upstox_live_session.py` old synchronous loop |
| 11-strike chain REST | up to 12 s timeout; repeated every 2 s | `CHAIN_URL` request and sanitizer |
| 3-model scan + narration | serial work on the same loop | dashboard log `3 models scan` |
| `live_feed.json` write + browser poll | next full snapshot; 20 ms watcher was only a fallback | dashboard `HEARTBEAT_AGE_6.536S` |
| Sanitizer cutoff | **3,000 ms** | `market/data_sanitizer.py` |

The exact arithmetic was: a 1 ms socket frame waited behind seconds of chain and
model work; the sanitizer only saw the resulting stale file. That is why a
healthy WebSocket could produce **4.0–6.5 s** UI age.

### 3.2 Architecture after the fix

```
Upstox V3 decoded tick
        │
        ├─ tick_seq += 1; tick_condition.notify_all()      < 1 ms
        │
        └─ FastTickPublisher (event-driven)
                 ├─ write state/live_fast.json              (LTP, option, depth, seq)
                 └─ POST /api/internal/fast-tick           (edge notify, 100 ms cap)
                              │
                    TickHub.publish_fast()
                              │
                    fast_overlay(snapshot)                 (no chain rebuild)
                              │
                    SSE tick_delta / patched full payload

Heavy worker (separate cadence): chain + Greeks + SMC + 3-model scoring
                 └─ full_snapshot → live_feed.json (never blocks fast lane)
```

Key implementation points:

- `SocketWatch` increments `tick_seq` and notifies the publisher on every
  decoded tick, including index, futures and option ticks.
- `FastTickPublisher` waits on the condition; its 500 ms timeout is only a
  fallback. It does not rebuild the 11-strike chain.
- `fast_overlay()` only replaces LTP/current candle/option/depth and the fast
  gate, preserving the heavy chain and SMC payload.
- `/api/internal/fast-tick` is token-authenticated with the existing edge token;
  the dashboard's file watcher is reduced from 20 ms to **10 ms** as a fallback.

**Measured local fast-overlay cost** (500 iterations, this laptop, synthetic
fast frame): **p50 0.122 ms**, **p95 0.235 ms**, max 10.228 ms. This measures the
SSE patch function, not the public internet. The next live session must record
the actual `tick_epoch → browser paint` delta.

**Honest limitation:** a `<10 ms` end-to-end claim is not proven tonight. It is
architecturally possible and the local patch cost is well below 10 ms, but the
wire-to-browser proof requires a live socket.

---

## 4. Why zero orders fired today — gate-by-gate

This session had **zero filled orders**. There was no single “99% signal” that
was suppressed. The exact blockers were layered:

### 09:15–09:40 — transport gate

- `reports/model_audit_postfix.json` at 09:33:28: age **3,089 ms** against the
  3,000 ms cutoff; `FEED_OFFLINE_OR_STALE`; **0/8** audit checks; entries
  suspended.
- Root cause: the heavy loop had not published a fresh heartbeat while the
  socket was still delivering frames. This was the start of the blinking.

### 09:40–11:00 — chain/depth gate

- `reports/model_audit_now.json` at 09:24: feed live, but only **4/8** checks;
  futures source `COMPOSITE/UNAVAILABLE`, depth `0/5` bids/asks.
- `reports/model_audit_phase2.json` at 11:07: audit **8/8**, but model cards
  were `WAITING_FOR_MODEL_INFERENCE`. The cycle had data but no prediction cache.
- The 11-strike chain was present; the selected option row still had zero depth
  because of the `setdefault` bug described in §2.1.

### 11:03–14:56 — probability gate

- `state/kronos_predictor_status.json` at 14:54: three roles loaded, but each
  had `samples=2`:
  - base: `paths_up=0/2` → **0.0%**
  - small: `paths_up=1/2` → **50.0%**
  - mini: `paths_up=0/2` → **0.0%**
- The old damped oscillator could only sit near the mid-50s, below the 72%
  Scalper and 78% Sniper borders. That is the “55.9% ceiling.” It was not a
  model opinion; it was a two-sample artefact.
- The final 14:56 feed's oscillator showed **87.5% downside** because it used
  the bearish path/price structure, but the Sniper checklist was still only
  **2/5**: p10 band false, futures volume 1.15x (needs 300%), OI panic false;
  only OBI and SL cap passed. Therefore the arbitrator correctly did **not**
  buy. `trade.status` remained `WATCHING`.

### Why this is not “force a green gate”

The fix does not turn the Sniper checklist green. It fixes the **false blocks**
(stale price/gate and zero option book) while leaving genuine blockers
(p10 band, futures volume, OI panic, spread, session window) fail-closed.

---

## 5. UI / chart defect trace

### 5.1 Moving IDLE bracket

| Layer | Old behaviour | Fix |
|---|---|---|
| `market/live_ticks.py` | `chart_levels` always carried plan `entry/sl/t1/t2`; plan was re-anchored per tick | IDLE now emits only `atp`, p10/p50/p90 reference, and one `trigger_zone` anchored to the **last closed 5m candle**; no `entry/sl/t1/t2` keys |
| `dashboard/static/index.html` `setOrderLines()` | read `LIVE.trade` or chart levels and drew all four lines even when `status != OPEN` | returns after ATP when no OPEN trade; execution bracket only for an open position |
| `dashboard/static/pro_workspace.js` `updateLevels()` | wrote `lv.entry`, `lv.sl`, `lv.t1`, `lv.t2` into visible inputs every tick | missing values now clear the inputs; no plan fallback |
| `dashboard/static/pro_enterprise.js` `currentLevels()` | fell back to the last closed trade's remembered prices | only trusts `LIVE.trade` when status is exactly `OPEN`; otherwise uses backend chart levels (empty in IDLE) |

The trigger anchor is the **second-last 5m bar** (last fully closed bar), so it
does not move until a new 5m candle forms. It is displayed as a single static
zone, not four chasing lines.

### 5.2 Chart viewport reset

- `applyVolumes()` previously called `series.setData(rows)` on every tick. That
  recreates the histogram data and can reset the viewport.
- It now calls `series.update(lastBar)` when the tail timestamp is unchanged,
  and only uses `setData()` when the series identity/first load genuinely
  changes. The viewport stays where the user put it.
- `renderOption()` calls `setData()` only when the series key changes
  (`symbol`, timeframe, or selected contract). A dynamic CE↔PE change is a real
  series change; ordinary ticks use `update()`.

### 5.3 Blinking gate

- `fast_overlay()` now makes a fresh fast frame the gate authority.
- Full-spectrum has a **12 s** LKG window (`FULL_SPECTRUM_STALE_MS` and
  `LKG_HOLD_MS`); a 9 s-old chain while the socket is alive is labelled
  `lkg_held`, not `BLOCKED`.
- `data_gate` carries `chain_age_ms`, `chain_stale`, `lkg`, and
  `lkg_window_ms`; the UI can honestly say `CHAIN LAG · FAST GATE LIVE` instead
  of flipping every modal red.

### 5.4 Stuck CE / wrong contract

- Old: watched side came from a path vote, with no active-trade lock and no
  manual pin.
- New: `choose_watch_side()` priority is:
  1. **OPEN trade** → its exact `option_type` (CE or PE);
  2. **manual pin** → CE/PE;
  3. **model bias** → downside > upside selects PE; upside >= downside selects CE;
  4. **bearish fallback** → PCR < 0.82 + below VWAP + negative OBI selects PE;
  5. otherwise CE.
- `contract_label()` never emits `0 CE`; it returns the real strike label or
  `NIFTY ATM PE/CE` when the strike is unavailable.
- The right-chart toolbar now exposes **AUTO / CE / PE**. The pin is a dashboard
  watch preference only; it does not override risk or force an order.

---

## 6. Permanent fixes implemented

### Fix 1 — Fast lane

- `scripts/upstox_live_session.py`:
  - `SocketWatch.tick_seq` + `tick_condition`;
  - every decoded tick notifies the publisher;
  - `FastTickPublisher` waits on the tick condition, writes
    `state/live_fast.json` and invokes `CloudEdge.notify_fast()`.
- `market/cloud_edge.py`: `notify_fast()` posts the token-authenticated
  `/api/internal/fast-tick` endpoint with a 100 ms upper bound.
- `dashboard/server.py`: `TickHub.publish_fast()`, token-authenticated fast
  route, 10 ms file watcher fallback.
- `market/live_ticks.py`: `fast_overlay()` patches only volatile fields.

### Fix 2 — Anti-flicker/LKG

- 12 s LKG window; LKG fields visible to the UI;
- fast gate authority prevents stale heavy snapshots from painting `BLOCKED`;
- real dead socket still fails closed.

### Fix 3 — Dynamic CE/PE + pin

- `market/option_picker.py`: `choose_watch_side()` and `contract_label()`;
- active trade pins strike and leg until close;
- manual `[AUTO] [CE] [PE]` API and toolbar.

### Fix 4 — Locked levels/static chart

- IDLE backend removes execution levels; UI clears them;
- open trade freezes `entry/sl/t1/t2`;
- `applyVolumes` no longer `setData` on every tick;
- only real series changes reset data; periodic ticks use `update`.

### Fix 5 — OBI/512/probability

- verified full-spectrum depth overrides a zero option row;
- base 102M lookback **480 → 512**;
- quantile CDF continuous probability (2–98%); path vote only blended at ≥8
  samples;
- `probability_source` / `quantile_cdf_pct` expose provenance.

### Fix 6 — Lifecycle test

- `tests/test_today_full_lifecycle_audit.py` covers:
  - PE on bearish flow and CE on bullish flow;
  - manual pin, active-trade lock, safe labels;
  - continuous probability, not 0/50/100;
  - IDLE levels absent and trigger anchor stable across a price change;
  - T1 50% booking, SL to cost + ₹1.50, T2 closure;
  - manual close;
  - fast LKG overlay keeps gate green and reports small age.

---

## 7. Verification table (before vs after)

| Check | Before | After | Evidence |
|---|---|---|---|
| Fresh tick waits for heavy chain | 4.0–6.5 s | event publisher wakes on `tick_seq`; local patch p50 **0.122 ms**, p95 **0.235 ms** | `HEARTBEAT_AGE_6.536S`; benchmark; code paths |
| UI gate during heavy cycle | `BLOCKED/STALE` blinking | fast frame authority + 12 s LKG; `lkg_held` honest label | `fast_overlay` gate assertions |
| IDLE ENTRY/SL/T1/T2 | moved with every plan tick | absent from `chart_levels`; UI clears; only ATP + static trigger | `test_idle_levels...`; `IDLE_STATIC` |
| Bearish option | stuck CE (`NIFTY 25200 CE`) | ATM PE on bearish bias; CE on bullish; open trade locked | `test_bearish_flow...` |
| `0 CE` label | possible when strike=0 | `NIFTY ATM CE/PE`; real label when strike exists | `contract_label` tests |
| Option OBI fed to 4M | zero preserved by `setdefault` | verified non-zero book wins; empty book not accepted as real | session merge; depth test |
| 102M bars | 480 requested | **512** requested, same 512 cap | `agent/kronos_ensemble.ROLE_CONFIG` |
| 2-sample probability | 0/50/100 then damped ~55.9 | continuous 2–98 from quantile CDF; path vote only ≥8 | `test_quantile...` |
| Paper lifecycle | not covered end to end | T1→BE→T2→close deterministic | `test_t1...` passes |
| Python syntax | — | 6 changed Python modules compile | `py_compile` |
| JS syntax | — | `pro_workspace.js`, `pro_enterprise.js`, all 9 inline HTML scripts parse | `node --check` |
| Pin API | no endpoint | GET/POST `AUTO/CE/PE` round trip; invalid mode 400 | local dashboard test on port 8099 |

### Commands actually run

```bash
python -m pytest -q tests/test_today_full_lifecycle_audit.py \
  tests/test_health_and_ledger.py tests/test_ai_command_center.py \
  tests/test_programmatic_scanner_deck.py tests/test_live_ticks.py \
  tests/test_live_session_helpers.py tests/test_tick_engine.py
# 50 passed

python -m pytest -q tests/test_today_full_lifecycle_audit.py
# 5 passed

python -m py_compile scripts/upstox_live_session.py market/live_ticks.py \
  market/option_picker.py model/live_predictor.py model/tri_model_orchestrator.py \
  agent/kronos_ensemble.py dashboard/server.py market/cloud_edge.py
# exit 0

node --check dashboard/static/pro_workspace.js
node --check dashboard/static/pro_enterprise.js
# exit 0; all 9 inline <script> blocks also parsed

git diff --check
# clean
```

---

## 8. What remains unverified / risks

1. **No live-market order was executed tonight.** Market closed at 15:30 IST;
   the session is not running. The lifecycle test is deterministic, not a live
   broker fill.
2. **Socket-to-browser latency must be measured in the next session.** The fast
   lane is event-driven and the local patch is sub-millisecond at p95, but the
   browser paint path over the tunnel requires a live frame and a live client.
3. **Model inference is still expensive.** `samples=2` is sufficient for a
   direction card but does not give a high-confidence forecast. The probability
   is now continuous and honest; it does not manufacture a 78% signal.
4. **The 4M option book can legitimately be empty** when the selected contract
   has no five-level depth. The UI will show zero and the model will not invent
   a book; this is correct fail-closed behaviour.
5. **Historical zero values remain in old artifacts** under `reports/`. They are
   evidence of the old bug, not live state. The report deliberately does not
   hide them.
6. **Full test suite** was not run end-to-end tonight; the focused suite covers
   the changed paths. A broader suite should be run before the next live open.

---

## 9. Next-session acceptance checklist

- [ ] Start session before 09:15 IST with `KRONOS_AUDIT_SAMPLES` appropriate for
      the machine; confirm `socket_frames` rises every second.
- [ ] Open dashboard; verify NIFTY and option LTP change on the same tick as
      the Upstox terminal (record `tick_epoch` to browser paint).
- [ ] Confirm no `HEARTBEAT_AGE > 1s` while socket frames are arriving.
- [ ] Confirm IDLE chart has candles + ATP + one trigger zone, no ENTRY/SL/T1/T2.
- [ ] On a bearish tape, confirm the watched label switches to ATM PE; on a
      bullish tape, ATM CE. Test `[CE] [PE] [AUTO]` on the live dashboard.
- [ ] Confirm `[DATA SYNC]` and 4M show the same non-zero OBI/TBQ as the verified
      option book; no `0 CE`.
- [ ] Confirm 102M card shows 512 bars and a model card, not a false 512 wait.
- [ ] Let a paper trade fire; verify T1 50%, SL to cost + ₹1.50, T2, and the red
      Close Position button.
- [ ] Capture final screenshots and a fresh API poll for the journal.

---

## 10. Bottom line

**English:** Today's failures were architectural, not market failures. The
WebSocket was fast; the application made it slow. The UI drew intent as if it
were an execution bracket. The option selector followed a weak path vote, not
the live direction. A zero-valued option row shadowed a verified book. A
two-sample forecast was displayed as a confidence percentage. Those five
architectural faults are fixed in code, and the paper lifecycle is now
deterministic. What cannot be claimed tonight is a live-market fill or a measured
wire-to-browser latency; both require the next open.

**తెలుగు:** నేటి సమస్యలు మార్కెట్ తప్పు కాదు—అప్లికేషన్ ఆర్కిటెక్చర్ తప్పు. WebSocket
వేగంగా ఉంది; దాన్ని భారీ synchronous loop నెమ్మదించింది. IDLE లో trade ఉన్నట్టు
లైన్లు గీసింది; option selector direction ను కాక path vote ను అనుసరించింది;
verified book ను 0.0 override చేసింది; 2-sample forecast ను confidence లా చూపింది.
ఈ ఐదూ ఇప్పుడు code లో fix అయ్యాయి, lifecycle test deterministic గా pass అయింది.
కానీ ఈ రాత్రి live fill లేదా live wire-to-browser latency నిరూపించలేము—అది
తదుపరి market open లోనే చేయాలి.

*Report generated from the actual repository state and recorded artifacts; no
price, fill, or model probability was synthesized for this document.*