/* 100% Model Data & Brain Audit tab.
 * All values are supplied by /api/model-audit.  Missing checkpoints stay
 * visibly missing; this client never substitutes a probability or a price. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const num = (value, digits) => value == null || !Number.isFinite(Number(value))
    ? "—" : Number(value).toFixed(digits == null ? 2 : digits);
  let loading = false;
  let timer = null;

  function isVisible() {
    const host = $("brainAudit");
    return !!host && !host.hidden;
  }

  function chip(el, text, cls, title) {
    if (!el) return;
    el.textContent = text;
    el.className = "brain-chip " + (cls || "warn");
    if (title) el.title = title;
  }

  function renderMatrix(data) {
    const host = $("brainMatrix");
    if (!host) return;
    const rows = data.matrix || [];
    host.innerHTML = rows.length ? rows.map((row) => {
      const ok = !!row.verified;
      return `<div class="brain-verify ${ok ? "ok" : "no"}">
        <div class="bv-top"><span>${esc(row.index)}. ${esc(row.label)}</span><b>${esc(row.badge)}</b></div>
        <div class="bv-val">${esc(row.value)}</div>
        <div class="bv-age">feed age ${row.age_ms == null ? "—" : esc(row.age_ms) + " ms"} · ${ok ? "input accepted" : "blocked"}</div>
      </div>`;
    }).join("") : '<div class="brain-empty">No audit matrix returned.</div>';
    chip($("brainVerifiedChip"), `${data.verified_count || 0}/${data.total_checks || 8} VERIFIED`,
      data.verified_count === data.total_checks ? "ok" : "no",
      "Every check must be green before a model may fire");
  }

  function renderModels(data) {
    const host = $("brainModels");
    if (!host) return;
    const models = data.models || [];
    host.innerHTML = models.length ? models.map((model) => {
      const loaded = !!model.model_loaded;
      const probability = model.probability_pct == null ? "—" : `${num(model.probability_pct)}%`;
      const inference = model.inference_ms == null ? "— ms" : `${num(model.inference_ms, 2)} ms`;
      return `<div class="brain-model ${loaded ? "loaded" : "missing"}">
        <div class="bm-head"><b>${esc(model.name)}</b><span>${esc(model.parameters)} · ${esc(model.interval)}</span></div>
        <div class="bm-shape">IN [1,512,6] · ${esc(model.volume_source)}</div>
        <div class="bm-prob">${probability}</div>
        <div class="bm-state">${esc(model.state)}</div>
        <div class="bm-reason">${loaded ? "INF " + esc(inference) + " · " : ""}${esc(model.reason)}</div>
      </div>`;
    }).join("") : '<div class="brain-empty">No model cards returned.</div>';
    const decision = data.decision || {};
    const hostDecision = $("brainDecision");
    if (hostDecision) {
      const state = decision.state || "WAITING_FOR_MODEL_OUTPUT";
      hostDecision.innerHTML = `<b>${esc(state)}</b> · ${esc(decision.loaded_models || 0)}/${esc(decision.total_models || 3)} checkpoints loaded` +
        (decision.reason ? `<br><span class="warn">${esc(decision.reason)}</span>` : "");
    }
  }

  function renderLog(data) {
    const host = $("brainLog");
    if (!host) return;
    const rows = data.log || [];
    if (!rows.length) {
      host.innerHTML = '<div class="brain-empty">No sanitizer or brain events yet.</div>';
      return;
    }
    const nearBottom = host.scrollTop + host.clientHeight >= host.scrollHeight - 40;
    host.innerHTML = rows.slice(-60).map((row) => {
      const stamp = String(row.ts || "").replace("T", " ").slice(11, 23);
      const level = String(row.level || "INFO").toUpperCase();
      return `<div class="brain-log-row ${esc(level)}">
        <span>${esc(stamp || "—")}</span><b>${esc(level)}</b><p>${esc(row.message)}</p>
      </div>`;
    }).join("");
    if (nearBottom) host.scrollTop = host.scrollHeight;
  }

  function render(data) {
    const state = data.feed_status || {};
    const live = state.status === "FEED_LIVE";
    chip($("brainFeedChip"), live ? "FEED LIVE" : "FEED OFFLINE / STALE",
      live ? "ok" : "no", `${state.reason || ""} · age ${state.age_ms == null ? "—" : state.age_ms} ms`);
    renderMatrix(data);
    renderModels(data);
    renderLog(data);
  }

  function renderError(message) {
    chip($("brainFeedChip"), "AUDIT UNAVAILABLE", "no", message);
    chip($("brainVerifiedChip"), "0/8", "no", message);
    const matrix = $("brainMatrix");
    if (matrix) matrix.innerHTML = `<div class="brain-empty">${esc(message)}</div>`;
    const models = $("brainModels");
    if (models) models.innerHTML = '<div class="brain-empty">No model cards — API unavailable.</div>';
    const log = $("brainLog");
    if (log) log.innerHTML = `<div class="brain-empty">${esc(message)}</div>`;
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch("/api/model-audit", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      renderError("100% audit API unavailable: " + error.message);
    } finally {
      loading = false;
    }
  }

  function boot() {
    window.loadModelAudit = load;
    const refresh = $("brainRefresh");
    if (refresh) refresh.addEventListener("click", load);
    if (timer) clearInterval(timer);
    timer = setInterval(() => { if (isVisible() && !document.hidden) load(); }, 1800);
    // Prime the panel even while it is docked/hidden: opening the audit modal
    // must never show an empty "Loading live audit…" card.
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
