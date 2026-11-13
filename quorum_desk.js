/* Always-visible 2-of-3 model cards, auto-regime badge and proof board. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const num = (value, digits) => {
    const out = Number(value);
    return Number.isFinite(out) ? out.toFixed(digits == null ? 2 : digits) : null;
  };
  const money = (value) => {
    const out = num(value, 0);
    return out == null ? '—' : `₹${Number(out).toLocaleString('en-IN')}`;
  };
  const price = (value) => {
    const out = num(value, 2);
    return out == null ? '—' : `₹${out}`;
  };
  const liveSymbol = () => {
    try { return typeof window.liveSymbol === 'function' ? window.liveSymbol() : 'NIFTY'; }
    catch (_) { return 'NIFTY'; }
  };
  const isOpen = (trade) => String((trade || {}).status || '').toUpperCase() === 'OPEN';

  function setModelCard(id, vote, fallbackTitle) {
    const el = $(id);
    if (!el) return;
    const v = vote || {};
    const confirmed = !!v.confirmed;
    const side = String(v.side || v.raw_side || '').toUpperCase();
    const confidence = num(v.confidence, 0);
    el.classList.toggle('confirmed', confirmed);
    el.innerHTML = `<div class="qhead"><span>${esc(v.short || fallbackTitle)}</span>` +
      `<span class="qstatus ${confirmed ? 'confirmed' : 'watching'}">${confirmed ? '🟢 CONFIRMED' : '🟡 WATCHING'}${confidence == null ? '' : ` · ${confidence}%`}</span></div>` +
      `<div class="qkicker">${esc(v.name || fallbackTitle)} · ${side === 'PE' || side === 'CE' ? side : 'NEUTRAL'}</div>` +
      `<div class="qconf">${confidence == null ? '—' : confidence}<small>%</small></div>` +
      `<div class="qreason" lang="te">${esc(v.reason_te || v.reason_en || 'Waiting for the next verified model cycle.')}</div>` +
      `<div class="qtrigger">${esc(v.trigger || 'Waiting for trigger price.')}</div>`;
  }

  function setDeskCard(id, html) {
    const el = $(id);
    if (!el) return;
    el.innerHTML = html;
  }

  function render(t) {
    t = t || {};
    if (!$('quorumDeck')) return;
    const symbol = liveSymbol();
    const quorumMap = t.quorum || {};
    const q = quorumMap[symbol] || quorumMap.NIFTY || {};
    const votes = Array.isArray(q.votes) ? q.votes : (Array.isArray(q.model_cards) ? q.model_cards : []);
    const byRole = {};
    votes.forEach((vote) => { byRole[vote.role] = vote; });
    setModelCard('qcard102', byRole.base, 'Kronos-base (102M)');
    setModelCard('qcard25', byRole.small, 'Kronos-small (25M)');
    setModelCard('qcard4', byRole.mini, 'Kronos-mini (4M)');

    // Compact 36px AI strip: real percentages, never a dash placeholder.
    const chip = (id, role) => {
      const el = $(id);
      if (!el) return;
      const vote = byRole[role] || {};
      const confidence = num(vote.confidence, 0);
      el.textContent = confidence == null ? '—' : `${confidence}%`;
      el.className = vote.confirmed ? 'pos' : 'warn';
      el.title = `${vote.name || role} · ${vote.side || 'NEUTRAL'} · ${vote.trigger || 'waiting'}`;
    };
    chip('proModel102', 'base');
    chip('proModel25', 'small');
    chip('proModel4', 'mini');
    if ($('proModelQuorum')) {
      $('proModelQuorum').textContent = `${q.quorum_count || '0/3'} ${q.approved ? 'CONFIRMED' : 'SCANNING'}`;
      $('proModelQuorum').className = q.approved ? 'pos' : 'warn';
    }

    const regimeMap = t.regime || {};
    const regime = regimeMap[symbol] || t.auto_regime || {};
    const badge = $('autoRegimeBadge');
    if (badge) {
      const ready = !!regime.mode;
      badge.textContent = ready
        ? `🔄 AUTO REGIME: ${regime.mode === 'TRENDY_DIRECTIONAL_HOLD' ? '🚀 TRENDY DIRECTIONAL MARKET' : '⚡ FAST SCALPING MARKET'} (Speed ${num(regime.speed_ticks_per_sec, 1) || '—'} ticks/s · ATR ${num(regime.body_atr_ratio, 1) || '—'}x)`
        : '🔄 AUTO REGIME: WAITING FOR LIVE SPEED';
      badge.className = 'auto-regime-badge ' + (!ready ? 'offline' : regime.mode === 'TRENDY_DIRECTIONAL_HOLD' ? 'trend' : 'scalp');
      badge.title = ready ? (regime.reason_en || '') : 'Waiting for ticks and 5m bars';
    }

    const tr = t.active_trade || {};
    const tradeOpen = isOpen(tr);
    const quorumText = `${q.quorum_count || '0/3'} Models Confirmed ${q.approved ? '✅' : '⏳'}`;
    const pnl = num(tr.realized_pnl_rs, 0) || 0;
    const pnlClass = pnl >= 0 ? 'pos' : 'neg';
    const activeLabel = tradeOpen ? '🟢 ACTIVE TRADE' : q.approved ? '🟢 READY' : '🟢 SCANNING';
    const regimeNote = regime.mode === 'TRENDY_DIRECTIONAL_HOLD'
      ? '40% T1 · 30% T2 · 30% runner · hold above 5m EMA9'
      : '70% T1 · stop to cost · 30% T2';
    setDeskCard('qcardScalp',
      `<div class="qhead"><span>⚡ SCALPING DESK (₹35K)</span><span class="qstatus ${q.approved ? 'confirmed' : 'live'}">${activeLabel}</span></div>` +
      `<div class="qmetric">${esc(quorumText)}</div>` +
      `<div class="qprices"><span>Live P&amp;L <b class="${pnlClass}">${pnl >= 0 ? '+' : ''}${money(pnl)}</b></span><span>Trades won <b>${esc((t.hud || {}).trades_won || (t.hud || {}).wins || 0)}</b></span></div>` +
      `<div class="qreason">${esc(regimeNote)}. ${esc(q.reason_te || 'High-accuracy 2-of-3 scanner is active.')}</div>`);

    // Levels are execution facts, not a live forecast.  While flat, do not
    // render or compute any entry/SL/T1/T2 from q.entry: those fields move with
    // the plan and were the source of the bracket chasing LTP on every tick.
    const entry = tradeOpen ? (tr.entry_price != null ? tr.entry_price : null) : null;
    const sl = tradeOpen ? (tr.current_trailing_sl != null ? tr.current_trailing_sl : null) : null;
    const t1 = tradeOpen ? (tr.target_1 != null ? tr.target_1 : null) : null;
    const t2 = tradeOpen ? (tr.target_2_p90 != null ? tr.target_2_p90 : (tr.target_2 != null ? tr.target_2 : null)) : null;
    const early = (t.intra_candle || {})[symbol] || {};
    const levels = tradeOpen
      ? `<div class="qprices"><span>ENTRY <b>${price(entry)}</b></span><span>SL <b>${price(sl)}</b></span><span>T1 <b>${price(t1)}</b></span><span>T2 <b>${price(t2)}</b></span></div>`
      : `<div class="qwait">⏳ Waiting for 2-of-3 Model Confirmation — Levels Lock at Execution</div>`;
    setDeskCard('qcardDirectional',
      `<div class="qhead"><span>🚀 DIRECTIONAL DESK (₹65K)</span><span class="qstatus ${tradeOpen || q.approved ? 'confirmed' : 'watching'}">${tradeOpen ? '🟢 EARLY ENTRY LIVE' : q.approved ? '🟢 READY' : '🟡 SCANNING'}</span></div>` +
      levels +
      `<div class="qmetric">${esc(q.side || '—')} · ${esc(early.ready ? `Inside 5m candle ${early.elapsed_sec || 0}s` : 'Waiting for intra-5m ignition')}</div>` +
      `<div class="qreason">${esc(early.reason_te || q.reason_te || 'Waiting for a 2-of-3 early entry.')}</div>`);

    let rows = Array.isArray(t.proof_ledger) ? t.proof_ledger.slice() : [];
    if (!rows.length && early.proof) rows = [early.proof];
    const body = $('proofBoardBody');
    const note = $('proofBoardNote');
    if (body) {
      if (!rows.length) {
        body.innerHTML = `<tr><td colspan="7" class="proof-empty">No trade taken yet — the proof ledger fills automatically on the first 2-of-3 intra-candle entry.</td></tr>`;
      } else {
        /* The human benchmark is the close of the entry's own 5m candle, not
           the 15:00 session cutoff: a 13:20 entry settles at 13:25. */
        const closeLabel = (row) => {
          const stamp = Number(row.human_close_expected_at || 0);
          if (!stamp) return '⏳ next 5m close';
          const ist = new Date((stamp + 19800) * 1000);
          return `⏳ ${String(ist.getUTCHours()).padStart(2, '0')}:${String(ist.getUTCMinutes()).padStart(2, '0')} 5m close`;
        };
        body.innerHTML = rows.slice().reverse().map((row) => {
          const human = row.human_5m_close_price == null
            ? closeLabel(row)
            : `<strong>${price(row.human_5m_close_price)}</strong>`;
          const edge = row.option_price_edge_inr == null
            ? `<span class="qmetric">${row.seconds_ahead_of_human == null ? 'saving…' : `+${Number(row.seconds_ahead_of_human).toFixed(0)}s early`}</span>`
            : `<strong class="${row.option_price_edge_inr >= 0 ? 'pos' : 'neg'}">${row.option_price_edge_inr >= 0 ? '+' : ''}${price(row.option_price_edge_inr)}</strong>`;
          const t1 = row.t1_booked ? `✅ ${price(row.t1_price)}` : '—';
          const t2 = row.t2_booked ? (row.runner_booked ? `✅ T2 ${price(row.t2_price)} · runner` : `✅ ${price(row.t2_price)}`) : '—';
          const net = Number(row.net_profit_rs) ? `<strong class="${Number(row.net_profit_rs) >= 0 ? 'pos' : 'neg'}">${Number(row.net_profit_rs) >= 0 ? '+' : ''}${money(row.net_profit_rs)}</strong>` : 'OPEN';
          return `<tr><td><strong>${esc(row.time || '—')}</strong><br>${esc(row.contract || `${row.symbol || symbol} ${row.strike || ''} ${row.side || ''}`)}</td>` +
            `<td><strong>${esc(row.quorum_count || '2/3')}</strong> · ${num(row.quorum_confidence, 0) || '—'}%</td>` +
            `<td><strong>${price(row.kronos_entry_price)}</strong><br><span class="qmetric">${num(row.elapsed_sec, 0) || '—'}s into candle</span></td>` +
            `<td>${human}<br>${edge}</td><td>${t1}</td><td>${t2}</td><td>${net}</td></tr>`;
        }).join('');
      }
    }
    if (note) {
      note.textContent = rows.length
        ? `${rows.length} intra-candle proof record${rows.length === 1 ? '' : 's'} · human candle-close prices fill automatically`
        : 'Waiting for the first intra-candle entry.';
    }
  }

  window.kronosQuorumTick = render;
  const current = window.KRONOS_LAST_TICK;
  if (current) render(current);
})();
