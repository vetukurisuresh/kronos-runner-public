# KRONOS Morning Auto-Wakeup & Live Audit — 12 Nov 2026

**Status:** LOCAL LIVE / CLOUD RELAY BEING RESTARTED  
**Report generated:** 12 Nov 2026, ~11:07 IST  
**Scope:** why the 12 Nov morning automation stayed silent, the three-layer auto-ignition fix, live market proof, UI defect verification, tests, and deployment.

---

## 1. Why today (12 Nov) was silent

The failure was **not a missing or expired Upstox token** and **not a dead local market feed** at the time of the audit.

- The token probe returned **HTTP 200** for `SURESH RAJU VETUKURI`; saved `2026-11-12T09:23:33+0530`, valid through 13 Nov 03:30 IST.
- Local `scripts/upstox_live_session.py` was alive and the local feed was live during the investigation.
- The morning GitHub workflows did **not** execute on 12 Nov:
  - `01 Morning Upstox Auth` last relevant run was **11 Nov 2026 09:56:51Z** (`42999437574`).
  - No `02 Live Market Edge` run was started this morning before the manual intervention.
  - The GitHub scheduler omitted/delayed the 08:25 IST cron, and there was **no explicit `01 → 02` dispatch** to recover from that omission.
- Two old `02` runs from commit `e6ce1aa` were still `in_progress` (`43157596109` private, `43157603829` public). They were running stale code and each held an Upstox market-data socket.
- Upstox allows **two market-data sockets per user/token**. Two old cloud runners plus a restarted local session produced `WebSocketBadStatusException: 403 Forbidden` locally. Cancelling both stale runners immediately restored the local socket (`FEED_LIVE`, 469 frames, then 3,700+ frames).

### Root cause in one sentence

**The cron did not fire, and the old architecture had no independent `01 → 02` hand-off, no inline 401 recovery, and no local supervisor; stale cloud runners then consumed both Upstox sockets and blocked the local reconnect.**

---

## 2. Permanent three-layer auto-ignition

### Layer A — Embedded supervisor in `dashboard/server.py`

New module: `market/auto_ignition_supervisor.py`.

- Runs a daemon thread every **5 seconds** during **08:25–15:30 IST, Mon–Fri**.
- Checks token validity, `state/live_feed.json` / `state/live_fast.json` freshness, and the live-session process.
- Runs the non-interactive TOTP mint when today's token is missing/invalid.
- Starts one session-only child with `state/live_session.pid` as the single-instance lock.
- **Cold-start grace:** 75 seconds, because the session downloads 512 bars for NIFTY/BANKNIFTY spot + futures before its first feed write.
- **90-second stale threshold** prevents a warming session from being killed.
- **Does not pass `--edge`** from the supervisor. The dashboard already owns `:8080`; the edge is either the dashboard's standalone tunnel or the cloud runner. This removes the old session → dashboard → supervisor spawn loop.
- Endpoint: `GET /api/auto-ignition`.

Current live evidence:

```json
{
  "thread_alive": true,
  "window_open": true,
  "feed_age_sec": 1.09,
  "session_pid": 10628,
  "last_action": "healthy pid=10628 feed_age=2.09603214263916s",
  "spawn_count": 0,
  "last_error": ""
}
```

The session was later restarted through the supervisor; it is now PID `1620` with the new quorum/OBI code and a live socket.

### Layer B — Deterministic cloud chain

Private `.github/workflows/01-morning-auth.yml` now has an explicit final step:

```text
gh workflow run 02-live-market-edge.yml --repo vetukurisuresh/kronos-trading-brain --ref main -f shift=shift-1
```

The private `02-live-market-edge.yml` also has:

- 09:05 IST and 09:16 IST backup crons.
- A duplicate-shift guard using `gh run list`.
- Inline token verification; on failure it runs `scripts/auto_upstox_morning_login.py --require --push-secret` before streaming.
- Token mirroring to the public repo's Actions secret.

Because Upstox permits two sockets, the architecture is intentionally **one cloud relay + one local desk**. The public repository remains the Pages surface and a manual runner fallback. Its scheduled `01`/`02` triggers were removed so it cannot become the third concurrent socket.

### Layer C — OS auto-start

`scripts/install_daily_autostart.py` now supports:

- `--startup-only`: writes a no-admin login guardian to  
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\KRONOS_Autonomous_Desk.vbs`.
- The original Scheduled Task path (`KRONOS_Autonomous_Desk`, login + 08:25 Mon–Fri).

**Installed and verified:** the no-admin login Startup Guardian.  
**Blocked:** the elevated Scheduled Task. `Register-ScheduledTask` returned `Access is denied` (`0x80070005`), and the elevation request was declined by the user. The task is not claimed as installed; the installer is ready for a later approved elevated run.

---

## 3. Live market proof — two consecutive timestamps

Source: `state/live_feed.json` (live Upstox V3 socket) and `/api/readiness`.

| Field | Sample 1 — 11:05:04 IST | Sample 2 — 11:05:11 IST |
|---|---|---|
| Feed status | `FEED_LIVE` | `FEED_LIVE` |
| Socket | connected, 3,686 frames | connected, 3,745 frames |
| NIFTY 50 | 25,315.55 | 25,313.95 |
| BANKNIFTY | 61,288.35 | 61,284.15 |
| Active option | NIFTY 25350 CE | NIFTY 25350 CE |
| Option LTP | ₹189.65 | ₹188.80 |
| Option OBI_5 | 2.4035x | 0.6250x |
| TBQ/TSQ | 2.27x | 2.29x |
| Quorum votes | base 66.7 CE · small 58.0 neutral · mini 70.6 PE | base 66.7 CE · small 58.0 neutral · mini 48.0 neutral |
| Scalp P&L | ₹982.50 | ₹982.50 |
| Sniper P&L | ₹438.56 | ₹438.56 |
| Grand Total | ₹1,421.06 | ₹1,421.06 |
| Identity | 982.50 + 438.56 = 1,421.06 | 982.50 + 438.56 = 1,421.06 |
| Trade/ledger | no new paper entry at capture | no new paper entry at capture |

The live model strip rendered **67% / 58% / 48%**, never `—%`; the drawer showed the 102M card **CONFIRMED · CE 67%**, 25M **WATCHING · 58%**, 4M **WATCHING · 48%**.

### Readiness

Local `/api/readiness` = **5/6 GREEN**:

1. 08:30 AM Auto-Token — GREEN · verified 09:23:33 IST.
2. GitHub Cloud Runner — GREEN · heartbeat.
3. Cloudflare Direct Stream — **RED** · local session is feed-only; a standalone tunnel was started, but the dashboard readiness source still reads the session's `edge` object, not the standalone record.
4. Upstox V3 WebSocket & 11-Strike Chain — GREEN · 5.8 ticks/s · chain 11/11.
5. 512 Candles & Futures Volume — GREEN · NIFTY/BANKNIFTY 512×1m/5m/15m.
6. 3 KRONOS Models (102M/25M/4M) — GREEN · feeds ready.

A standalone `cloudflared` tunnel is live at:

`https://wayne-stating-quite-looksmart.trycloudflare.com`

It is not counted as a sixth green check because the readiness derivation does not overlay `state/live_stream_url.json` onto the session's edge block. The cloud relay publishes its own edge and is expected to produce the 6/6 record; this report does not claim 6/6 locally before that run.

---

## 4. UI defect verification (local `http://127.0.0.1:8080`)

Measured with a 1440×1000 viewport:

| Defect | Result |
|---|---|
| Chart space | `#panes` 878 px; each chart 806 px; ~88% of viewport height for the dual-chart area. |
| Pre-decision levels | `ENTRY / SL / T1 / T2` inputs are `display:none`; option level text is empty; status reads “⏳ WAITING FOR 2-OF-3 MODEL CONFIRMATION — LEVELS LOCK AT EXECUTION”. |
| `chart_levels` while idle | `levels_locked:false`, `levels_state:IDLE_STATIC`, no entry/sl/t1/t2; only p10/p50/p90, ATP and trigger zone. |
| Model freeze | Model strip 67% / 58% / 48%; no `—%`; drawer cards live. |
| Option OBI | Condition row: “CE OBI 1.02x (Buyers > Sellers)” when live; option `obi_5` nonzero (2.4035 / 0.625 in the proof samples). |
| P&L contradiction | Header ₹983 + ₹439 = ₹1,421; matching EOD identity 982.50 + 438.56 = 1,421.06. |
| Drawer | `#proEvidenceDrawer` hidden by default; one click shows the five cards + proof board; no levels generated. |
| Console | No page errors or console errors in the headless run. |

The old black overlays, top condition box, level input strip, bottom card wall and EOD strip are hidden/removed from the main viewport.

---

## 5. Tests and static checks

- `python -m pytest` targeted suite under fixture isolation: **28 passed**.
  - `tests/test_cloud_100way_selection.py`
  - `tests/test_live_session_helpers.py`
  - `tests/test_quorum_intra_candle_proof.py`
  - `tests/test_direct_runner_edge_sync.py`
- The three quorum/replay tests prefer `state/live_feed.json` when present; a direct run against today's live tape fails their **14:56 fixture expectations**. They pass 7/7 when the live file is masked and the versioned `tests/fixtures/live_feed_1456.json` is used. No assertions were weakened.
- `node --check` passed for `quorum_desk.js` and `pro_workspace.js`.
- YAML parse passed for both edited workflows.
- `python -m py_compile` passed for the changed Python modules.
- Fast-overlay p95 after the revision cache: below 1 ms in the targeted test (was 1.129 ms on Windows).

---

## 6. Deployment state

- Local dashboard: PID `6604`; live session: PID `1620`; supervisor alive; feed live.
- Old cloud runs **cancelled**:
  - private `43157596109` — cancelled.
  - public `43157603829` — cancelled.
- Private repo: new supervisor, quorum/OBI fallbacks, UI, workflow chain and public-schedule guard are staged for commit/push.
- Public repo: new Python/static bundle copied; scheduled `01`/`02` triggers removed; staged for commit/push.
- Public Pages and the single private cloud relay are restarted after the push; this report is written before that final run completes.

### Known blockers

1. **Scheduled Task not registered** — elevation denied. Login Startup Guardian is installed; the 08:25 task remains an explicit manual/admin step.
2. **Upstox two-socket cap** — one cloud relay only. Running private and public runners together, plus the local desk, produces 403 on the third socket.
3. **Local readiness edge** — standalone tunnel is live but not reflected in the session-derived edge check.

---

## 7. Telugu summary (సారాంశం)

- ఈ రోజు ఉదయం GitHub cron రాలేదు; 01 → 02 కి స్వయంగా dispatch లేదు. టోకెన్ మాత్రం HTTP 200తో చెల్లుబాటు.
- పాత రెండు cloud runs రెండు Upstox sockets పట్టుకున్నాయి; మూడో socketకి 403 వచ్చింది. ఆ రెండు రన్లను ఆపగానే local feed `FEED_LIVE` అయింది.
- ఇప్పుడు supervisor 5 సెకన్లకోసారి టోకెన్, feed, session ను చూసుకుంటుంది; 75 సెకన్ల cold-start grace ఉంది.
- మూడు models 67% / 58% / 48% చూపిస్తున్నాయి; `—%` లేదు. OBI nonzero; ENTRY/SL/T1/T2 scanning సమయంలో ఖాళీ.
- P&L: ₹982.50 + ₹438.56 = ₹1,421.06; వైరుధ్యం పోయింది.
- పరీక్షలు: fixture isolationతో 28/28 pass.
- Scheduled Taskకి admin permission నిరాకరించబడింది; login Startup Guardian మాత్రమే ఇన్‌స్టాల్ అయింది. Cloud runner ఒకటే — Upstox socket పరిమితి వల్ల.

---

**Evidence files:** `state/live_feed.json`, `state/live_stream_url.json`, `state/readiness_capture2.json`, `state/cloudflared_local.log`, `state/auto_ignition_supervisor.log`.
