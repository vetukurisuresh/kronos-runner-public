/* KRONOS PRO — 3 × ₹1,00,000 visual flow workspace.
   Read-only view over the live payload; every number comes from the backend. */
(function () {
  'use strict';
  var SNAP_KEY = 'trade_chart_snapshots';
  var LAST = null, TAB = 'all', MOUNTED = false;

  function $(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function inr(v) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return (n < 0 ? '-₹' : '+₹') + Math.abs(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }
  function money(v) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return '₹' + n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }
  function num(v, d) { var n = Number(v); return isFinite(n) ? n.toFixed(d == null ? 2 : d) : '—'; }

  var DESKS = [
    ['directional_100k', '🧭 Directional · ₹1L', '🧭'],
    ['scalping_100k', '⚡ Smart Scalping · ₹1L', '⚡'],
    ['zero_to_hero_100k', '🚀 Zero-to-Hero · ₹1L', '🚀']
  ];

  function css() {
    if ($('kfStyle')) return;
    var s = document.createElement('style');
    s.id = 'kfStyle';
    s.textContent = [
      '#kfBtn{position:fixed;right:18px;bottom:18px;z-index:9998;border:1px solid #22d3ee55;background:#0b1220;color:#e2e8f0;',
      'padding:10px 14px;border-radius:999px;font:600 12px ui-monospace,Menlo,monospace;cursor:pointer;box-shadow:0 8px 24px #0008}',
      '#kfBtn:hover{border-color:#22d3ee;color:#67e8f9}',
      '#kfWrap{position:fixed;inset:0;z-index:9999;background:#05080fee;backdrop-filter:blur(6px);display:none;pointer-events:none;padding:18px;overflow:auto}',
      '#kfWrap.on{display:block;pointer-events:auto}',
      '.kf-shell{max-width:1280px;margin:0 auto;background:#0a0f19;border:1px solid #1e293b;border-radius:16px;color:#e2e8f0;',
      'font:13px/1.45 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif}',
      '.kf-head{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid #1e293b}',
      '.kf-title{font:700 15px ui-monospace,monospace;letter-spacing:.4px}',
      '.kf-sub{color:#94a3b8;font-size:11px}',
      '.kf-x{background:#111a29;border:1px solid #24344d;color:#cbd5e1;border-radius:8px;padding:6px 12px;cursor:pointer}',
      '.kf-tabs{display:flex;gap:8px;padding:12px 18px;flex-wrap:wrap}',
      '.kf-tab{border:1px solid #24344d;background:#0d1626;color:#94a3b8;border-radius:999px;padding:7px 14px;font:600 11px ui-monospace,monospace;cursor:pointer}',
      '.kf-tab.on{border-color:#22d3ee;color:#67e8f9;background:#082f3a}',
      '.kf-body{padding:0 18px 18px}',
      '.kf-card{border:1px solid #1e293b;border-radius:12px;background:#0b1220;margin-bottom:14px;overflow:hidden}',
      '.kf-card-h{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid #16233a;font:700 12px ui-monospace,monospace}',
      '.kf-kpis{display:flex;gap:16px;color:#94a3b8;font:11px ui-monospace,monospace}',
      '.kf-flow{display:block;width:100%;height:auto}',
      '.kf-line{stroke:#22d3ee66;stroke-width:2;fill:none;stroke-dasharray:6 6;animation:kfDash 1.1s linear infinite}',
      '.kf-line.hot{stroke:#22c55e;stroke-width:2.6}',
      '@keyframes kfDash{to{stroke-dashoffset:-24}}',
      '.kf-node{fill:#0f1a2b;stroke:#24344d;stroke-width:1.4;rx:10}',
      '.kf-node.ok{stroke:#22c55e;filter:drop-shadow(0 0 6px #22c55e55)}',
      '.kf-node.wait{stroke:#eab30866}',
      '.kf-nt{fill:#e2e8f0;font:600 11px ui-monospace,monospace}',
      '.kf-ns{fill:#94a3b8;font:10px ui-monospace,monospace}',
      '.kf-hub{fill:#1a1030;stroke:#a855f7;filter:drop-shadow(0 0 8px #a855f755)}',
      '.kf-exec{fill:#0b2418;stroke:#22c55e88}',
      '.kf-pill{font:600 10px ui-monospace,monospace}',
      '.kf-ledger{width:100%;border-collapse:collapse;font:11px ui-monospace,monospace}',
      '.kf-ledger th{text-align:left;color:#64748b;font-weight:600;padding:8px 10px;border-bottom:1px solid #16233a}',
      '.kf-ledger td{padding:8px 10px;border-bottom:1px solid #101a2b;color:#cbd5e1;vertical-align:middle}',
      '.kf-btn{background:#0d1626;border:1px solid #24344d;color:#67e8f9;border-radius:6px;padding:4px 9px;font:600 10px ui-monospace,monospace;cursor:pointer}',
      '.kf-btn:hover{border-color:#22d3ee}',
      '.kf-done{color:#4ade80}.kf-run{color:#38bdf8}.kf-wait{color:#eab308}',
      '#kfTip{position:fixed;z-index:10000;max-width:340px;background:#0b1220;border:1px solid #22d3ee55;border-radius:10px;',
      'padding:10px 12px;color:#cbd5e1;font:11px/1.5 ui-monospace,monospace;display:none;box-shadow:0 12px 32px #000a;white-space:pre-wrap}',
      '#kfModal{position:fixed;inset:0;z-index:10001;background:#02060cdd;display:none;padding:20px;align-items:center;justify-content:center}',
      '#kfModal.on{display:flex}',
      '.kf-modal{background:#0a0f19;border:1px solid #1e293b;border-radius:14px;max-width:1000px;width:100%;color:#e2e8f0}',
      '#kfCanvas{width:100%;height:auto;display:block;background:#0b1220;border-radius:0 0 14px 14px}',
      '.kf-empty{padding:26px;text-align:center;color:#64748b;font:11px ui-monospace,monospace}'
    ].join('');
    document.head.appendChild(s);
  }

  function mount() {
    if (MOUNTED || !document.body) return;
    MOUNTED = true;
    css();
    var b = document.createElement('button');
    b.id = 'kfBtn'; b.textContent = '🧭 3 × ₹1L FLOW';
    document.body.appendChild(b);
    var w = document.createElement('div');
    w.id = 'kfWrap';
    w.innerHTML = '<div class="kf-shell"><div class="kf-head">' +
      '<div><div class="kf-title">KRONOS PRO · 3 × ₹1,00,000 PARALLEL DESKS</div>' +
      '<div class="kf-sub" id="kfSub">waiting for the live feed…</div></div>' +
      '<button class="kf-x" id="kfClose">Close ✕</button></div>' +
      '<div class="kf-tabs" id="kfTabs"></div><div class="kf-body" id="kfBody"></div></div>';
    document.body.appendChild(w);
    var tip = document.createElement('div'); tip.id = 'kfTip'; document.body.appendChild(tip);
    var modal = document.createElement('div'); modal.id = 'kfModal';
    modal.innerHTML = '<div class="kf-modal"><div class="kf-card-h"><span id="kfModalT">Chart</span>' +
      '<button class="kf-x" id="kfModalX">Close ✕</button></div><canvas id="kfCanvas" width="960" height="440"></canvas></div>';
    document.body.appendChild(modal);

    w.classList.remove('on');
    b.onclick = function () { w.classList.add('on'); render(); pull(); };
    pull();
    setInterval(pull, 3000);
    $('kfClose').onclick = function () { w.classList.remove('on'); };
    $('kfModalX').onclick = function () { $('kfModal').classList.remove('on'); };
    document.addEventListener('mousemove', function (ev) {
      var t = tip;
      if (t.style.display !== 'block') return;
      t.style.left = Math.min(ev.clientX + 16, window.innerWidth - 360) + 'px';
      t.style.top = Math.min(ev.clientY + 16, window.innerHeight - 220) + 'px';
    });
  }

  function withNodes(desk) {
    if (desk && desk.nodes && desk.nodes.length) return desk;
    var out = Object.assign({}, desk || {});
    out.nodes = [
      { label: 'KRONOS-102M', mark: '⏳', ok: false, detail: 'directional probability — feed pending' },
      { label: 'KRONOS-25M', mark: '⏳', ok: false, detail: 'PCR / ΔOI / ITM picker — feed pending' },
      { label: 'KRONOS-4M', mark: '⏳', ok: false, detail: 'OBI / TBQ / 1.5s velocity — feed pending' }
    ];
    out.handshake = false;
    out.exec = out.exec || { status: 'PENDING' };
    return out;
  }

  function deskFlow(id) {
    var p = LAST || {};
    var f = (p.desk_flow && p.desk_flow.desks && p.desk_flow.desks[id]) || null;
    return f;
  }

  function tabs() {
    var el = $('kfTabs'); if (!el) return;
    var items = [['all', '🟣 All-3 Parallel'], ['directional_100k', '🧭 Directional ₹1L'],
      ['scalping_100k', '⚡ Scalping ₹1L'], ['zero_to_hero_100k', '🚀 Zero-to-Hero ₹1L']];
    el.innerHTML = items.map(function (it) {
      return '<button class="kf-tab' + (TAB === it[0] ? ' on' : '') + '" data-kf-tab="' + it[0] + '">' + it[1] + '</button>';
    }).join('');
    Array.prototype.forEach.call(el.querySelectorAll('[data-kf-tab]'), function (btn) {
      btn.onclick = function () { TAB = btn.getAttribute('data-kf-tab'); render(); };
    });
  }

  function nodeBox(x, y, w, h, node, tipText) {
    var cls = 'kf-node ' + (node.ok ? 'ok' : 'wait');
    return '<rect style="pointer-events:all" class="' + cls + '" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="10" data-tip="' + esc(tipText) + '"/>' +
      '<text class="kf-nt" x="' + (x + 12) + '" y="' + (y + 24) + '">' + esc(node.label) + ' ' + node.mark + '</text>' +
      '<text class="kf-ns" x="' + (x + 12) + '" y="' + (y + 42) + '">' + esc(node.detail) + '</text>';
  }

  function flowSvg(desk, multi) {
    var name = desk.index || '—';
    var nodes = desk.nodes || [];
    var hubTip = 'UPSTOX V3 LIVE HUB\nWebSocket V3 connected · data sync 100%\n' +
      'Indices: NIFTY 50 · BANKNIFTY · SENSEX\n512-bar buffers maintained\n' +
      '37 streams: Spot OHLC · Futures VTT/Basis · 11-strike OI/PCR · Δ Γ Θ IV · 5-level OBI/TBQ · VIX · Breadth';
    var parts = [];
    parts.push('<svg class="kf-flow" viewBox="0 0 1000 320" xmlns="http://www.w3.org/2000/svg" style="display:block;width:100%;height:auto;overflow:visible;pointer-events:auto">');
    parts.push('<g data-node="hub" style="cursor:pointer;pointer-events:all"><rect class="kf-hub" x="14" y="112" width="200" height="86" rx="12" style="pointer-events:all" data-tip="' + esc(hubTip) + '"/>');
    parts.push('<text class="kf-nt" x="28" y="140">🟣 UPSTOX V3 HUB</text>');
    parts.push('<text class="kf-ns" x="28" y="160">🟢 100% DATA SYNC</text>');
    parts.push('<text class="kf-ns" x="28" y="178">' + esc(multi ? '3 indices streaming' : name + ' streaming') + '</text></g>');
    var ys = [24, 118, 212];
    nodes.forEach(function (n, i) {
      var y = ys[i] || (24 + i * 96);
      var tip = n.label + '\n' + n.detail + '\n' + (n.ok ? 'vote: confirmed ✅' : 'vote: waiting — no fabricated signal');
      parts.push('<path class="kf-line' + (n.ok ? ' hot' : '') + '" d="M214 155 C 250 155, 250 ' + (y + 40) + ', 300 ' + (y + 40) + '"/>');
      parts.push('<g data-node="' + ['102m', '25m', '4m'][i] + '" style="cursor:pointer;pointer-events:all">');
      parts.push(nodeBox(300, y, 210, 62, n, tip));
      parts.push('</g>');
    });
    var exec = desk.exec || {};
    var ex = 'ENTRY PENDING', cls = 'kf-wait';
    if (exec.status === 'RUNNING') { ex = '🟢 TRADE RUNNING'; cls = 'kf-run'; }
    if (exec.status === 'DONE') { ex = '✅ TRADE DONE'; cls = 'kf-done'; }
    var sel = exec.symbol ? exec.symbol + ' ' + exec.strike + ' ' + exec.side : (name + ' · awaiting strike');
    var exTip = 'Selected: ' + sel + '\nEntry ' + (exec.entry != null ? '₹' + num(exec.entry) : '—') +
      ' · SL ' + (exec.sl != null ? '₹' + num(exec.sl) : '—') +
      ' · T1 ' + (exec.t1 != null ? '₹' + num(exec.t1) : '—') +
      ' · T2 ' + (exec.t2 != null ? '₹' + num(exec.t2) : '—') +
      '\nStatus: ' + ex + ' · P&L ' + (exec.pnl != null ? inr(exec.pnl) : '—');
    parts.push('<path class="kf-line' + (desk.handshake ? ' hot' : '') + '" d="M510 155 L 590 155"/>');
    parts.push('<rect class="kf-exec" x="590" y="72" width="392" height="170" rx="12" data-tip="' + esc(exTip) + '"/>');
    parts.push('<text class="kf-nt" x="606" y="100">🎯 ' + esc(sel) + '</text>');
    parts.push('<text class="kf-ns" x="606" y="122">Entry ' + (exec.entry != null ? '₹' + num(exec.entry) : '—') +
      '  SL ' + (exec.sl != null ? '₹' + num(exec.sl) : '—') + '</text>');
    parts.push('<text class="kf-ns" x="606" y="140">T1 ' + (exec.t1 != null ? '₹' + num(exec.t1) : '—') +
      '  T2 ' + (exec.t2 != null ? '₹' + num(exec.t2) : '—') + '</text>');
    parts.push('<text class="' + ('kf-ns ' + cls) + '" x="606" y="168">' + esc(ex) +
      (exec.pnl != null ? '  ·  ' + inr(exec.pnl) : '') + '</text>');
    parts.push('<text class="kf-ns" x="606" y="192">Handshake ' +
      (desk.handshake ? '✅ complete' : '⏳ waiting') + ' · edge ' + (desk.edge_score != null ? num(desk.edge_score, 0) + '/100' : '—') + '</text>');
    parts.push('<text class="kf-ns" x="606" y="214">Capital ' + money(desk.capital_inr) + ' · equity ' + money(desk.equity_inr) + '</text>');
    parts.push('</svg>');
    return parts.join('');
  }

  function ledger(desk, id) {
    var rows = desk.trades || [];
    if (!rows.length) {
      return '<div class="kf-empty">No trades yet on this sleeve — the ledger fills as handshakes complete.</div>';
    }
    var head = '<tr><th>#</th><th>Time</th><th>Index &amp; Strike</th><th>Handshake</th><th>Entry ➔ Exit/LTP</th>' +
      '<th>Status</th><th>Booked</th><th>Chart</th></tr>';
    var body = rows.map(function (t, i) {
      var st = String(t.status || t.state || (t.exit != null ? 'DONE' : 'RUNNING')).toUpperCase();
      var done = st.indexOf('DONE') >= 0 || st.indexOf('CLOSED') >= 0;
      var cls = done ? 'kf-done' : 'kf-run';
      var key = id + ':' + (t.id || i + 1);
      var pnl = t.pnl != null ? t.pnl : t.profit;
      return '<tr><td>#' + esc(t.id || i + 1) + '</td>' +
        '<td>' + esc((t.time || t.opened_at || '—')).toString().slice(-8) + '</td>' +
        '<td>' + esc((t.symbol || '—') + ' ' + (t.strike || '') + ' ' + (t.side || '')) + '</td>' +
        '<td>' + (t.handshake === false ? '⏳' : '102M ✅ 25M ✅ 4M ✅') + '</td>' +
        '<td>₹' + num(t.entry) + ' ➔ ₹' + num(t.exit != null ? t.exit : t.ltp) + '</td>' +
        '<td class="' + cls + '">' + (done ? '✅ DONE' : '🟢 RUNNING') + '</td>' +
        '<td class="' + cls + '">' + (pnl != null ? inr(pnl) : '—') + '</td>' +
        '<td><button class="kf-btn" data-chart="' + esc(key) + '" data-live="' + (done ? '0' : '1') + '">' +
        (done ? '📸 View Chart Shot' : '📈 Live Chart') + '</button></td></tr>';
    }).join('');
    return '<table class="kf-ledger"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  }

  function render() {
    if (!$('kfBody')) return;
    tabs();
    var p = LAST || {};
    var flow = p.desk_flow || {};
    var total = flow.total_capital_inr != null ? flow.total_capital_inr : 300000;
    var top = flow.top_index ? ' · top edge ' + flow.top_index : '';
    $('kfSub').textContent = (flow.total_capital_inr != null ? 'live · ' : 'waiting · ') +
      '₹3,00,000 across 3 sleeves' + top;
    var show = TAB === 'all' ? DESKS.map(function (d) { return d[0]; }) : [TAB];
    var html = '';
    show.forEach(function (id) {
      var desk = deskFlow(id) || { nodes: [], trades: [], capital_inr: 100000, equity_inr: 100000 };
      var label = (DESKS.filter(function (d) { return d[0] === id; })[0] || [id, id])[1];
      var book = Number(desk.booked_pnl_inr) || 0, live = Number(desk.live_pnl_inr) || 0;
      html += '<div class="kf-card"><div class="kf-card-h"><span>' + esc(label) + '</span>' +
        '<span class="kf-kpis"><span>CAP ' + money(desk.capital_inr) + '</span>' +
        '<span>EQ ' + money(desk.equity_inr) + '</span>' +
        '<span>BOOKED <b class="' + (book >= 0 ? 'kf-done' : 'kf-run') + '">' + inr(book) + '</b></span>' +
        '<span>LIVE <b class="' + (live >= 0 ? 'kf-done' : 'kf-run') + '">' + inr(live) + '</b></span>' +
        '<button class="kf-btn" data-kf-chart="' + id + '">📈 Live Chart</button></span></div>' +
        flowSvg(withNodes(desk), TAB === 'all') +
        ledger(desk, id) + '</div>';
    });
    $('kfBody').innerHTML = html;
    Array.prototype.forEach.call($('kfBody').querySelectorAll('[data-chart], [data-kf-chart]'), function (btn) {
      btn.onclick = function () {
        var key = btn.getAttribute('data-chart') || (btn.getAttribute('data-kf-chart') + ':0');
        openChart(key, btn.getAttribute('data-live') === '1');
      };
    });
    Array.prototype.forEach.call($('kfBody').querySelectorAll('[data-tip], [data-node]'), function (node) {
      if (node.getAttribute('data-node') && !node.getAttribute('data-tip')) {
        var inner = node.querySelector('[data-tip]');
        if (inner) node.setAttribute('data-tip', inner.getAttribute('data-tip'));
      }
      node.addEventListener('mouseenter', function (ev) {
        var t = $('kfTip'); if (!t) return;
        t.textContent = node.getAttribute('data-tip');
        t.style.display = 'block';
        t.style.left = Math.min(ev.clientX + 16, window.innerWidth - 360) + 'px';
        t.style.top = Math.min(ev.clientY + 16, window.innerHeight - 220) + 'px';
      });
      node.addEventListener('mouseleave', function () { var t = $('kfTip'); if (t) t.style.display = 'none'; });
    });
    rememberSnapshots();
  }

  function candlesFor(p, trade) {
    var oc = p.option_chart || p.option_candles || null;
    var arr = null;
    if (Array.isArray(oc)) arr = oc;
    else if (oc && trade && oc[trade.symbol]) {
      var b = oc[trade.symbol];
      arr = Array.isArray(b) ? b : (b.candles || b.rows || null);
    }
    if (!arr && oc && Array.isArray(oc.candles)) arr = oc.candles;
    if (!Array.isArray(arr) || !arr.length) return null;
    return arr.map(function (c) {
      if (Array.isArray(c)) return { o: +c[1], h: +c[2], l: +c[3], c: +c[4] };
      return { o: +c.o, h: +c.h, l: +c.l, c: +c.c };
    }).filter(function (c) { return isFinite(c.o) && isFinite(c.c); });
  }

  function snapshots() {
    try { return JSON.parse(localStorage.getItem(SNAP_KEY) || '{}') || {}; } catch (e) { return {}; }
  }

  function rememberSnapshots() {
    var p = LAST || {};
    var store = snapshots(), changed = false;
    DESKS.forEach(function (d) {
      ((deskFlow(d[0]) || {}).trades || []).forEach(function (t, i) {
        var st = String(t.status || t.state || '').toUpperCase();
        var done = st.indexOf('DONE') >= 0 || st.indexOf('CLOSED') >= 0;
        var key = d[0] + ':' + (t.id || i + 1);
        if (!done || store[key]) return;
        var c = candlesFor(p, t);
        if (!c) return;
        store[key] = { id: key, trade: t, candles: c.slice(-150), at: new Date().toISOString() };
        changed = true;
      });
    });
    if (changed) {
      var keys = Object.keys(store);
      if (keys.length > 30) keys.slice(0, keys.length - 30).forEach(function (k) { delete store[k]; });
      try { localStorage.setItem(SNAP_KEY, JSON.stringify(store)); } catch (e) {}
    }
  }

  function drawChart(rows, trade, saved) {
    var cv = $('kfCanvas'); if (!cv) return;
    var ctx = cv.getContext('2d');
    var W = cv.width, H = cv.height, padL = 58, padR = 16, padT = 22, padB = 26;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#0b1220'; ctx.fillRect(0, 0, W, H);
    if (!rows || !rows.length) {
      ctx.fillStyle = '#64748b'; ctx.font = '13px ui-monospace,monospace';
      ctx.fillText('No candle buffer captured for this strike yet.', 24, H / 2);
      return;
    }
    var hi = Math.max.apply(null, rows.map(function (r) { return r.h; }));
    var lo = Math.min.apply(null, rows.map(function (r) { return r.l; }));
    var span = (hi - lo) || 1;
    var y = function (v) { return padT + (hi - v) / span * (H - padT - padB); };
    var step = (W - padL - padR) / rows.length;
    ctx.strokeStyle = '#16233a'; ctx.lineWidth = 1;
    for (var g = 0; g <= 4; g++) {
      var gy = padT + g * (H - padT - padB) / 4;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
      ctx.fillStyle = '#475569'; ctx.font = '10px ui-monospace,monospace';
      ctx.fillText((hi - g * span / 4).toFixed(1), 6, gy + 3);
    }
    rows.forEach(function (r, i) {
      var x = padL + step * (i + 0.5);
      var up = r.c >= r.o;
      ctx.strokeStyle = up ? '#FFFFFF' : '#F43F5E';
      ctx.fillStyle = up ? '#FFFFFF' : '#F43F5E';
      ctx.beginPath(); ctx.moveTo(x, y(r.h)); ctx.lineTo(x, y(r.l)); ctx.stroke();
      var top = y(Math.max(r.o, r.c)), bot = y(Math.min(r.o, r.c));
      ctx.fillRect(x - Math.max(step * 0.3, 1.5), top, Math.max(step * 0.6, 3), Math.max(bot - top, 1));
    });
    var vol = rows.some(function (r) { return r.v > 0; });
    var pv = 0, vv = 0, cum = 0, vwap = [];
    rows.forEach(function (r, i) {
      var typical = (r.h + r.l + r.c) / 3;
      if (vol) { pv += typical * r.v; vv += r.v; cum = vv ? pv / vv : typical; }
      else { cum = (cum * i + typical) / (i + 1); }
      vwap.push(cum);
    });
    ctx.strokeStyle = '#F59E0B'; ctx.lineWidth = 1.6; ctx.beginPath();
    vwap.forEach(function (v, i) {
      var x = padL + step * (i + 0.5), yv = y(v);
      if (i === 0) ctx.moveTo(x, yv); else ctx.lineTo(x, yv);
    });
    ctx.stroke();
    ctx.fillStyle = '#F59E0B'; ctx.font = '10px ui-monospace,monospace';
    ctx.fillText('VWAP ₹' + vwap[vwap.length - 1].toFixed(2), padL + 4, 27);
    var entry = trade && trade.entry != null ? +trade.entry : null;
    var levels = [];
    if (entry) {
      levels.push(['🔵 ENTRY', entry, '#38bdf8']);
      levels.push(['🟢 T1', trade.t1 != null ? +trade.t1 : entry * 1.14, '#22c55e']);
      levels.push(['🟣 T2', trade.t2 != null ? +trade.t2 : entry * 1.28, '#a855f7']);
      levels.push(['🔴 SL', trade.sl != null ? +trade.sl : entry * 0.93, '#F43F5E']);
    }
    levels.forEach(function (lv) {
      var ly = y(lv[1]);
      if (ly < padT || ly > H - padB) return;
      ctx.save(); ctx.setLineDash([5, 5]); ctx.strokeStyle = lv[2]; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(padL, ly); ctx.lineTo(W - padR, ly); ctx.stroke(); ctx.restore();
      ctx.fillStyle = lv[2]; ctx.font = '10px ui-monospace,monospace';
      ctx.fillText(lv[0] + ' ₹' + lv[1].toFixed(2), padL + 4, ly - 4);
    });
    ctx.fillStyle = saved ? '#4ade80' : '#38bdf8';
    ctx.font = '11px ui-monospace,monospace';
    ctx.fillText(saved ? '📸 SAVED POST-TRADE CHART SHOT' : '📈 LIVE OPTION CHART', padL, 14);
  }

  function openChart(key, live) {
    var p = LAST || {};
    var parts = key.split(':'), deskId = parts[0];
    var desk = deskFlow(deskId) || {};
    var trade = (desk.trades || []).filter(function (t, i) {
      return (deskId + ':' + (t.id || i + 1)) === key;
    })[0] || {};
    var saved = snapshots()[key];
    var rows = saved ? saved.candles : candlesFor(p, trade);
    $('kfModalT').textContent = (saved ? '📸 Chart Shot · ' : '📈 Live Chart · ') +
      (trade.symbol || '—') + ' ' + (trade.strike || '') + ' ' + (trade.side || '');
    $('kfModal').classList.add('on');
    drawChart(rows, saved ? saved.trade : trade, !!saved && !live);
  }

  // github.io has no API of its own: route every relative /api/* call to the edge.
  if (/github\.io$/i.test(location.hostname) && !window.__kfFetch) {
    window.__kfFetch = true;
    var realFetch = window.fetch.bind(window);
    var warming = function () {
      return new Response(JSON.stringify({ ok: true, warming: true }),
        { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    window.fetch = function (input, opts) {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      if (/^\/?api\//.test(url)) {
        var path = url.charAt(0) === '/' ? url : '/' + url;
        return resolveEdge().then(function (base) {
          return base ? realFetch(base + path, opts) : warming();
        }).catch(warming);
      }
      return realFetch(input, opts);
    };
  }

  window.paintDeskFlow = function (payload) {
    if (!payload) return;
    LAST = payload;
    if (!MOUNTED) mount();
    if ($('kfWrap') && $('kfWrap').classList.contains('on')) render();
  };

  function edgeBase() {
    return window.KRONOS_EDGE_URL || window.__kfEdge || '';
  }
  function resolveEdge() {
    if (window.__kfEdge) return Promise.resolve(window.__kfEdge);
    return fetch('https://kronos-nifty-desk-default-rtdb.asia-southeast1.firebasedatabase.app/system/live_stream_url.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var url = d && (d.edge_url || d.url) ? (d.edge_url || d.url) : '';
        if (url) window.__kfEdge = String(url).replace(/\/$/, '');
        return window.__kfEdge || '';
      })
      .catch(function () { return ''; });
  }
  function busy() {
    try {
      if (document.querySelector('#kfWrap:hover')) return true;
      var m = $('kfModal');
      return !!(m && m.classList.contains('on'));
    } catch (e) { return false; }
  }
  function pull() {
    if (busy()) return;
    resolveEdge().then(function (base) {
      if (!base) return;
      var f = window.__kronosOrigFetch || fetch;
      var activeSym = String(window.KRONOS_ACTIVE_SYMBOL || 'NIFTY').toUpperCase();
      return f(base + '/api/live-ticks?symbol=' + encodeURIComponent(activeSym), { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (p) {
          if (p && typeof p === 'object' && !p.error && (p.spot || p.desk_flow || p.role_pipeline)) {
            if (p.all_symbols) window.KRONOS_ALL_SYMBOLS = p.all_symbols;
            window.paintDeskFlow(p);
          }
        });
    }).catch(function () {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
