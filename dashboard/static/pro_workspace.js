/* KRONOS PRO WORKSPACE — Upstox-inspired shell.
 * High-frequency ticks are coalesced into one requestAnimationFrame paint.
 * Feature panels live in modals and are only repainted while their modal is open.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const e = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const n = (v, d = 0) => {
    const x = Number(v);
    return Number.isFinite(x) ? x.toLocaleString('en-IN', { maximumFractionDigits: d }) : '—';
  };
  const money = (v) => (Number(v) < 0 ? '-' : '') + '₹' + n(Math.abs(Number(v) || 0), 0);
  const pct = (v) => Number.isFinite(Number(v)) ? Number(v).toFixed(1) + '%' : '—';
  const symbol = () => {
    try { return String(SYMBOL || 'NIFTY'); } catch (_) { return 'NIFTY'; }
  };
  /* Direct GitHub runner edge: all /api routes can be globally prefixed. */
  const edgeBase = () => {
    try { return String(window.EDGE_BASE_URL || '').replace(/\/+$/, ''); }
    catch (_) { return ''; }
  };
  const edgeApi = (path) => {
    const route = String(path || '');
    if (route.indexOf('/api/') !== 0) return route;
    const base = edgeBase();
    return base ? base + route : route;
  };
  const stash = $('proNodeStash');
  const modal = $('proModal');
  const body = $('proModalBody');
  const isOpen = (key) => !!active && (!key || active === key);
  let active = null;
  let mounted = [];
  let raf = 0;
  let paintTimer = 0;
  let pending = null;

  /* ---- Chart Studio: persisted, per-user visual control ------------------ */
  const STYLE_KEY = 'kronos.chartStyle.v1';
  const STYLE_DEFAULTS = {
    bull: '#10B981', bear: '#F43F5E', bullRev: '#00E5FF', bearRev: '#D946EF',
    hollow: false, reversal: true, corridor: true, volume: true, autoFollow: false,
  };
  let STYLE = Object.assign({}, STYLE_DEFAULTS);
  let styleApplied = false;
  try {
    const saved = JSON.parse(localStorage.getItem(STYLE_KEY) || '{}');
    Object.keys(STYLE_DEFAULTS).forEach((k) => {
      if (saved[k] != null && typeof saved[k] === typeof STYLE_DEFAULTS[k]) STYLE[k] = saved[k];
    });
  } catch (_) { /* private mode or corrupt value: keep defaults */ }

  function saveStyle() {
    try { localStorage.setItem(STYLE_KEY, JSON.stringify(STYLE)); } catch (_) { /* no-op */ }
  }
  function autoFollowOn() { return !!STYLE.autoFollow; }
  function corridorOn() { return !!STYLE.corridor; }

  function applyChartStyle() {
    if (typeof SERIES === 'undefined' || !SERIES.spot || !SERIES.spot.candle) return;
    const cs = SERIES.spot.candle, co = SERIES.opt && SERIES.opt.candle;
    const common = {
      upColor: STYLE.hollow ? 'rgba(0,0,0,0)' : STYLE.bull,
      downColor: STYLE.hollow ? 'rgba(0,0,0,0)' : STYLE.bear,
      borderVisible: true,
      borderUpColor: STYLE.bull, borderDownColor: STYLE.bear,
      wickUpColor: STYLE.bull, wickDownColor: STYLE.bear,
    };
    try { cs && cs.applyOptions(common); } catch (_) {}
    try { co && co.applyOptions(common); } catch (_) {}
    styleApplied = true;
    try {
      if (SERIES.spot.volume) SERIES.spot.volume.applyOptions({ visible: !!STYLE.volume });
      if (SERIES.opt.volume) SERIES.opt.volume.applyOptions({ visible: !!STYLE.volume });
    } catch (_) {}
    try {
      [SERIES.spot.p10, SERIES.spot.p90, SERIES.spot.median].forEach((s) => s && s.applyOptions({ visible: !!STYLE.corridor }));
    } catch (_) {}
    const so = $('overlaySpot'), oo = $('overlayOpt');
    if (so) so.style.display = '';
    if (oo) oo.style.display = '';
    if (!STYLE.corridor) { try { CONE.sig = ''; } catch (_) {} }
  }

  function syncStyleControls() {
    const pairs = [['csBull', 'bull', 'csBullHex'], ['csBear', 'bear', 'csBearHex'], ['csBullRev', 'bullRev', 'csBullRevHex'], ['csBearRev', 'bearRev', 'csBearRevHex']];
    pairs.forEach(([id, key, hexId]) => {
      const input = $(id), code = $(hexId);
      if (input) input.value = STYLE[key];
      if (code) code.textContent = STYLE[key].toUpperCase();
    });
    [['csHollow', 'hollow'], ['csReversal', 'reversal'], ['csCorridor', 'corridor'], ['csVolume', 'volume'], ['csAutoFollow', 'autoFollow']].forEach(([id, key]) => {
      const input = $(id); if (input) input.checked = !!STYLE[key];
    });
  }

  function fitCharts() {
    if (typeof CHARTS === 'undefined') return;
    try { CHARTS.spot && CHARTS.spot.timeScale().fitContent(); } catch (_) {}
    try { CHARTS.opt && CHARTS.opt.timeScale().fitContent(); } catch (_) {}
    try { if (typeof updateDateAxis === 'function') { updateDateAxis('spot'); updateDateAxis('opt'); } } catch (_) {}
  }

  function toggleStudio(force) {
    const card = $('proChartStudio');
    if (!card) return;
    const show = force == null ? card.hidden : !!force;
    card.hidden = !show;
  }

  function bindChartStudio() {
    const card = $('proChartStudio');
    if (!card) return;
    syncStyleControls();
    document.querySelectorAll('[data-chart-studio]').forEach((b) => b.addEventListener('click', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      toggleStudio();
    }));
    const closeBtn = $('csClose');
    if (closeBtn) closeBtn.addEventListener('click', () => toggleStudio(false));
    [['csBull', 'bull', 'csBullHex'], ['csBear', 'bear', 'csBearHex'], ['csBullRev', 'bullRev', 'csBullRevHex'], ['csBearRev', 'bearRev', 'csBearRevHex']].forEach(([id, key, hexId]) => {
      const input = $(id);
      if (!input) return;
      input.addEventListener('input', () => {
        STYLE[key] = input.value;
        const code = $(hexId); if (code) code.textContent = input.value.toUpperCase();
        saveStyle(); applyChartStyle(); drawSpot(); drawOption();
      });
    });
    [['csHollow', 'hollow'], ['csReversal', 'reversal'], ['csCorridor', 'corridor'], ['csVolume', 'volume'], ['csAutoFollow', 'autoFollow']].forEach(([id, key]) => {
      const input = $(id);
      if (!input) return;
      input.addEventListener('change', () => {
        STYLE[key] = !!input.checked; saveStyle(); applyChartStyle();
        if (key === 'autoFollow' && STYLE.autoFollow) fitCharts();
        drawSpot(); drawOption();
      });
    });
    const fit = $('csFit'); if (fit) fit.addEventListener('click', fitCharts);
    const reset = $('csReset');
    if (reset) reset.addEventListener('click', () => {
      STYLE = Object.assign({}, STYLE_DEFAULTS); saveStyle(); syncStyleControls(); applyChartStyle(); fitCharts(); drawSpot(); drawOption();
    });
    document.addEventListener('pointerdown', (ev) => {
      if (card.hidden) return;
      if (card.contains(ev.target) || (ev.target.closest && ev.target.closest('[data-chart-studio]'))) return;
      toggleStudio(false);
    });
  }

  function updateLevels(t) {
    const lv = (t && t.chart_levels) || {};
    const put = (id, v, suffix) => {
      const el = $(id); if (!el) return;
      el.textContent = v == null || !Number.isFinite(Number(v)) ? '—' : n(v, 2) + (suffix || '');
    };
    put('proLevelEntry', lv.entry);
    put('proLevelSl', lv.sl);
    put('proLevelT1', lv.t1);
    put('proLevelT2', lv.t2);
    put('proLevelAtp', lv.atp);
    const legend = $('proLevelsLegend');
    if (legend) {
      const tr = (t && t.active_trade) || {};
      legend.classList.toggle('closed', String(tr.status || '').toUpperCase() === 'CLOSED');
    }
  }

  const META = {
    sniper: ['🔥 CORE TRADING ENGINE · PRIORITY 1', '🎯 Reversal & SMC Sniper (₹65K)', 'Macro pool arbitration, 5-point reversal checklist, SMC order block / FVG wait-zone and Greek Taylor T1/T2/Gear-5 targets.'],
    scalp: ['🔥 CORE TRADING ENGINE · PRIORITY 1', '⚡ Smart Scalper (₹35K)', 'Fast-sleeve modes, house-money compounding, CE/PE scalp statistics and the live mutex state.'],
    parallel: ['🔥 CORE TRADING ENGINE · PRIORITY 1', '🛰️ Parallel Alpha Radar (5 Engines)', 'Five independent quant engines: parity discount, 3-second lead-lag, heavyweight tsunami, coiled spring and index rotator.'],
    hero: ['🔥 CORE TRADING ENGINE · PRIORITY 1', '💥 Expiry Zero-to-Hero', 'Gamma blast scanner, HeroScore ranker, trapped-writer fuel and AUTO / FORCE ON / OFF controls.'],
    brain: ['🧠 AI BRAIN & TELEMETRY · PRIORITY 2', '🎚️ Bull/Bear Oscillator & 3 Models', '78% institutional confirmation border plus the three Telugu KRONOS models (102M / 25M / 4M).'],
    speed: ['🧠 AI BRAIN & TELEMETRY · PRIORITY 2', '📜 Millisecond Brain Log & Speed', 'Live ticks/s, wire latency, scan Hz, six data-pipe health rows and the pausable Telugu narration terminal.'],
    health: ['📡 UPSTOX SYNC & DATA AUDIT · PRIORITY 3', '✅ 08:40 AM Health Checks', 'Six-point green-check readiness: token, runner, Cloudflare edge, V3 socket, 512 bars and three models.'],
    audit: ['📡 UPSTOX SYNC & DATA AUDIT · PRIORITY 3', '🔬 Full-Spectrum & 8/8 Model Audit', 'Eleven-strike PCR/max-pain, option Greeks, five-level OBI and the complete 8/8 model input matrix.'],
    ledger: ['📊 JOURNAL & REPORTS · PRIORITY 4', '📒 Trade Ledger & Calendar', 'Interactive trade history and the P&L calendar.'],
    journal: ['📊 JOURNAL & REPORTS · PRIORITY 4', '📄 8K Pro Journal PDF', 'One-click A4 PDF dossier with the inline chart viewer.'],
  };

  const NODES = {
    sniper: ['sniperStrip'],
    scalp: ['scalperStrip'],
    parallel: ['parallelAlpha'],
    hero: ['heroStrip'],
    brain: [],
    speed: [],
    health: ['autopilotBar'],
    audit: ['fullSpectrum', 'brainAudit'],
  };

  function unmount() {
    mounted.forEach((node) => {
      try {
        node.style.removeProperty('display');
        stash.appendChild(node);
      } catch (_) { /* keep going */ }
    });
    mounted = [];
  }

  function mountNode(node) {
    if (!node) return;
    node.hidden = false;
    body.appendChild(node);
    node.style.setProperty('display', 'block', 'important');
    mounted.push(node);
  }

  function mountXray(selectors) {
    selectors.forEach((sel) => {
      const node = document.querySelector(sel);
      if (!node) return;
      node.hidden = false;
      body.appendChild(node);
      node.style.removeProperty('display');
      mounted.push(node);
    });
  }

  function gate() {
    const t = pending || {};
    const g = (t.master_arbitrator && t.master_arbitrator.data_gate) || {};
    return { live: !!g.live, allowed: g.allowed !== false && !!g.live, reason: g.reason || 'LIVE DATA REQUIRED' };
  }

  function selectedCandidate(t) {
    const a = (t && t.master_arbitrator) || {};
    return ((a.sleeve_b || {}).selected) || ((a.sleeve_a || {}).selected) || {};
  }

  function candidateRows(t) {
    const a = (t && t.master_arbitrator) || {};
    const rows = [];
    ['sleeve_b', 'sleeve_a'].forEach((key) => {
      const s = a[key] || {};
      const c = s.candidates || [];
      const list = Array.isArray(c) ? c : Object.keys(c).map((k) => c[k]);
      list.forEach((v) => rows.push(Object.assign({ sleeve: key === 'sleeve_b' ? 'B ₹65K' : 'A ₹35K' }, v || {})));
    });
    return rows.sort((x, y) => Number(y.edge_score || 0) - Number(x.edge_score || 0));
  }

  function arbSummary(t) {
    const g = gate();
    const s = selectedCandidate(t);
    return `<div class="pro-card">
      <h3>Master Signal Arbitrator · 0–100 Edge Score</h3>
      <div class="pro-kv"><span>Data Gate</span><b class="${g.allowed ? 'pro-good' : 'pro-bad'}">${g.allowed ? 'PASS · ZERO DUMMY' : 'BLOCKED_BY_DATA_GATE'}</b></div>
      <div class="pro-kv"><span>Winning engine</span><b>${e(s.engine || 'WAITING FOR VALID SETUP')}</b></div>
      <div class="pro-kv"><span>Edge score / threshold</span><b>${s.edge_score == null ? '—' : Number(s.edge_score).toFixed(1)} / ${s.threshold == null ? '—' : Number(s.threshold).toFixed(0)}</b></div>
      <div class="pro-kv"><span>Reason</span><b>${e(s.reason || g.reason)}</b></div>
      <div class="pro-english" style="margin-top:9px">The highest validated engine is routed immediately. A lower-priority checklist cannot veto an A+ setup.</div>
      <div class="pro-telugu">అత్యధిక స్కోర్ ఉన్న ఇంజిన్ వెంటనే ఎంపిక అవుతుంది. తక్కువ ప్రాధాన్యత చెక్‌లిస్ట్ A+ ట్రేడ్‌ను ఆపదు.</div>
    </div>`;
  }

  function scoreTable(t) {
    const rows = candidateRows(t);
    if (!rows.length) return '<div class="pro-card"><h3>Candidate Ranking</h3><p class="pro-warn">No candidate published yet. Waiting for a live V3 tick and a valid closed candle.</p><div class="te">లైవ్ V3 టిక్ మరియు పూర్తి క్యాండిల్ కోసం వేచి ఉంది.</div></div>';
    return `<div class="pro-card"><h3>Candidate Ranking · Live Arbitration</h3>
      <div class="pro-stack">${rows.slice(0, 8).map((r) => `<div class="pro-kv"><span>${e(r.sleeve)} · ${e(r.engine)}</span><b class="${Number(r.valid) ? 'pro-good' : 'pro-bad'}">${r.edge_score == null ? '—' : Number(r.edge_score).toFixed(1)} · ${Number(r.valid) ? 'VALID' : 'BLOCKED'}</b></div>`).join('')}</div>
    </div>`;
  }

  function sniperDetails(t) {
    const full = ((t.full_spectrum || {})[symbol()]) || {};
    const sniper = full.sniper || {};
    const plan = ((STATE && STATE.symbols && STATE.symbols[symbol()]) || {}).plan || {};
    const checks = Array.isArray(sniper.checks) ? sniper.checks : [];
    const checkHtml = checks.length ? checks.map((c) => `<div class="pro-kv"><span>${e(c.label || c.name || c.key)}</span><b class="${c.ok ? 'pro-good' : 'pro-bad'}">${c.ok ? '✅ PASS' : '⏳ WAIT'}</b></div>`).join('') : ['HTF trend aligned', 'Liquidity sweep complete', 'SMC order block / FVG tagged', 'OBI absorption ≥ 0.72', 'Reversal candle confirmed'].map((x, i) => `<div class="pro-kv"><span>${i + 1}. ${x}</span><b class="${sniper.complete ? 'pro-good' : 'pro-warn'}">${sniper.complete ? '✅ PASS' : '⏳ WAIT'}</b></div>`).join('');
    return `<div class="pro-grid">
      <div class="pro-card"><h3>5-Point Sniper Checklist</h3>${checkHtml}<div class="pro-kv"><span>Checklist</span><b class="${sniper.complete ? 'pro-good' : 'pro-warn'}">${sniper.complete ? '5/5 READY' : 'WAITING FOR CONFLUENCE'}</b></div></div>
      <div class="pro-card"><h3>SMC Wait-Zone & Forecast Corridor</h3>
        <div class="pro-kv"><span>p10 floor</span><b>${n(plan.p10, 2)}</b></div>
        <div class="pro-kv"><span>Median trajectory</span><b>${n(plan.median, 2)}</b></div>
        <div class="pro-kv"><span>p90 ceiling</span><b>${n(plan.p90, 2)}</b></div>
        <div class="pro-kv"><span>Watch entry zone</span><b>${n(plan.watch_entry, 2)}</b></div>
        <div class="pro-kv"><span>Institutional SL</span><b class="pro-bad">${n(plan.stop_loss, 2)}</b></div>
      </div>
      <div class="pro-card"><h3>Greek Taylor Targets</h3>
        <div class="pro-kv"><span>T1 · p65</span><b class="pro-good">${n(plan.target1 || plan.p65, 2)}</b></div>
        <div class="pro-kv"><span>T2 · p90</span><b class="pro-info">${n(plan.target2 || plan.p90, 2)}</b></div>
        <div class="pro-kv"><span>Gear-5 runner</span><b>${e((t.option_chart || {}).strike_symbol || 'ARMED ON ENTRY')}</b></div>
        <div class="pro-kv"><span>Exit trigger</span><b>OBI 5L &lt; 0.72 · 1m CHoCH</b></div>
      </div>
      ${arbSummary(t)}
      ${scoreTable(t)}
    </div>`;
  }

  function scalpDetails(t) {
    const s = t.scalp_sleeve || {};
    const modes = t.scalper || {};
    const hud = t.hud || {};
    const wins = Number(s.wins || s.win_count || 0);
    const losses = Number(s.losses || s.loss_count || 0);
    const rate = wins + losses ? (wins / (wins + losses) * 100) : null;
    return `<div class="pro-grid">
      <div class="pro-card"><h3>Scalper Mode Controls</h3>
        <div class="pro-kv"><span>AUTO</span><b class="${s.mode === 'AUTO' || !s.mode ? 'pro-good' : ''}">${e(s.mode || 'AUTO')}</b></div>
        <div class="pro-kv"><span>PING-PONG</span><b>${Number(modes.ping_pong_active) ? 'ACTIVE' : 'STANDBY'}</b></div>
        <div class="pro-kv"><span>VELOCITY</span><b>${Number(modes.velocity_active) ? 'ACTIVE' : 'STANDBY'}</b></div>
        <div class="pro-kv"><span>Mutex</span><b>${e(s.mutex || s.state || 'FREE')}</b></div>
      </div>
      <div class="pro-card"><h3>House-Money Compounder</h3>
        <div class="pro-big ${Number(hud.house_money) < 0 ? 'pro-bad' : 'pro-good'}">${money(hud.house_money || s.house_money || 0)}</div>
        <div class="pro-score-track"><div class="pro-score-fill" style="width:${Math.max(0, Math.min(100, Number(hud.house_money_pct || s.house_money_pct || 0)))}%"></div></div>
        <p>Only realised house money is reinvested into the next scalp.</p>
        <div class="te">లాభపడిన డబ్బును మాత్రమే తదుపరి స్కాల్ప్‌లో పునఃవినియోగిస్తుంది.</div>
      </div>
      <div class="pro-card"><h3>CE / PE Scalp Statistics</h3>
        <div class="pro-kv"><span>CE scalps</span><b>${n(s.ce_scalps || 0)}</b></div>
        <div class="pro-kv"><span>PE scalps</span><b>${n(s.pe_scalps || 0)}</b></div>
        <div class="pro-kv"><span>Win rate</span><b class="${rate != null && rate >= 50 ? 'pro-good' : 'pro-warn'}">${rate == null ? '—' : rate.toFixed(0) + '%'}</b></div>
        <div class="pro-kv"><span>Realised</span><b>${money(s.realized_pnl_rs || 0)}</b></div>
      </div>
      <div class="pro-card"><h3>Zero-Conflict Routing</h3><p>Sleeve A can flip CE ↔ PE only after a reversal candle and a free mutex. It cannot block the ₹65K macro sleeve.</p><div class="te">మ్యూటెక్స్ ఖాళీగా ఉండి, రివర్సల్ క్యాండిల్ నిర్ధారణ అయినప్పుడు మాత్రమే CE ↔ PE మారుతుంది.</div></div>
    </div>`;
  }

  function brainMeter(t) {
    const o = t.model_oscillator || {};
    const bull = Number(o.bullish_upside_prob_pct || 0);
    const bear = Number(o.bearish_downside_prob_pct || 0);
    const border = !!(o.bullish_border_reached || o.bearish_border_reached);
    return `<div class="pro-card">
      <h3>Giant Bull / Bear Confirmation Border · 78%</h3>
      <div class="pro-grid three" style="gap:8px">
        <div><span class="pro-badge ${bull >= 78 ? 'ok' : ''}">BULLISH UPSIDE</span><div class="pro-big pro-good">${pct(bull)}</div></div>
        <div><span class="pro-badge">CHOP PRESSURE</span><div class="pro-big">${pct(o.chop_neutral_pressure_pct)}</div></div>
        <div><span class="pro-badge ${bear >= 78 ? 'no' : ''}">BEARISH DOWNSIDE</span><div class="pro-big pro-bad">${pct(bear)}</div></div>
      </div>
      <div class="pro-score-track" style="height:18px;margin-top:10px"><div class="pro-score-fill" style="width:${Math.max(bull, bear)}%;background:${bull >= bear ? 'linear-gradient(90deg,#0f766e,#34d399)' : 'linear-gradient(90deg,#881337,#fb7185)'}"></div></div>
      <div class="pro-kv"><span>Border state</span><b class="${border ? 'pro-good' : 'pro-warn'}">${e(o.border_status_te || (border ? '78% BORDER CONFIRMED' : 'WAITING FOR 78% CONFIRMATION'))}</b></div>
    </div>`;
  }

  function speedDetails(t) {
    const p = t.programmatic || {};
    const s = p.speed_telemetry || t.speed_telemetry || {};
    return `<div class="pro-grid three">
      <div class="pro-card"><h3>Ticks / second</h3><div class="pro-big pro-info">${n(s.ticks_per_sec, 1)}</div><p>V3 websocket ingestion rate</p></div>
      <div class="pro-card"><h3>Wire latency</h3><div class="pro-big pro-good">${n(s.exchange_to_server_latency_ms, 1)} ms</div><p>Exchange → sanitizer</p></div>
      <div class="pro-card"><h3>Model scan</h3><div class="pro-big pro-warn">${n(s.model_scan_latency_ms, 1)} ms</div><p>${n(s.decisions_per_sec_hz, 1)} Hz decision rate</p></div>
    </div>`;
  }

  function healthDetails(t) {
    const g = gate();
    const live = g.live;
    const checks = [
      ['Upstox token', live, 'Minted by the 08:25 IST workflow'],
      ['Runner heartbeat', live, 'GitHub Actions keepalive 08:35–15:30'],
      ['Cloudflare edge', !!(t.edge_live || live), 'Public SSE tunnel'],
      ['Upstox V3 socket', Number((((t.programmatic || {}).speed_telemetry || {}).ticks_per_sec) || 0) > 0 || live, 'Market-data websocket'],
      ['512-bar history', Array.isArray(t.candles) && t.candles.length > 0, 'Five-minute warm-up window'],
      ['Three KRONOS models', !!(t.model_oscillator && t.model_oscillator.bullish_upside_prob_pct != null), '102M / 25M / 4M loaded'],
    ];
    return `<div class="pro-grid"><div class="pro-card"><h3>Six-Point Health Gate</h3>
      ${checks.map((r) => `<div class="pro-kv"><span>${e(r[0])}<br><small style="color:#587086">${e(r[2])}</small></span><b class="${r[1] ? 'pro-good' : 'pro-bad'}">${r[1] ? '✅ READY' : '⛔ BLOCKED'}</b></div>`).join('')}
    </div><div class="pro-card"><h3>Fail-Closed Contract</h3><p>If any required stream is stale, fixture or zero, the master arbitrator hard-blocks entries with <b>BLOCKED_BY_DATA_GATE</b>. No synthetic price can ever arm a trade.</p><div class="te">ఏదైనా డేటా పాతది, ఫిక్చర్ లేదా సున్నా అయితే ఆర్బిట్రేటర్ ఎంట్రీలను ఖచ్చితంగా నిరోధిస్తుంది. నకిలీ ధరతో ట్రేడ్ జరగదు.</div><div class="pro-kv"><span>Current gate</span><b class="${g.allowed ? 'pro-good' : 'pro-bad'}">${g.allowed ? 'PASS' : e(g.reason)}</b></div></div></div>`;
  }

  function auditDetails(t) {
    const m = t.master_arbitrator || {};
    const candidates = candidateRows(t);
    return `<div class="pro-grid"><div class="pro-card"><h3>Zero-Dummy Certification</h3>
      <div class="pro-flow"><span>Upstox V3 Tick</span><i>➔</i><span>&lt;0.5ms Sanitizer</span><i>➔</i><span>102M / 25M / 4M</span><i>➔</i><span>SMC + 5 Alpha</span><i>➔</i><span>Master Arbitrator</span><i>➔</i><span>₹35K + ₹65K + Gear-5</span><i>➔</i><span>50/50 UI</span></div>
      <div class="pro-kv"><span>Published candidates</span><b>${candidates.length}</b></div>
      <div class="pro-kv"><span>Selected engine</span><b>${e(selectedCandidate(t).engine || '—')}</b></div>
      <div class="pro-kv"><span>Feed gate</span><b class="${gate().allowed ? 'pro-good' : 'pro-bad'}">${gate().allowed ? 'LIVE · VERIFIED' : 'BLOCKED'}</b></div>
    </div><div class="pro-card"><h3>Input Streams</h3>
      <div class="pro-kv"><span>Spot OHLC + SMC</span><b>${t.spot ? '✅ PRESENT' : '⛔ MISSING'}</b></div>
      <div class="pro-kv"><span>Option chain / Greeks</span><b>${t.option_chain && Object.keys(t.option_chain).length ? '✅ PRESENT' : '⛔ MISSING'}</b></div>
      <div class="pro-kv"><span>5-level depth / OBI</span><b>${(t.option || {}).obi_5 != null ? '✅ PRESENT' : '⛔ MISSING'}</b></div>
      <div class="pro-kv"><span>Parallel engines</span><b>${t.parallel_alpha ? '✅ PRESENT' : '⛔ MISSING'}</b></div>
      <div class="pro-kv"><span>Arbitrator gate</span><b>${m.data_gate ? '✅ EVALUATED' : '⛔ MISSING'}</b></div>
    </div></div>`;
  }

  function ledgerSpecial(key) {
    toggleStudio(false);
    const id = key === 'ledger' ? 'ledgerHudBtn' : 'journalQuickBtn';
    close();
    const btn = $(id) || (key === 'ledger' ? $('ledgerBtn') : null);
    if (btn) btn.click();
    else if (window.toast) window.toast('Ledger is still loading…');
  }

  function build(key, t) {
    if (key === 'sniper') return sniperDetails(t);
    if (key === 'scalp') return scalpDetails(t);
    if (key === 'brain') return brainMeter(t);
    if (key === 'speed') return speedDetails(t);
    if (key === 'health') return healthDetails(t);
    if (key === 'audit') return auditDetails(t);
    if (key === 'parallel') return `<div class="pro-card"><h3>Five Independent Quant Engines</h3><p>Parity discount, 3-second lead-lag, 5/5 heavyweight tsunami, coiled spring and index rotator scan simultaneously. The arbitrator ranks only validated outputs.</p><div class="te">ఐదు క్వాంట్ ఇంజిన్‌లు ఒకేసారి పరిశీలిస్తాయి. ధృవీకరించిన ఫలితాలను మాత్రమే ఆర్బిట్రేటర్ ర్యాంక్ చేస్తుంది.</div></div>`;
    if (key === 'hero') return `<div class="pro-card"><h3>Expiry Gamma Blast</h3><p>HeroScore ranks ATM–1 OTM strikes in the ₹8–₹32 premium band. AUTO arms only when trapped-writer fuel and 2× convexity are both verified.</p><div class="te">ATM–1 OTM స్ట్రైక్‌లలో Γ కాన్వెక్సిటీ ధృవీకరణ జరిగితేనే AUTO ఎంట్రీ అవుతుంది.</div></div>`;
    return '';
  }

  function updateHeader(t) {
    t = t || {};
    const block = (STATE && STATE.symbols) || {};
    const bank = block.BANKNIFTY || {};
    const nifty = block.NIFTY || {};
    const liveSpot = t.spot ? (t.spot.display != null ? t.spot.display : t.spot.ltp) : null;
    const niftyLtp = symbol() === 'NIFTY' && liveSpot != null ? liveSpot : nifty.spot;
    if ($('bigSpot') && niftyLtp != null) $('bigSpot').textContent = n(niftyLtp, 2);
    const bankLtp = symbol() === 'BANKNIFTY' && liveSpot != null ? liveSpot : bank.spot;
    if ($('proBankLtp')) $('proBankLtp').textContent = bankLtp == null ? '—' : n(bankLtp, 2);
    if ($('proBankDelta')) {
      const ch = Number(bank.change_pct != null ? bank.change_pct : bank.chg_pct);
      const el = $('proBankDelta');
      el.textContent = Number.isFinite(ch) ? `${ch >= 0 ? '+' : ''}${ch.toFixed(2)}%` : '—';
      el.className = Number.isFinite(ch) ? (ch >= 0 ? 'up' : 'down') : '';
    }
    const niftyCh = Number(t.spot && t.spot.change_pct != null ? t.spot.change_pct : (nifty.change_pct != null ? nifty.change_pct : nifty.chg_pct));
    const nd = $('bigSpotDelta');
    if (nd && Number.isFinite(niftyCh)) {
      nd.textContent = `${niftyCh >= 0 ? '+' : ''}${niftyCh.toFixed(2)}%`;
      nd.className = niftyCh >= 0 ? 'good' : 'bad';
    }
    const getText = (id, fallback) => { const el = $(id); return el && el.textContent ? el.textContent : fallback; };
    updateLevels(t);
    updateModalStats(t);
    if ($('proScalpPnl')) $('proScalpPnl').textContent = getText('scalpProfitMain', money((t.scalp_sleeve || {}).realized_pnl_rs || 0));
    if ($('proSniperPnl')) $('proSniperPnl').textContent = getText('sniperProfitMain', money((t.sniper_sleeve || {}).realized_pnl_rs || 0));
    if ($('proTotalPnl')) $('proTotalPnl').textContent = getText('combinedProfitMain', money(((t.hud || {}).total_net_pnl || 0)));
    if ($('proVaultState')) $('proVaultState').textContent = 'VAULT ' + getText('hudVault', money(((t.hud || {}).profit_vault || 0)));
    if ($('proSniperState')) $('proSniperState').textContent = getText('sniperProfitState', 'T1/T2 · WAITING');
    if ($('proScalpState')) $('proScalpState').textContent = ((t.scalp_sleeve || {}).state || getText('scalpProfitTicker', 'WAITING')).slice(0, 42);

    const call = t.chart_call || {};
    const callout = $('proAiCallout');
    if (callout) {
      callout.textContent = call.text || '🟡 KRONOS AI: WAITING FOR LIVE 3-MODEL FUSION';
      callout.className = 'pro-ai-callout ' + (String(call.direction || '').toLowerCase() === 'bull' ? 'bull' : String(call.direction || '').toLowerCase() === 'bear' ? 'bear' : 'chop');
    }
    const oc = t.option_chart || {};
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v == null || v === '' ? '—' : v; };
    set('proOptLtp', oc.ltp == null ? null : '₹' + n(oc.ltp, 2));
    set('proOptDelta', oc.delta == null ? null : Number(oc.delta).toFixed(2));
    set('proOptGamma', oc.gamma == null ? null : Number(oc.gamma).toFixed(4));
    set('proOptIv', oc.iv == null ? null : Number(oc.iv).toFixed(2) + '%');
    set('proOptObi', oc.obi_5 == null ? null : Number(oc.obi_5).toFixed(2));

    const g = gate();
    const checks = [
      !!(t.spot && !t.stale), !!(t.option && t.option.ltp), !!(t.option_chain && Object.keys(t.option_chain).length),
      !!(t.programmatic && t.programmatic.speed_telemetry), !!(t.model_oscillator), g.live,
    ].filter(Boolean).length;
    const pill = $('proConnPill');
    if (pill) {
      const direct = !!(LIVE.edge && LIVE.edge.open && edgeBase());
      const online = g.live && checks >= 6;
      pill.className = 'pro-conn ' + (direct || online ? 'online' : 'offline');
      pill.textContent = direct
        ? edgeBadgeText()
        : online ? '🟢 6/6 LIVE CONNECTED' : `🔴 ${checks}/6 STALE / OFFLINE`;
    }
    const feed = $('proSideFeed');
    if (feed) feed.textContent = g.live ? '● LIVE V3 · SANITIZER OK' : '● STALE / OFFLINE · ENTRIES BLOCKED';
    const ml = $('proModalLive');
    if (ml) ml.textContent = g.live ? '🟢 LIVE V3 · ZERO-DUMMY GATE PASS' : '🔴 BLOCKED_BY_DATA_GATE';

    const sym = symbol();
    document.querySelectorAll('[data-pro-index]').forEach((b) => b.classList.toggle('on', b.dataset.proIndex === sym));
    updateDots(t, g);
  }

  function updateModalStats(t) {
    const s = selectedCandidate(t);
    const g = gate();
    const edge = $('proModalEdge'), gateEl = $('proModalGate'), sleeve = $('proModalSleeve');
    if (edge) edge.textContent = s.edge_score == null ? '—' : Number(s.edge_score).toFixed(1);
    if (gateEl) {
      gateEl.textContent = g.allowed ? 'PASS' : 'BLOCKED';
      gateEl.className = g.allowed ? 'ok' : 'no';
    }
    if (sleeve) sleeve.textContent = s.sleeve === 'sleeve_a' || s.sleeve === 'A ₹35K' ? '₹35K SCALP' : s.sleeve === 'sleeve_b' || s.sleeve === 'B ₹65K' ? '₹65K MACRO' : 'STANDBY';
  }

  function updateDots(t, g) {
    const set = (key, cls) => {
      const dot = document.querySelector(`[data-stat="${key}"]`);
      if (dot) dot.className = 'dot ' + cls;
    };
    if (!g.live) {
      ['sniper', 'scalp', 'parallel', 'hero', 'brain', 'speed'].forEach((k) => set(k, 'blocked'));
      set('health', 'blocked');
      ['audit', 'ledger', 'journal'].forEach((k) => set(k, 'active'));
      return;
    }
    const a = t.master_arbitrator || {};
    const s = selectedCandidate(t);
    const tradeOpen = String(((t.active_trade || {}).status) || '').toUpperCase() === 'OPEN';
    set('sniper', s.edge_score != null || tradeOpen ? 'active' : 'waiting');
    set('scalp', Number((t.scalp_sleeve || {}).open_qty || 0) > 0 ? 'active' : 'waiting');
    set('parallel', Number(((t.parallel_alpha || {}).active_count) || 0) > 0 ? 'active' : 'waiting');
    set('hero', Number(((t.hero || {}).trades_today) || 0) > 0 ? 'active' : 'waiting');
    set('brain', a.data_gate ? 'active' : 'waiting');
    set('speed', Number((((t.programmatic || {}).speed_telemetry || {}).ticks_per_sec) || 0) > 0 ? 'active' : 'waiting');
    set('health', (a.data_gate || {}).allowed ? 'active' : 'waiting');
    set('audit', 'active');
    set('ledger', 'active');
    set('journal', 'active');
  }

  function flushPaint() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    if (paintTimer) { clearTimeout(paintTimer); paintTimer = 0; }
    const tick = pending;
    try { updateHeader(tick); } catch (err) { console.warn('KRONOS pro header paint failed', err); }
    drawSpot(); drawOption();
    if (active) {
      const live = $('proModalLive');
      if (live && tick) {
        const g = gate();
        live.textContent = g.live ? '🟢 LIVE V3 · ZERO-DUMMY GATE PASS' : '🔴 BLOCKED_BY_DATA_GATE';
      }
    }
  }

  function paint(t) {
    pending = t || pending;
    if (raf || paintTimer) return;
    raf = requestAnimationFrame(flushPaint);
    /* Visible tabs paint in the next frame; hidden tabs still refresh at 8 Hz. */
    paintTimer = setTimeout(flushPaint, 120);
  }

  function refreshMounted(key) {
    const t = pending || {};
    try {
      if (key === 'sniper' || key === 'audit') window.renderFullSpectrum && window.renderFullSpectrum();
      if (key === 'sniper') window.renderSniper && window.renderSniper();
      if (key === 'scalp') window.renderScalper && window.renderScalper();
      if (key === 'parallel') window.paintParallelRadar && window.paintParallelRadar();
      if (key === 'hero') window.renderHero && window.renderHero();
      if (key === 'brain' || key === 'speed' || key === 'audit') window.paintXray && window.paintXray(t);
    } catch (err) { console.warn('KRONOS modal refresh failed', err); }
  }

  function open(key) {
    if (!META[key]) return;
    if (key === 'ledger' || key === 'journal') { ledgerSpecial(key); return; }
    toggleStudio(false);
    if (active) close(false);
    active = key;
    pending = (typeof LIVE !== 'undefined' && LIVE.tick) ? LIVE.tick : pending;
    const meta = META[key];
    $('proModalCat').textContent = meta[0];
    $('proModalTitle').textContent = meta[1];
    $('proModalSub').textContent = meta[2];
    body.innerHTML = build(key, pending || {});
    body.scrollTop = 0;
    updateModalStats(pending || {});
    if (key === 'brain') mountXray(['#xray .td-zone-osc', '#xray .td-zone-models']);
    else if (key === 'speed') mountXray(['#xray .td-zone-speed', '#xray .td-zone-log']);
    else (NODES[key] || []).forEach((id) => mountNode($(id)));
    modal.hidden = false;
    document.body.classList.add('pro-modal-open');
    document.querySelectorAll('.pro-nav-item').forEach((b) => b.classList.toggle('active', b.dataset.proModal === key));
    requestAnimationFrame(() => refreshMounted(key));
    closeSidebar();
  }

  function close(clearActive = true) {
    if (!modal) return;
    unmount();
    modal.hidden = true;
    body.innerHTML = '';
    document.body.classList.remove('pro-modal-open');
    document.querySelectorAll('.pro-nav-item').forEach((b) => b.classList.remove('active'));
    if (clearActive) active = null;
  }

  function closeSidebar() {
    const side = $('proSidebar');
    if (side) side.classList.remove('open');
    const scrim = $('proSidebarScrim');
    if (scrim) scrim.classList.remove('show');
  }

  function toggleSidebar() {
    const side = $('proSidebar');
    if (!side) return;
    const on = !side.classList.contains('open');
    side.classList.toggle('open', on);
    const scrim = $('proSidebarScrim');
    if (scrim) scrim.classList.toggle('show', on);
  }

  document.querySelectorAll('[data-pro-modal]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      open(btn.dataset.proModal);
    });
  });
  const closeBtn = $('proModalClose');
  if (closeBtn) closeBtn.addEventListener('click', () => close());
  if (modal) modal.addEventListener('click', (ev) => { if (ev.target === modal) close(); });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      const studio = $('proChartStudio');
      if (studio && !studio.hidden) toggleStudio(false);
      else if (modal && !modal.hidden) close();
      else closeSidebar();
    }
  });
  const menu = $('menuBtn');
  if (menu) menu.addEventListener('click', toggleSidebar);
  const sideClose = $('proSidebarClose');
  if (sideClose) sideClose.addEventListener('click', closeSidebar);
  const scrim = $('proSidebarScrim');
  if (scrim) scrim.addEventListener('click', closeSidebar);
  document.querySelectorAll('[data-pro-index]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = document.querySelector(`.tab[data-sym="${btn.dataset.proIndex}"]`);
      if (target) target.click();
      else if (btn.dataset.proIndex === 'NIFTY' || btn.dataset.proIndex === 'BANKNIFTY') {
        try { SYMBOL = btn.dataset.proIndex; } catch (_) { /* no-op */ }
      }
      document.querySelectorAll('[data-pro-index]').forEach((b) => b.classList.toggle('on', b === btn));
    });
  });
  window.addEventListener('resize', () => { flushPaint(); paint(pending); });

  function drawCandleContract(chart, series, overlayId, visuals) {
    if (!chart || !series || !visuals || !Array.isArray(visuals.candles)) return;
    const canvas = $(overlayId);
    if (!canvas) return;
    const w = canvas.clientWidth || canvas.parentElement.clientWidth;
    const h = canvas.clientHeight || canvas.parentElement.clientHeight;
    if (w < 30 || h < 30) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr); canvas.height = Math.floor(h * dpr);
      canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const shown = new Set(((chart.__data || []).slice(-260)).map((r) => Number(r.time)));
    const rows = visuals.candles.filter((r) => !shown.size || shown.has(Number(r.time))).slice(-180);
    if (!rows.length) return;
    let spacing = 7;
    try { spacing = Math.max(2, Math.min(11, chart.timeScale().options().barSpacing || 7)); } catch (_) { /* default */ }
    const body = Math.max(1.5, spacing * 0.58);
    rows.forEach((r) => {
      let x = null;
      try { x = chart.timeScale().timeToCoordinate(r.time); } catch (_) { x = null; }
      if (x == null || x < -10 || x > w + 10) return;
      const yH = series.priceToCoordinate(r.high), yL = series.priceToCoordinate(r.low);
      const yO = series.priceToCoordinate(r.open), yC = series.priceToCoordinate(r.close);
      if ([yH, yL, yO, yC].some((v) => v == null)) return;
      const isBull = Number(r.close) >= Number(r.open);
      const isRev = !!r.is_reversal && STYLE.reversal;
      /* Filled mode delegates normal bodies to Lightweight Charts; only the
         reversal candle needs the glow/marker overlay, so nothing is painted twice. */
      if (!STYLE.hollow && !isRev) return;
      const color = isRev ? (isBull ? STYLE.bullRev : STYLE.bearRev) : (isBull ? STYLE.bull : STYLE.bear);
      ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath();
      ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke();
      const top = Math.min(yO, yC), height = Math.max(1.2, Math.abs(yC - yO));
      if (STYLE.hollow) {
        ctx.fillStyle = '#070c13'; ctx.fillRect(x - body / 2, top, body, height);
        ctx.strokeStyle = color; ctx.lineWidth = 1.2;
        ctx.strokeRect(x - body / 2, top, body, height);
      } else {
        ctx.fillStyle = color; ctx.fillRect(x - body / 2, top, body, height);
      }
      if (isRev) {
        ctx.save(); ctx.shadowColor = color; ctx.shadowBlur = 13;
        ctx.fillStyle = STYLE.hollow ? 'rgba(7,12,19,.55)' : color;
        ctx.fillRect(x - body / 2, top, body, height);
        ctx.strokeStyle = color; ctx.lineWidth = 1.6;
        ctx.strokeRect(x - body / 2, top, body, height); ctx.restore();
        const y = r.reversal === 'BULL' ? yL + 8 : yH - 8;
        ctx.fillStyle = color; ctx.font = '700 8px "Roboto Mono",monospace';
        ctx.textAlign = 'center';
        ctx.fillText(r.reversal === 'BULL' ? '▲ BUY CE' : '▼ EXIT / PE', x, y);
      }
    });
  }

  function drawSpot() {
    try {
      if (!styleApplied) applyChartStyle();
      const t = (typeof LIVE !== 'undefined' && LIVE.tick) || pending || {};
      const cv = t.candle_visuals || {};
      const tf = (typeof TF !== 'undefined') ? TF : '5m';
      const v = (cv.spot_by_tf && cv.spot_by_tf[tf]) || cv.spot;
      if (typeof CHARTS !== 'undefined' && CHARTS.spot && typeof SERIES !== 'undefined') {
        drawCandleContract(CHARTS.spot, SERIES.spot.candle, 'overlaySpot', v);
      }
    } catch (err) { console.warn('KRONOS hollow-candle paint failed', err); }
  }

  function drawOption() {
    try {
      if (!styleApplied) applyChartStyle();
      const t = (typeof LIVE !== 'undefined' && LIVE.tick) || pending || {};
      const cv = t.candle_visuals || {};
      const tf = (typeof OPT_TF !== 'undefined') ? OPT_TF : '5m';
      const v = (cv.option_by_tf && cv.option_by_tf[tf]) || cv.option;
      if (typeof CHARTS !== 'undefined' && CHARTS.opt && typeof SERIES !== 'undefined') {
        drawCandleContract(CHARTS.opt, SERIES.opt.candle, 'overlayOpt', v);
      }
    } catch (err) { console.warn('KRONOS option-candle paint failed', err); }
  }

  window.kronosPro = {
    open,
    close,
    drawSpot,
    drawOption,
    fitCharts,
    autoFollowOn,
    corridorOn,
    edgeApi,
    edgeBase,
    applyChartStyle,
    style: () => Object.assign({}, STYLE),
    openStudio: () => toggleStudio(true),
    closeStudio: () => toggleStudio(false),
    shouldRender: (group) => {
      if (!active) return false;
      if (group === 'full') return active === 'sniper' || active === 'audit' || active === 'health';
      if (group === 'brain') return active === 'brain' || active === 'speed' || active === 'audit';
      return active === group;
    },
    isOpen,
  };
  window.kronosProTick = paint;
  bindChartStudio();
  applyChartStyle();
  if (typeof LIVE !== 'undefined' && LIVE.tick) paint(LIVE.tick);
  paint(null);
  setTimeout(() => paint((typeof LIVE !== 'undefined' && LIVE.tick) ? LIVE.tick : null), 600);
})();
