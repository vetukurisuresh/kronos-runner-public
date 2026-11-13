/* KRONOS AI COMMAND CENTER
 * One window, five independently zoomable panels, date-wise archive and
 * downloadable Telugu logs. Values are rendered only from the API.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
  const num = (value, digits = 0) => {
    if (value == null || value === '') return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return n.toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits });
  };
  const pct = (value) => (value != null && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(1) + '%' : '—');
  const age = (ms) => {
    if (ms == null || !Number.isFinite(Number(ms))) return '—';
    const n = Math.max(0, Number(ms));
    if (n < 1000) return Math.round(n) + 'ms';
    if (n < 60000) return (n / 1000).toFixed(1) + 's';
    return Math.round(n / 60000) + 'm';
  };
  const todayIST = () => {
    const shifted = new Date(Date.now() + 5.5 * 3600 * 1000);
    return shifted.toISOString().slice(0, 10);
  };
  const api = (path) => String(path);

  const state = {
    open: false,
    date: todayIST(),
    payload: null,
    timer: 0,
    request: 0,
    paused: { scalper: false, intraday: false, model: false },
    auto: { scalper: true, intraday: true, model: true },
    filters: { scalper: 'all', intraday: 'all' },
    frozen: { scalper: [], intraday: [], model: [] },
    zoomed: null,
  };

  function setText(id, value) {
    const el = $(id);
    if (el) el.textContent = value == null ? '—' : String(value);
  }
  function setHTML(id, value) {
    const el = $(id);
    if (el) el.innerHTML = value;
  }
  function toast(message) {
    let node = document.querySelector('.cc-toast');
    if (!node) {
      node = document.createElement('div');
      node.className = 'cc-toast';
      document.body.appendChild(node);
    }
    node.textContent = message;
    clearTimeout(node._timer);
    node._timer = setTimeout(() => node.remove(), 3200);
  }

  function setOpen(open) {
    const win = $('commandCenterWindow');
    if (!win) return;
    state.open = !!open;
    win.hidden = !state.open;
    win.setAttribute('aria-hidden', String(!state.open));
    document.body.classList.toggle('cc-open', state.open);
    const top = $('ccTopOpen');
    const side = $('ccSideOpen');
    if (top) top.setAttribute('aria-expanded', String(state.open));
    if (side) side.setAttribute('aria-expanded', String(state.open));
    if (state.open) {
      const date = $('ccDate');
      if (date) {
        date.max = todayIST();
        date.value = state.date;
      }
      load();
      schedule();
    } else {
      clearTimeout(state.timer);
      state.timer = 0;
      closeZoom();
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    state.timer = 0;
    if (!state.open || state.date !== todayIST()) return;
    state.timer = setTimeout(() => { load(); schedule(); }, 4000);
  }

  async function load() {
    if (!state.open) return;
    const request = ++state.request;
    const date = $('ccDate')?.value || state.date;
    state.date = date;
    const live = date === todayIST();
    const path = live
      ? '/api/command-center/live?symbol=NIFTY'
      : '/api/command-center/history?date=' + encodeURIComponent(date);
    try {
      const response = await fetch(api(path), { cache: 'no-store' });
      const body = await response.json();
      if (request !== state.request) return;
      if (!response.ok || body.ok === false) throw new Error(body.reason || 'command center unavailable');
      state.payload = body;
      render(body, live);
      schedule();
    } catch (error) {
      if (request !== state.request) return;
      renderUnavailable(error.message || String(error), live);
    }
  }

  function renderUnavailable(message, live) {
    state.payload = null;
    setHTML('ccGateBanner', `<b>${live ? 'OFFLINE_OR_STALE_FEED' : 'NO_ARCHIVE_FOR_DATE'}</b><span>${esc(message)}</span>`);
    const banner = $('ccGateBanner');
    if (banner) banner.className = 'cc-gate-banner is-blocked';
    setText('ccLiveDot', '● UNAVAILABLE');
    const dot = $('ccLiveDot');
    if (dot) dot.classList.remove('is-live');
    setText('ccAsOf', 'NO VERIFIED RESPONSE');
    setHTML('ccAccuracyBreakdown', '<div><span>Status</span><b>WAITING</b></div>');
    setHTML('ccStreamProof', '');
    setHTML('ccScalperLog', '<div class="cc-empty">No verified Scalper log is available. Zero dummy entries are shown.<br><span lang="te">ధృవీకరించిన స్కాల్పర్ లాగ్ లేదు; నకిలీ ఎంట్రీలు చూపబడవు.</span></div>');
    setHTML('ccIntradayLog', '<div class="cc-empty">No verified Intraday Sniper log is available.<br><span lang="te">ధృవీకరించిన ఇంట్రాడే లాగ్ లేదు.</span></div>');
    setHTML('ccModelCards', '');
    setHTML('ccModelLog', '<div class="cc-empty">No model-consumption proof for this request.</div>');
    setHTML('ccWhaleWindows', '');
    setHTML('ccWhaleSummary', '<div><span>Market speed</span><b>—</b></div><div><span>Whale zones</span><b>—</b></div><div><span>Verified prints</span><b>0</b></div>');
  }

  function streamRows(sync) {
    const streams = Array.isArray(sync.streams) ? sync.streams : [];
    const byKey = {};
    streams.forEach((row) => { byKey[row.key] = row; });
    const labels = {
      spot: 'SPOT', futures: 'FUTURES', option_chain: 'OPTION CHAIN', depth: '5-LEVEL DEPTH',
      greeks: 'GREEKS', vix: 'INDIA VIX', breadth: 'BREADTH', candles: '512 CANDLES',
    };
    return Object.keys(labels).map((key) => {
      const row = byKey[key] || { present: false, verified: false, reads: 0, last_tick_age_ms: null };
      return `<div class="cc-stream ${row.verified ? 'is-live' : ''}"><div class="cc-stream-top"><i></i>${labels[key]}</div><small>${row.verified ? 'VERIFIED' : 'NO DATA'} · ${num(row.reads)} reads · ${age(row.last_tick_age_ms)}</small></div>`;
    }).join('');
  }

  function renderAccuracy(payload) {
    const accuracy = payload.accuracy || {};
    setText('ccDecisionAccuracy', pct(accuracy.decision_accuracy_pct));
    setText('ccTradeWinRate', pct(accuracy.trade_win_rate_pct));
    setText('ccAvgLead', accuracy.avg_lead_time_sec == null ? '—' : `${Number(accuracy.avg_lead_time_sec) >= 0 ? '+' : ''}${Number(accuracy.avg_lead_time_sec).toFixed(1)}s`);
    setText('ccBlockedTraps', accuracy.correctly_blocked_traps == null || !Number(accuracy.total_decisions) ? '—' : num(accuracy.correctly_blocked_traps));
    setText('ccDecisionMeta', `${num(accuracy.scored_decisions)} scored · ${num(accuracy.total_decisions)} total`);
    setText('ccWinMeta', `${num(accuracy.winning_trades)} wins · ${num(accuracy.losing_trades)} losses`);
    setHTML('ccAccuracyBreakdown', [
      ['Total decisions', num(accuracy.total_decisions)],
      ['Winning trades', num(accuracy.winning_trades)],
      ['Profitable early exits', num(accuracy.profitable_early_exits)],
      ['Correctly blocked traps', num(accuracy.correctly_blocked_traps)],
      ['Open / unscored', num(accuracy.open_decisions)],
      ['Lead samples', num(accuracy.lead_samples)],
    ].map(([key, value]) => `<div><span>${esc(key)}</span><b>${esc(value)}</b></div>`).join(''));

    const decisions = Array.isArray(accuracy.decisions) ? accuracy.decisions : [];
    const withReason = decisions.filter((row) => row && row.human_failure_reason_te);
    if (withReason.length) {
      const last = withReason[withReason.length - 1];
      setHTML('ccLeadExplain', `${esc(last.human_failure_reason_te)}<div class="cc-kv-list" style="margin-top:8px"><div><span>Signal to human lead</span><b>${esc(last.lead_time_seconds_ahead == null ? '—' : Number(last.lead_time_seconds_ahead).toFixed(1) + 's')}</b></div><div><span>Price advantage</span><b>${last.price_advantage_inr == null ? '—' : '₹' + num(last.price_advantage_inr, 2)}</b></div></div>`);
    } else if (accuracy.avg_lead_time_sec != null) {
      setHTML('ccLeadExplain', `Average verified lead is <b>${Number(accuracy.avg_lead_time_sec).toFixed(1)} seconds</b> ahead of the human candle-close entry. Exact rupee advantage appears with each closed decision.`);
    } else {
      setHTML('ccLeadExplain', 'No actionable signal has been scored yet. The engine will show the exact seconds and rupees only after a verified decision.');
    }
    const sync = payload.sync || {};
    setHTML('ccStreamProof', streamRows(sync));
    setText('ccFrameId', sync.source_frames == null ? '—' : num(sync.source_frames));
    setText('ccRead102', num(sync.frames_read_by_102m));
    setText('ccRead25', num(sync.frames_read_by_25m));
    setText('ccRead4', num(sync.frames_read_by_4m));
    const pass = sync.single_pass || {};
    setText('ccPassBudget', pass.measured_ms == null || !pass.measured_ms
      ? 'Single-pass budget: waiting for the first verified canonical frame.'
      : `Single-pass shared read: ${Number(pass.measured_ms).toFixed(3)} ms / ${Number(pass.budget_ms || 0.5).toFixed(1)} ms budget · ${pass.under_budget ? 'WITHIN BUDGET' : 'OVER BUDGET'}`);
  }

  function logsFor(payload, stream) {
    if (state.paused[stream] && state.frozen[stream].length) return state.frozen[stream];
    const rows = (payload.logs && Array.isArray(payload.logs[stream])) ? payload.logs[stream] : [];
    if (!state.paused[stream]) state.frozen[stream] = rows;
    return rows;
  }

  function filterLogs(rows, stream) {
    const filter = state.filters[stream] || 'all';
    if (filter === 'all') return rows;
    return rows.filter((row) => String(row.kind || '').toLowerCase() === filter);
  }

  function logHTML(rows, stream) {
    if (!rows.length) {
      const label = stream === 'scalper' ? 'Scalper' : stream === 'intraday' ? 'Intraday Sniper' : 'model';
      return `<div class="cc-empty">No verified ${label} log for this date.<br><span lang="te">ధృవీకరించిన లాగ్ లేదు; నకిలీ ఎంట్రీలు చూపబడవు.</span></div>`;
    }
    return rows.slice(-160).map((row) => {
      const kind = String(row.kind || 'scan').toLowerCase();
      const cls = kind === 'entry' ? 'entry' : kind === 'exit' ? 'exit' : '';
      return `<div class="cc-log-row"><span class="cc-time">${esc(row.time_ist || row.ts || '')}</span><span class="cc-kind ${cls}">${esc(kind)}</span><span class="cc-log-text" lang="te">${esc(row.text || '')}</span></div>`;
    }).join('');
  }

  function renderLogs(payload) {
    const scalper = filterLogs(logsFor(payload, 'scalper'), 'scalper');
    const intraday = filterLogs(logsFor(payload, 'intraday'), 'intraday');
    const model = logsFor(payload, 'model');
    setHTML('ccScalperLog', logHTML(scalper, 'scalper'));
    setHTML('ccIntradayLog', logHTML(intraday, 'intraday'));
    setHTML('ccModelLog', logHTML(model, 'model'));
    if (state.auto.scalper && !state.paused.scalper) {
      const node = $('ccScalperLog'); if (node) node.scrollTop = node.scrollHeight;
    }
    if (state.auto.intraday && !state.paused.intraday) {
      const node = $('ccIntradayLog'); if (node) node.scrollTop = node.scrollHeight;
    }
    if (state.auto.model && !state.paused.model) {
      const node = $('ccModelLog'); if (node) node.scrollTop = node.scrollHeight;
    }
  }

  function renderModels(payload) {
    const cards = Array.isArray(payload.models) ? payload.models : [];
    if (!cards.length) {
      const names = [['102M', 'Kronos-base (102M)'], ['25M', 'Kronos-small (25M)'], ['4M', 'Kronos-mini (4M)']];
      setHTML('ccModelCards', names.map(([key, label]) => `<div class="cc-model-card"><span class="cc-kicker">${key} MODEL</span><h3>${label}</h3><p>No verified thought published for this frame.</p><span class="cc-pill">WAITING</span></div>`).join(''));
      return;
    }
    setHTML('ccModelCards', cards.slice(0, 3).map((card) => {
      const stateLabel = String(card.decision_state || (card.gate_passed ? 'GATE PASSED' : 'WAITING'));
      return `<div class="cc-model-card"><span class="cc-kicker">${esc(card.params || card.role || 'KRONOS')}</span><h3>${esc(card.name || card.role || 'KRONOS')}</h3><p lang="te">${esc(card.thinking_te || card.reading_te || 'No verified thought published.')}</p><span class="cc-pill">${esc(stateLabel)}</span></div>`;
    }).join(''));
  }

  function renderWhales(payload) {
    const windows = Array.isArray(payload.whale_windows) ? payload.whale_windows : [];
    if (!windows.length) {
      setHTML('ccWhaleWindows', '<div class="cc-empty">No 30-minute windows for this date.</div>');
      setHTML('ccWhaleSummary', '<div><span>Market speed</span><b>—</b></div><div><span>Whale zones</span><b>—</b></div><div><span>Verified prints</span><b>0</b></div>');
      return;
    }
    const live = windows.filter((row) => row.status === 'LIVE').length;
    const zones = windows.filter((row) => row.whale_zone);
    const strongest = zones.slice().sort((a, b) => Number((b.whale_zone || {}).weighted_volume || 0) - Number((a.whale_zone || {}).weighted_volume || 0))[0];
    const totalPrints = windows.reduce((sum, row) => sum + Number((row.metrics || {}).tick_count || 0), 0);
    setHTML('ccWhaleSummary', [
      ['Windows with verified flow', `${zones.length}/13`],
      ['Live windows', num(live)],
      ['Verified prints', num(totalPrints)],
    ].map(([label, value]) => `<div><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join('') + (strongest ? `<div style="grid-column:1/-1"><span>Strongest institutional footprint</span><b>${esc((strongest.whale_zone || {}).bucket_label || '—')}</b></div>` : ''));
    setHTML('ccWhaleWindows', windows.map((row) => {
      const metrics = row.metrics || {};
      const whale = row.whale_zone;
      const empty = !metrics.tick_count && metrics.last_price == null;
      return `<div class="cc-window-card ${row.status === 'LIVE' ? 'is-live' : ''} ${empty ? 'is-empty' : ''}"><h3>${esc(row.window || '')}<span>${esc(row.status || '')}</span></h3><div class="cc-window-metrics"><span>Ticks/s</span><b>${num(metrics.avg_ticks_per_sec, 2)}</b><span>Range</span><b>${metrics.spot_range_pts == null ? '—' : num(metrics.spot_range_pts, 1) + ' pts'}</b><span>Option ATR</span><b>${metrics.option_atr == null ? '—' : num(metrics.option_atr, 2)}</b><span>VIX Δ</span><b>${metrics.vix_delta == null ? '—' : num(metrics.vix_delta, 2)}</b></div><div class="cc-whale-zone">${whale ? `<span lang="te">${esc(whale.text_te || whale.text || '')}</span><br><span class="cc-bias">${esc(whale.bias || '')}</span>` : `<span>${empty ? 'Waiting for verified V3 flow.' : esc(metrics.regime || 'AWAITING_VERIFIED_FLOW')}</span>`}</div></div>`;
    }).join(''));
  }

  function render(payload, live) {
    const sync = payload.sync || {};
    const gate = sync.data_gate || payload.data_gate || {};
    const verified = !!gate.allowed;
    const banner = $('ccGateBanner');
    if (banner) {
      banner.className = 'cc-gate-banner ' + (verified ? 'is-live' : 'is-blocked');
      banner.innerHTML = `<b>${esc(sync.live_status || gate.live_status || (verified ? 'LIVE_MARKET_VERIFIED' : 'OFFLINE_OR_STALE_FEED'))}</b><span>${esc(gate.reason || 'Waiting for 8 verified streams and three model reads.')}</span>`;
    }
    setText('ccAsOf', payload.as_of || (payload.date ? `${payload.date} · ARCHIVE` : 'WAITING'));
    setText('ccLiveDot', verified ? '● LIVE V3 · VERIFIED' : '● OFFLINE / STALE');
    const dot = $('ccLiveDot');
    if (dot) dot.classList.toggle('is-live', verified);
    setText('ccArchiveState', payload.archive ? `${payload.archive.records || 0} RECORDS · ${(payload.source || '').toUpperCase()}` : (payload.source || 'NO ARCHIVE'));
    renderAccuracy(payload);
    renderLogs(payload);
    renderModels(payload);
    renderWhales(payload);
    if (!live && payload.source === 'no_archive_for_date') {
      toast('No verified archive for this date; empty state shown.');
    }
  }

  async function download() {
    const date = state.date || todayIST();
    const scope = $('ccDownloadScope')?.value || 'all';
    const format = $('ccDownloadFormat')?.value || 'txt';
    const button = $('ccDownload');
    if (button) { button.disabled = true; button.textContent = 'Preparing…'; }
    try {
      const response = await fetch(api(`/api/command-center/download-logs?date=${encodeURIComponent(date)}&scope=${scope}&format=${format}`), { cache: 'no-store' });
      if (!response.ok) {
        let reason = 'download failed';
        try { const payload = await response.json(); reason = payload.reason || reason; } catch (_) { /* ignore */ }
        throw new Error(reason);
      }
      const blob = await response.blob();
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `kronos_command_center_${date}_${scope}.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1500);
    } catch (error) {
      toast('Download unavailable: ' + error.message);
    } finally {
      if (button) { button.disabled = false; button.textContent = '⬇ Download logs'; }
    }
  }

  function closeZoom() {
    document.querySelectorAll('.cc-panel.cc-zoomed').forEach((panel) => panel.classList.remove('cc-zoomed'));
    state.zoomed = null;
  }

  function zoom(panel) {
    if (!panel) return;
    const isZoomed = panel.classList.contains('cc-zoomed');
    closeZoom();
    if (!isZoomed) {
      panel.classList.add('cc-zoomed');
      state.zoomed = panel;
    }
  }

  function bind() {
    ['ccTopOpen', 'ccSideOpen'].forEach((id) => $(id)?.addEventListener('click', () => setOpen(true)));
    document.querySelectorAll('[data-cc-close]').forEach((node) => node.addEventListener('click', () => setOpen(false)));
    $('ccRefresh')?.addEventListener('click', load);
    $('ccDate')?.addEventListener('change', () => {
      state.date = $('ccDate').value || todayIST();
      state.payload = null;
      load();
    });
    $('ccDownload')?.addEventListener('click', download);
    document.querySelectorAll('[data-cc-zoom]').forEach((button) => button.addEventListener('click', (event) => {
      event.stopPropagation();
      zoom(button.closest('.cc-panel'));
    }));
    document.querySelectorAll('[data-cc-font]').forEach((button) => button.addEventListener('click', (event) => {
      event.stopPropagation();
      const panel = button.closest('.cc-panel');
      if (!panel) return;
      const current = Number(panel.style.getPropertyValue('--cc-scale') || 1);
      const next = Math.max(0.9, Math.min(1.5, current + (button.dataset.ccFont === '+' ? 0.08 : -0.08)));
      panel.style.setProperty('--cc-scale', next.toFixed(2));
    }));
    document.querySelectorAll('[data-cc-filter]').forEach((select) => select.addEventListener('change', () => {
      const stream = select.dataset.ccFilter;
      state.filters[stream] = select.value;
      state.paused[stream] = false;
      renderLogs(state.payload || {});
    }));
    document.querySelectorAll('[data-cc-pause]').forEach((checkbox) => checkbox.addEventListener('change', () => {
      const stream = checkbox.dataset.ccPause;
      state.paused[stream] = checkbox.checked;
      if (checkbox.checked && state.payload) state.frozen[stream] = logsFor(state.payload, stream);
      if (!checkbox.checked && state.payload) renderLogs(state.payload);
    }));
    document.querySelectorAll('[data-cc-auto]').forEach((checkbox) => checkbox.addEventListener('change', () => {
      state.auto[checkbox.dataset.ccAuto] = checkbox.checked;
    }));
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !state.open) return;
      if (document.querySelector('.cc-panel.cc-zoomed')) closeZoom();
      else setOpen(false);
    });
  }

  window.kronosCommandCenter = { open: () => setOpen(true), close: () => setOpen(false), refresh: load };
  bind();
})();
