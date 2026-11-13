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
      "</div>"
    );
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
    host.innerHTML =
      '<div class="desk-tabs">' + tabs + "</div>" +
      '<div class="desk-cards">' + cards + "</div>" +
      '<div class="desk-rank-wrap"><div class="desk-k">INDEX MOMENTUM (0–100)</div>' +
      rankingRows(payload.index_momentum) + "</div>";
  }

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
