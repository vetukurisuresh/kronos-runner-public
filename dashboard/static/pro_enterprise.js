/* KRONOS ENTERPRISE DESK ACTIONS
 * Light-only UI shell, deterministic trade pod, option target controls,
 * KRONOS forecast candles and the broker-style viewport lock.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
  const fmt = (v, d = 2) => (v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }));
  const money = (v) => (Number(v) < 0 ? '-' : '') + '₹' + Math.abs(Number(v) || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const compact = (v) => (Number.isFinite(Number(v)) ? Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—');
  const saved = (key, fallback) => {
    try { const x = localStorage.getItem(key); return x == null ? fallback : x; } catch (_) { return fallback; }
  };
  const save = (key, value) => { try { localStorage.setItem(key, String(value)); } catch (_) {} };

  /* The desk is intentionally light-only. The legacy theme button is hidden
     and any late dark-mode toggle is corrected here. */
  function enforceLight() {
    document.documentElement.dataset.theme = 'light';
    try { if (typeof setTheme === 'function') setTheme('light'); } catch (_) {}
  }
  enforceLight();
  setTimeout(enforceLight, 150);
  setInterval(() => {
    if (document.documentElement.dataset.theme !== 'light') enforceLight();
  }, 3000);

  /* ---------- sidebar: desktop collapse, mobile drawer ---------- */
  const navToggle = $('pro-nav-toggle') || $('menuBtn');
  const sidebar = $('proSidebar');
  const scrim = $('proSidebarScrim');

  function resizeSoon() {
    [40, 160, 280].forEach((ms) => setTimeout(() => {
      try { if (typeof resizeCharts === 'function') resizeCharts(); } catch (_) {}
      try { if (typeof drawOverlays === 'function') drawOverlays(); } catch (_) {}
      window.dispatchEvent(new Event('resize'));
    }, ms));
  }
  function setNavState() {
    if (!navToggle) return;
    const desktop = window.innerWidth > 980;
    const collapsed = desktop
      ? document.body.classList.contains('pro-sidebar-collapsed')
      : !(sidebar && sidebar.classList.contains('open'));
    navToggle.setAttribute('aria-expanded', String(!collapsed));
  }
  function toggleNav() {
    if (!sidebar) return;
    if (window.innerWidth <= 980) {
      const open = !sidebar.classList.contains('open');
      sidebar.classList.toggle('open', open);
      if (scrim) scrim.classList.toggle('show', open);
      navToggle && navToggle.setAttribute('aria-expanded', String(open));
      return;
    }
    const collapsed = document.body.classList.toggle('pro-sidebar-collapsed');
    sidebar.classList.remove('open');
    if (scrim) scrim.classList.remove('show');
    navToggle && navToggle.setAttribute('aria-expanded', String(!collapsed));
    resizeSoon();
  }
  if (navToggle) navToggle.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    toggleNav();
  });
  const closeNav = $('proSidebarClose');
  if (closeNav) closeNav.addEventListener('click', () => {
    if (window.innerWidth <= 980) {
      sidebar && sidebar.classList.remove('open');
      scrim && scrim.classList.remove('show');
      navToggle && navToggle.setAttribute('aria-expanded', 'false');
    } else {
      document.body.classList.add('pro-sidebar-collapsed');
      navToggle && navToggle.setAttribute('aria-expanded', 'false');
      resizeSoon();
    }
  });
  if (scrim) scrim.addEventListener('click', () => {
    sidebar && sidebar.classList.remove('open');
    scrim.classList.remove('show');
    navToggle && navToggle.setAttribute('aria-expanded', 'false');
  });
  setNavState();

  /* ---------- active trade pod ---------- */
  function tradeState() {
    let tr = null;
    try { tr = (typeof LIVE !== 'undefined' && LIVE.trade) || null; } catch (_) {}
    if (!tr) return { open: false, stage: 'IDLE', stageLabel: 'IDLE', qty: 0, pnl: 0, label: 'NO ACTIVE POSITION', sub: 'NO POSITION' };
    const open = String(tr.status || '').toUpperCase() === 'OPEN';
    if (!open) return { open: false, stage: 'IDLE', stageLabel: 'IDLE', qty: 0, pnl: 0, label: 'NO ACTIVE POSITION', sub: 'NO POSITION' };
    const t1 = !!tr.t1_booked;
    const t2 = !!tr.t2_booked;
    const stage = t2 ? 'T2' : t1 ? 'T1' : 'OPEN';
    const stageLabel = t2
      ? `🟣 T2 BOOKED · GEAR-${tr.tsl_gear || 5}`
      : t1 ? '🟢 T1 BOOKED · 50%' : '🔵 ENTRY ACTIVE';
    const qty = num(tr.qty_open) != null ? num(tr.qty_open) : (num(tr.qty) || 0);
    const pnl = num(tr.live_pnl_rs) || 0;
    const total = num(tr.qty) || qty || 0;
    const sub = total && qty && qty !== total
      ? `${qty}/${total} QTY RUNNING`
      : `${qty || total} QTY RUNNING`;
    return { open, stage, stageLabel, qty, pnl, label: open ? 'LIVE POSITION' : 'NO ACTIVE POSITION', sub };
  }

  function updateTradePod() {
    const st = tradeState();
    const pod = $('activeTradePod');
    const stage = $('tradeStage');
    const qty = $('tradeQty');
    const pnl = $('tradePnl');
    const sub = $('tradeSub');
    const close = $('topCloseBtn');
    if (pod) pod.dataset.stage = st.open ? st.stage : 'IDLE';
    if (stage) stage.textContent = st.stageLabel;
    if (qty) qty.textContent = st.open ? st.sub : 'NO POSITION';
    if (pnl) {
      pnl.textContent = st.open ? `${st.pnl >= 0 ? '+' : ''}${money(st.pnl)}` : '+₹0';
      pnl.classList.toggle('neg', st.open && st.pnl < 0);
    }
    if (sub) sub.textContent = st.open ? `${st.qty} ACTIVE · LIVE P&L` : 'WAITING FOR ENTRY';
    if (close) close.hidden = !st.open;
  }
  const topClose = $('topCloseBtn');
  if (topClose) topClose.addEventListener('click', () => {
    try {
      if (typeof closePosition === 'function') closePosition();
      else if (window.toast) window.toast('Close endpoint unavailable');
    } catch (err) { if (window.toast) window.toast('Close failed: ' + err.message); }
  });

  /* ---------- docked model + option target controls ---------- */
  const LEVEL_KEYS = ['entry', 'sl', 't1', 't2'];
  const LEVEL_ID = { entry: 'optLevelEntry', sl: 'optLevelSl', t1: 'optLevelT1', t2: 'optLevelT2' };
  const LEVEL_LIVE = { entry: 'entry', sl: 'sl', t1: 't1', t2: 't2' };
  let overrides = {};
  try { overrides = JSON.parse(saved('kronos.levels.v1', '{}')) || {}; } catch (_) { overrides = {}; }
  let labelSize = Number(saved('kronos.levels.font', 14));
  if (![12, 14, 16, 18].includes(labelSize)) labelSize = 14;

  function currentLevels() {
    let t = {};
    try { t = (typeof LIVE !== 'undefined' && LIVE.tick) || {}; } catch (_) {}
    const tr = (typeof LIVE !== 'undefined' && LIVE.trade) || {};
    const lv = (t && t.chart_levels) || {};
    return {
      entry: num(tr.entry_price) != null ? num(tr.entry_price) : num(lv.entry),
      sl: num(tr.current_trailing_sl) != null ? num(tr.current_trailing_sl) : num(lv.sl),
      t1: num(tr.target_1) != null ? num(tr.target_1) : num(lv.t1),
      t2: num(tr.target_2_p90) != null ? num(tr.target_2_p90) : num(lv.t2),
    };
  }
  function effectiveLevels() {
    const live = currentLevels();
    const out = {};
    LEVEL_KEYS.forEach((k) => {
      const ov = num(overrides[k]);
      out[k] = ov != null ? ov : live[k];
    });
    return out;
  }
  function publishLevels() {
    window.__kronosLevelOverrides = Object.assign({}, overrides);
    window.__kronosLabelSize = labelSize;
    try {
      if (typeof PL_SIG !== 'undefined') PL_SIG.v = '';
      if (typeof setOrderLines === 'function') setOrderLines((typeof LIVE !== 'undefined' && LIVE.opt) || null);
    } catch (_) {}
    drawOptionLevelLabels();
  }
  function syncLevelInputs() {
    const live = currentLevels();
    LEVEL_KEYS.forEach((k) => {
      const el = $(LEVEL_ID[k]);
      if (!el) return;
      const value = num(overrides[k]) != null ? num(overrides[k]) : live[k];
      if (document.activeElement !== el) el.value = value == null ? '' : String(value);
      el.dataset.live = live[k] == null ? '' : String(live[k]);
    });
    const qty = $('optQty');
    if (qty) {
      const tr = (typeof LIVE !== 'undefined' && LIVE.trade) || {};
      if (document.activeElement !== qty) qty.value = String(num(tr.qty_open) != null ? num(tr.qty_open) : (num(tr.qty) || ''));
    }
    const f = $('optFontSize');
    if (f) f.textContent = labelSize + 'px';
  }
  function setLevel(key, value) {
    const v = num(value);
    if (v == null) return;
    overrides[key] = v;
    save('kronos.levels.v1', JSON.stringify(overrides));
    publishLevels();
  }
  function resetLevels() {
    overrides = {};
    save('kronos.levels.v1', '{}');
    publishLevels();
    syncLevelInputs();
    if (window.toast) window.toast('Target lines reset to canonical live levels');
  }
  function stepLevel(key, delta) {
    const cur = effectiveLevels()[key];
    if (cur == null) return;
    setLevel(key, Math.max(0, Math.round((cur + delta) * 100) / 100));
    syncLevelInputs();
  }

  document.querySelectorAll('[data-level-step]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.levelStep;
      const dir = Number(btn.dataset.dir || (btn.textContent.trim() === '+' ? 1 : -1));
      stepLevel(key, dir * (key === 'sl' ? 0.05 : 0.05));
    });
  });
  LEVEL_KEYS.forEach((key) => {
    const el = $(LEVEL_ID[key]);
    if (!el) return;
    el.addEventListener('change', () => { setLevel(key, el.value); syncLevelInputs(); });
    el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { setLevel(key, el.value); el.blur(); } });
  });
  const optQty = $('optQty');
  if (optQty) optQty.addEventListener('change', () => {
    const v = Math.max(0, Math.floor(Number(optQty.value) || 0));
    window.__kronosPlanQty = v;
    optQty.value = String(v);
    if (window.toast) window.toast(`Planned size ${v} Qty · live close uses the broker position`);
  });
  const fontMinus = $('optFontMinus');
  const fontPlus = $('optFontPlus');
  if (fontMinus) fontMinus.addEventListener('click', () => { labelSize = Math.max(12, labelSize - 2); save('kronos.levels.font', labelSize); publishLevels(); syncLevelInputs(); });
  if (fontPlus) fontPlus.addEventListener('click', () => { labelSize = Math.min(18, labelSize + 2); save('kronos.levels.font', labelSize); publishLevels(); syncLevelInputs(); });
  const reset = $('optLevelReset');
  if (reset) reset.addEventListener('click', resetLevels);

  publishLevels();

  /* ---------- AI scanner drawer ---------- */
  const aiBtn = $('aiDrawerBtn');
  if (aiBtn) aiBtn.addEventListener('click', () => {
    try {
      if (window.kronosPro && window.kronosPro.open) window.kronosPro.open('ai');
      else if (window.toast) window.toast('AI Scanner is loading');
    } catch (err) { if (window.toast) window.toast('AI Scanner unavailable'); }
  });

  /* ---------- 12 future KRONOS candles + range box ---------- */
  const FORECAST = { sig: '', series: null };
  function activeBlock() {
    try {
      const state = (typeof STATE !== 'undefined') ? STATE : (window.__kronos && window.__kronos.STATE ? window.__kronos.STATE() : null);
      const sym = (typeof SYMBOL !== 'undefined') ? SYMBOL : 'NIFTY';
      return state && state.symbols ? state.symbols[sym] : null;
    } catch (_) { return null; }
  }
  function forecastRows() {
    const block = activeBlock();
    const f = (block && block.forecast) || {};
    const rows = Array.isArray(f.median_candles) ? f.median_candles.slice(0, 12) : [];
    if (!rows.length) return [];
    const k = window.__kronos;
    const data = k && k.CHARTS && k.CHARTS.spot && k.CHARTS.spot.__data;
    const last = data && data.length ? data[data.length - 1] : null;
    if (!last) return [];
    const tf = (typeof TF !== 'undefined' ? TF : '5m');
    const step = ((typeof TF_MINUTES !== 'undefined' && TF_MINUTES[tf]) || 5) * 60;
    return rows.map((row, i) => {
      const c = num(row.c != null ? row.c : row.close);
      if (c == null || !last.time) return null;
      const o = num(row.o != null ? row.o : row.open);
      const h = num(row.h != null ? row.h : row.high);
      const l = num(row.l != null ? row.l : row.low);
      const open = o == null ? c : o;
      return {
        time: Number(last.time) + step * (i + 1),
        open,
        high: h == null ? Math.max(open, c) : h,
        low: l == null ? Math.min(open, c) : l,
        close: c,
      };
    }).filter(Boolean);
  }
  function ensureForecast() {
    const k = window.__kronos;
    if (!k || !k.CHARTS || !k.CHARTS.spot) return null;
    if (!k.SERIES.spot.forecast) {
      k.SERIES.spot.forecast = k.CHARTS.spot.addCandlestickSeries({
        upColor: 'rgba(255,255,255,0)',
        downColor: 'rgba(255,255,255,0)',
        borderVisible: true,
        borderUpColor: '#059669',
        borderDownColor: '#e11d48',
        wickUpColor: '#059669',
        wickDownColor: '#e11d48',
        priceLineVisible: false,
        lastValueVisible: false,
      });
    }
    return k.SERIES.spot.forecast;
  }
  function updateForecast() {
    const series = ensureForecast();
    if (!series) return;
    const rows = forecastRows();
    const sig = JSON.stringify(rows);
    if (sig !== FORECAST.sig) {
      FORECAST.sig = sig;
      try { series.setData(rows); } catch (_) {}
    }
    const badge = $('proAiCallout');
    if (badge) {
      const f = (activeBlock() || {}).forecast || {};
      if (badge.dataset.direction === '') {
        // leave the canonical model text from pro_workspace.updateHeader
      }
    }
    drawForecastRange();
  }
  function rangeContext() {
    const cv = $('forecastRange');
    if (!cv) return null;
    const w = cv.clientWidth || (cv.parentElement && cv.parentElement.clientWidth) || 0;
    const h = cv.clientHeight || (cv.parentElement && cv.parentElement.clientHeight) || 0;
    if (w < 30 || h < 30) return null;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.floor(w * dpr) || cv.height !== Math.floor(h * dpr)) {
      cv.width = Math.floor(w * dpr); cv.height = Math.floor(h * dpr);
      cv.style.width = w + 'px'; cv.style.height = h + 'px';
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }
  function drawForecastRange() {
    const layer = rangeContext();
    const k = window.__kronos;
    if (!layer || !k || !k.CHARTS.spot || !k.SERIES.spot.candle) return;
    const block = activeBlock() || {};
    const f = block.forecast || {};
    const p10 = num(f.p10), p90 = num(f.p90);
    const rows = forecastRows();
    if (p10 == null || p90 == null || !rows.length) return;
    const { ctx, w, h } = layer;
    const chart = k.CHARTS.spot;
    const y10 = k.SERIES.spot.candle.priceToCoordinate(p10);
    const y90 = k.SERIES.spot.candle.priceToCoordinate(p90);
    const x1 = chart.timeScale().timeToCoordinate(rows[0].time);
    const x2 = chart.timeScale().timeToCoordinate(rows[rows.length - 1].time);
    if ([y10, y90].some((v) => v == null)) return;
    const left = x1 == null ? Math.max(8, w - 150) : Math.max(0, x1 - 8);
    const right = x2 == null ? w - 6 : Math.min(w, x2 + 10);
    if (right <= left + 4) return;
    const top = Math.min(y10, y90), bottom = Math.max(y10, y90);
    const bull = Number(f.paths_up || 0) >= Number(f.samples || 0) * 0.5;
    const edge = bull ? '#059669' : '#e11d48';
    ctx.save();
    ctx.fillStyle = bull ? 'rgba(5,150,105,.055)' : 'rgba(225,29,72,.05)';
    ctx.fillRect(left, top, right - left, Math.max(2, bottom - top));
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = edge;
    ctx.lineWidth = 1;
    [top, bottom].forEach((y) => {
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    });
    ctx.setLineDash([]);
    const tag = (text, y, align) => {
      ctx.font = "700 10px 'JetBrains Mono', monospace";
      const tw = ctx.measureText(text).width + 10;
      const tx = w - tw - 4;
      const ty = Math.max(2, Math.min(h - 18, y - 9));
      ctx.fillStyle = 'rgba(255,255,255,.96)';
      ctx.strokeStyle = edge;
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(tx, ty, tw, 16, 4); else ctx.rect(tx, ty, tw, 16);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = edge;
      ctx.textAlign = 'right';
      ctx.fillText(text, w - 9, ty + 11);
      ctx.textAlign = 'left';
    };
    tag(`p90 ${compact(p90)}`, top, 'right');
    tag(`p10 ${compact(p10)}`, bottom, 'right');
    ctx.fillStyle = edge;
    ctx.font = "700 9px 'JetBrains Mono', monospace";
    ctx.fillText('KRONOS 12-CANDLE RANGE', left + 5, Math.max(12, top - 6));
    ctx.restore();
  }

  /* Clear right-side level labels; the lines themselves are Lightweight Charts
     price lines, this overlay only makes the values legible at the chosen size. */
  function levelContext() {
    const cv = $('optLevelLabels');
    if (!cv) return null;
    const w = cv.clientWidth || (cv.parentElement && cv.parentElement.clientWidth) || 0;
    const h = cv.clientHeight || (cv.parentElement && cv.parentElement.clientHeight) || 0;
    if (w < 30 || h < 30) return null;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.floor(w * dpr) || cv.height !== Math.floor(h * dpr)) {
      cv.width = Math.floor(w * dpr); cv.height = Math.floor(h * dpr);
      cv.style.width = w + 'px'; cv.style.height = h + 'px';
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }
  function drawOptionLevelLabels() {
    const layer = levelContext();
    const k = window.__kronos;
    if (!layer || !k || !k.SERIES.opt.candle) return;
    const values = effectiveLevels();
    const colors = { entry: '#0369a1', sl: '#dc2626', t1: '#059669', t2: '#7c3aed' };
    const labels = { entry: 'ENTRY', sl: 'SL', t1: 'T1', t2: 'T2' };
    const { ctx, w, h } = layer;
    const series = k.SERIES.opt.candle;
    Object.keys(values).forEach((key) => {
      const v = values[key];
      if (v == null) return;
      let y = null;
      try { y = series.priceToCoordinate(v); } catch (_) { y = null; }
      if (y == null || y < -10 || y > h + 10) return;
      const text = `${labels[key]} ${fmt(v, 2)}`;
      ctx.font = `800 ${labelSize}px 'JetBrains Mono', monospace`;
      const tw = ctx.measureText(text).width + 12;
      const tx = w - tw - 8;
      const ty = Math.max(1, Math.min(h - (labelSize + 8), y - (labelSize / 2) - 4));
      ctx.fillStyle = 'rgba(255,255,255,.96)';
      ctx.strokeStyle = colors[key];
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(tx, ty, tw, labelSize + 8, 4); else ctx.rect(tx, ty, tw, labelSize + 8);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = colors[key];
      ctx.textAlign = 'right';
      ctx.fillText(text, w - 14, ty + labelSize + 1);
      ctx.textAlign = 'left';
    });
  }

  /* Stage copy is derived only from the canonical trade payload. */
  function updateStageCopy() {
    const st = tradeState();
    const title = $('levelStageTitle');
    const sub = $('levelStageSub');
    if (title) title.textContent = st.open ? (st.stage === 'T2' ? 'Stage 3 · Runner' : st.stage === 'T1' ? 'Stage 2 · T1 Booked' : 'Stage 1 · Entry Placed') : 'Stage 1 · Waiting';
    if (sub) sub.textContent = st.open ? (st.stage === 'T2' ? '20% booked · 30% momentum runner' : st.stage === 'T1' ? '50% booked · SL at cost' : 'Live position · targets armed') : 'No position · bracket ready';
  }

  function refresh() {
    updateTradePod();
    const big = $('bigSpot'), dock = $('dockSpot');
    if (big && dock) dock.textContent = big.textContent;
    const bigDelta = $('bigSpotDelta'), dockDelta = $('dockSpotDelta');
    if (bigDelta && dockDelta) {
      dockDelta.textContent = bigDelta.textContent;
      dockDelta.className = /good|up/.test(bigDelta.className || '') ? '' : 'down';
    }
    syncLevelInputs();
    drawOptionLevelLabels();
    updateForecast();
    updateStageCopy();
  }

  const previous = window.kronosProTick;
  window.kronosProTick = function (t) {
    if (typeof previous === 'function') {
      try { previous(t); } catch (err) { console.warn('KRONOS tick paint failed', err); }
    }
    refresh();
  };

  window.addEventListener('resize', () => setTimeout(refresh, 120));
  setTimeout(refresh, 400);
  setTimeout(refresh, 1400);
  setInterval(refresh, 700);

  window.kronosEnterprise = {
    toggleNav,
    refresh,
    levels: effectiveLevels,
    resetLevels,
  };
})();
