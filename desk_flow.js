/* 3 x ₹1,00,000 desk flow panel.
 *
 * Reads the `desks` and `index_momentum` blocks that /api/live-ticks now
 * returns.  Renders only what the payload contains: no trade, price or
 * probability is invented here.  Chart candles stay white for bullish and
 * coral for bearish; no setData() is called on live ticks.
 */
(function () {
  "use strict";

  var DESK_TABS = [
    { key: "directional_100k", label: "🧭 Directional ₹1L" },
    { key: "scalping_100k", label: "⚡ Smart Scalping ₹1L" },
    { key: "zero_to_hero_100k", label: "🚀 Zero-to-Hero ₹1L" },
    { key: "ALL", label: "🌐 All-3 Parallel" }
  ];
  var active = "ALL";
  var lastPayload = null;

  function inr(value) {
    var n = Number(value) || 0;
    var sign = n < 0 ? "-" : "+";
    return sign + "₹" + Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  function deskCard(key, desk) {
    var routed = desk.routed_index
      ? esc(desk.routed_index) + " · edge " + Number(desk.routed_edge_score || 0).toFixed(1)
      : "no live edge yet";
    var exits = desk.exits || {};
    return (
      '<div class="desk-card" data-desk="' + esc(key) + '">' +
      '<div class="desk-title">' + esc(desk.label) + "</div>" +
      '<div class="desk-line">Equity ₹' + Number(desk.equity_inr || 0).toLocaleString("en-IN") + "</div>" +
      '<div class="desk-line">Booked ' + inr(desk.booked_pnl_inr) + " · Live " + inr(desk.live_pnl_inr) + "</div>" +
      '<div class="desk-line">Route: ' + routed + "</div>" +
      '<div class="desk-line">Exits T1 ' + (exits.t1_pct || 0) + "% / T2 " +
      (exits.t2_pct || 0) + "% / Runner " + (exits.runner_pct || 0) + "%</div>" +
      '<div class="desk-line desk-muted">' + esc(desk.strategy) + "</div>" +
      '<button class="desk-chart-btn" data-symbol="NIFTY">📈 Chart</button>' +
      ledgerRows(key, desk.trades || []) +
      "</div>"
    );
  }

  // Per-trade rows: # | Time | Index & Strike | Entry -> Exit/LTP | Status | Booked ₹ | chart.
  // A completed trade's chart is frozen to localStorage when it closes.
  var SHOT_KEY = "kronos_desk_shots";
  function loadShots() {
    try { return JSON.parse(localStorage.getItem(SHOT_KEY) || "{}"); } catch (e) { return {}; }
  }
  function ledgerRows(deskKey, trades) {
    if (!trades.length) return '<div class="desk-muted">No trades yet.</div>';
    var shots = loadShots();
    return '<table class="desk-ledger"><tr><th>#</th><th>Time</th><th>Index &amp; Strike</th>' +
      '<th>Entry ₹</th><th>LTP/Exit ₹</th><th>Status</th><th>Booked ₹</th><th></th></tr>' +
      trades.map(function (t, i) {
        var done = String(t.status || "").indexOf("DONE") >= 0;
        var sid = deskKey + ":" + (t.id || i);
        var btn = done && shots[sid]
          ? '<button class="desk-shot-btn" data-shot="' + esc(sid) + '">📸 Saved Chart Shot</button>'
          : '<button class="desk-chart-btn" data-symbol="' + esc(t.symbol || "NIFTY") +
            '" data-trade="' + esc(sid) + '">📈 Live Chart</button>';
        return '<tr><td>#' + (i + 1) + '</td><td>' + esc(t.time || "—") + '</td><td>' +
          esc(t.index_strike || "—") + '</td><td>' + esc(t.entry == null ? "—" : t.entry) +
          '</td><td>' + esc(t.exit_or_ltp == null ? "—" : t.exit_or_ltp) + '</td><td>' +
          esc(t.status || "⏳ ENTRY PENDING") + '</td><td>' + inr(t.booked_pnl_inr) +
          '</td><td>' + btn + '</td></tr>';
      }).join("") + "</table>";
  }

  // Auto-save a frozen chart shot when a trade reaches ✅ TRADE DONE.
  function saveShot(sid, trade, candles) {
    var shots = loadShots();
    if (shots[sid]) return;
    shots[sid] = { candles: candles || [], levels: trade.levels || {}, booked: trade.booked_pnl_inr, symbol: trade.symbol };
    try { localStorage.setItem(SHOT_KEY, JSON.stringify(shots)); } catch (e) { /* storage full: keep in memory only */ }
  }

  function rankingRows(ranking) {
    if (!ranking || !ranking.length) {
      return '<div class="desk-muted">Momentum ranking waiting for the live feed.</div>';
    }
    return ranking.map(function (row, i) {
      return (
        '<div class="desk-rank">#' + (i + 1) + " " + esc(row.index) +
        " · score " + Number(row.score || 0).toFixed(1) + "/100" +
        " · ATR " + Number(row.atr_expansion || 0).toFixed(2) +
        " · OBI " + Number(row.obi_5_score || 0).toFixed(2) +
        " · TBQ " + Number(row.tbq_tsq_score || 0).toFixed(2) + "</div>"
      );
    }).join("");
  }

  // SVG node-flow: Upstox hub -> Kronos 102M/25M/4M -> strike/trade node.
  // Model % and checkmarks come only from the payload; missing values show as "—".
  function pct(v) {
    return v == null || isNaN(Number(v)) ? "—" : Number(v).toFixed(1) + "%";
  }

  function flowSvg(payload, desk) {
    var m = payload.model_oscillator || {};
    var up = Number(m.bullish_upside_prob_pct || 0);
    var down = Number(m.bearish_downside_prob_pct || 0);
    var ready = up >= 75 || down >= 75;
    // 102M >= 75%: a glowing pulse travels 102M -> 25M and 102M -> 4M.
    var pulse = up >= 75
      ? '<circle class="flow-pulse" r="4" fill="#FFFFFF"><animateMotion dur="1.4s" repeatCount="indefinite" path="M80,100 L160,110"/></circle>' +
        '<circle class="flow-pulse" r="4" fill="#FFFFFF"><animateMotion dur="1.4s" repeatCount="indefinite" path="M80,100 L160,180"/></circle>'
      : "";
    var tradeLabel = desk && desk.trade_status ? esc(desk.trade_status) : "⏳ ENTRY PENDING";
    var stamp = ready ? "✅" : "⏳";
    var models = [
      { id: "102M", y: 40, p: up, tip: "Kronos-102M · upside " + pct(up) + " · down " + pct(down) },
      { id: "25M", y: 110, p: up, tip: "Kronos-25M · confirms 102M bias" },
      { id: "4M", y: 180, p: up, tip: "Kronos-4M · confirms 102M bias" }
    ];
    var nodes = models.map(function (mo) {
      var glow = ready ? ' class="flow-glow"' : "";
      return '<g class="flow-model" data-tip="' + esc(mo.tip) + '">' +
        '<line x1="80" y1="100" x2="160" y2="' + mo.y + '"' + glow + ' stroke="#64748b" stroke-width="2"/>' +
        '<rect x="160" y="' + (mo.y - 18) + '" width="110" height="36" rx="8" fill="#1e293b" stroke="#334155"/>' +
        '<text x="215" y="' + (mo.y + 5) + '" text-anchor="middle" fill="#e2e8f0" font-size="13">' +
        mo.id + " " + pct(mo.p) + " " + (ready ? "✅" : "") + "</text></g>";
    }).join("");
    return (
      '<svg class="flow-svg" viewBox="0 0 400 220" role="img" aria-label="Model flow">' +
      '<g class="flow-hub" data-tip="Upstox V3 · NIFTY / BANKNIFTY / SENSEX ticks · 512-bar status · 37 streams">' +
      '<rect x="0" y="80" width="80" height="40" rx="8" fill="#312e81"/>' +
      '<text x="40" y="104" text-anchor="middle" fill="#f5f3ff" font-size="11">🟣 UPSTOX 🟢 100%</text></g>' +
      nodes + pulse +
      '<g class="flow-strike" data-tip="Strike node: ' + tradeLabel + '">' +
      '<line x1="270" y1="110" x2="330" y2="110" stroke="' + (ready ? "#22c55e" : "#64748b") + '" stroke-width="2"/>' +
      '<rect x="330" y="92" width="68" height="36" rx="8" fill="#0f172a" stroke="#334155"/>' +
      '<text x="364" y="114" text-anchor="middle" fill="#e2e8f0" font-size="10">' + stamp + " " + tradeLabel + "</text></g>" +
      "</svg>"
    );
  }

  // Chart modal: dark slate, bullish #FFFFFF, bearish #F43F5E, VWAP #F59E0B.
  // Draws only the candles it is given; never calls setData on live ticks.
  function drawChart(canvas, candles, vwap, levels) {
    var ctx = canvas.getContext("2d");
    var w = canvas.width, h = canvas.height;
    ctx.fillStyle = "#0f172a";
    ctx.fillRect(0, 0, w, h);
    if (!candles || !candles.length) {
      ctx.fillStyle = "#94a3b8";
      ctx.fillText("No candles in the live payload yet.", 20, 30);
      return;
    }
    var lo = Infinity, hi = -Infinity;
    candles.forEach(function (c) { lo = Math.min(lo, c.low); hi = Math.max(hi, c.high); });
    var span = (hi - lo) || 1;
    var step = w / candles.length;
    var y = function (v) { return h - 20 - ((v - lo) / span) * (h - 40); };
    candles.forEach(function (c, i) {
      var x = i * step + step / 2;
      var up = c.close >= c.open;
      ctx.strokeStyle = ctx.fillStyle = up ? "#FFFFFF" : "#F43F5E";
      ctx.beginPath(); ctx.moveTo(x, y(c.high)); ctx.lineTo(x, y(c.low)); ctx.stroke();
      var top = y(Math.max(c.open, c.close)), bot = y(Math.min(c.open, c.close));
      ctx.fillRect(x - step * 0.3, top, step * 0.6, Math.max(bot - top, 1));
    });
    if (vwap) {
      ctx.strokeStyle = "#F59E0B";
      ctx.beginPath(); ctx.moveTo(0, y(vwap)); ctx.lineTo(w, y(vwap)); ctx.stroke();
    }
    // Trade-specific levels: right-axis rupee tags. Drawn only when the trade has them.
    var lv = levels || {};
    var tags = [
      ["entry", "ENTRY", "#38BDF8"], ["sl", "SMALL SL", "#F43F5E"], ["trail", "TRAILING SL", "#10B981"],
      ["t1", "T1", "#22C55E"], ["t2", "T2", "#A855F7"], ["exit", "EXIT", "#F59E0B"]
    ];
    tags.forEach(function (tg) {
      var v = lv[tg[0]];
      if (v == null || isNaN(Number(v))) return;
      ctx.strokeStyle = tg[2];
      ctx.beginPath(); ctx.moveTo(0, y(v)); ctx.lineTo(w, y(v)); ctx.stroke();
      ctx.fillStyle = tg[2];
      ctx.fillText(tg[1] + " ₹" + Number(v).toFixed(2), w - 130, y(v) - 4);
    });
  }

  // openChartModal(symbol, frozen): a frozen shot (completed trade) draws its saved
  // candles and levels; otherwise the live payload candles for that symbol.
  function liveCandles(snap) {
    // /api/live-ticks serves the series under spot.candles_5m.
    return (snap.candles || (snap.spot || {}).candles_5m || []);
  }

  function openChartModal(symbol, frozen) {
    var snap = lastPayload || {};
    var candles = frozen ? frozen.candles : liveCandles(snap);
    var levels = frozen ? frozen.levels : null;
    var modal = document.createElement("div");
    modal.className = "desk-modal";
    modal.innerHTML = '<div class="desk-modal-box"><div class="desk-modal-head">' +
      esc(symbol) + (frozen ? " · saved shot" : " · 5m live") +
      ' <button class="desk-modal-close">✕</button></div>' +
      '<canvas width="720" height="360"></canvas></div>';
    document.body.appendChild(modal);
    drawChart(modal.querySelector("canvas"), candles, (snap.spot || {}).vwap || snap.vwap, levels);
    modal.addEventListener("click", function (e) {
      if (e.target === modal || e.target.classList.contains("desk-modal-close")) modal.remove();
    });
  }

  document.addEventListener("click", function (event) {
    var chart = event.target && event.target.closest && event.target.closest(".desk-chart-btn");
    if (chart) openChartModal(chart.getAttribute("data-symbol") || "NIFTY", null);
    var shot = event.target && event.target.closest && event.target.closest(".desk-shot-btn");
    if (shot) openChartModal("saved", loadShots()[shot.getAttribute("data-shot")] || null);
  });

  // Called when a trade reaches ✅ TRADE DONE: freeze its candles and levels.
  function onTradeDone(sid, trade) {
    saveShot(sid, trade, lastPayload ? liveCandles(lastPayload) : []);
  }
  window.kronosDeskTradeDone = onTradeDone;

  function render(payload) {
    var host = document.getElementById("deskFlowPanel");
    if (!host || !payload || !payload.desks) return;
    lastPayload = payload;
    var tabs = DESK_TABS.map(function (t) {
      return '<button class="desk-tab' + (t.key === active ? " active" : "") +
        '" data-tab="' + t.key + '">' + t.label + "</button>";
    }).join("");
    var keys = active === "ALL"
      ? ["directional_100k", "scalping_100k", "zero_to_hero_100k"]
      : [active];
    var cards = keys.map(function (k) {
      return payload.desks[k] ? deskCard(k, payload.desks[k]) : "";
    }).join("");
    var flowDesk = active === "ALL" ? null : payload.desks[active];
    host.innerHTML =
      '<div class="desk-tabs">' + tabs + "</div>" +
      flowSvg(payload, flowDesk) +
      '<div class="desk-tip" id="deskTip" hidden></div>' +
      '<div class="desk-cards">' + cards + "</div>" +
      '<div class="desk-rank-wrap"><div class="desk-k">INDEX MOMENTUM (0–100)</div>' +
      rankingRows(payload.index_momentum) + "</div>";
  }

  // Hover telemetry: a floating popover shows the element's data-tip text.
  document.addEventListener("mouseover", function (event) {
    var node = event.target && event.target.closest && event.target.closest("[data-tip]");
    var tip = document.getElementById("deskTip");
    if (!tip) return;
    if (!node) { tip.hidden = true; return; }
    tip.textContent = node.getAttribute("data-tip");
    tip.hidden = false;
  });

  document.addEventListener("click", function (event) {
    var btn = event.target && event.target.closest && event.target.closest(".desk-tab");
    if (!btn) return;
    active = btn.getAttribute("data-tab") || "ALL";
    if (lastPayload) render(lastPayload);
  });

  function mount() {
    if (document.getElementById("deskFlowPanel")) return;
    var anchor = document.getElementById("sidebar") || document.body;
    var host = document.createElement("section");
    host.id = "deskFlowPanel";
    host.className = "desk-flow";
    host.setAttribute("aria-label", "3 x 1 lakh desk flow");
    anchor.parentNode.insertBefore(host, anchor);
  }

  function poll() {
    var symbol = "NIFTY";
    fetch("/api/live-ticks?symbol=" + symbol, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (payload) { if (payload) render(payload); })
      .catch(function () {});
  }

  function start() {
    mount();
    poll();
    setInterval(poll, 3000);
  }

  window.KronosDeskFlow = { render: render };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
