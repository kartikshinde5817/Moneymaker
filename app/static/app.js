/* Money Maker dashboard.
 *
 * The Outbox is the default screen, not the Dashboard: the daily job is
 * approving or rejecting drafts, and every draft is shown next to the evidence
 * it was written from, so a claim can be checked before it is sent.
 */

const $ = s => document.querySelector(s);
const el = (h) => { const d = document.createElement('div'); d.innerHTML = h.trim(); return d.firstElementChild; };
const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let META = {}, VIEW = 'outbox';
const FILTERS = { service: '', source: '', country: '', intent: '', status: '', has_email: '', min_score: 0, q: '', date_from: '', date_to: '', sort: 'score', page: 1 };

const SERVICE_LABEL = {
  web_development: 'Web Development', computer_vision: 'Computer Vision',
  ai_agent: 'AI Agents', ai_automation: 'AI Automation',
};
const SOURCE_LABEL = {
  tender: 'Public tender', google_places: 'Google Places', companies_house: 'Companies House',
  job_board: 'Job ad', hacker_news: 'Hacker News', osm: 'OpenStreetMap', website: 'Website', legacy: 'Imported',
  serp: 'Web search', serper: 'Web search', yelp: 'Yelp', github: 'GitHub',
  contracts_finder: 'Contracts Finder', find_a_tender: 'Find a Tender',
  sam_gov: 'SAM.gov', adzuna: 'Adzuna', openstreetmap: 'OpenStreetMap',
};

function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast on' + (kind ? ' ' + kind : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = 'toast', 3800);
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let m = `Error ${r.status}`;
    try { m = (await r.json()).detail || m; } catch (e) { /* non-JSON error */ }
    throw new Error(m);
  }
  return (r.headers.get('content-type') || '').includes('json') ? r.json() : r.text();
}
const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? null : JSON.stringify(body),
});
const patch = (p, body) => api(p, {
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

const scoreClass = s => s >= 70 ? 'high' : s >= 40 ? 'med' : 'low';
const fmtDate = s => !s ? '—' : new Date(s).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
const fmtTime = s => !s ? '—' : new Date(s).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

/* ---------------------------------------------------------------- shell */

const NAV = [
  { key: 'workflow', label: 'Agent', icon: 'M5 3v4a2 2 0 002 2h3m4 0h3a2 2 0 002-2V3M5 21v-4a2 2 0 012-2h10a2 2 0 012 2v4M12 9v6' },
  { key: 'outbox', label: 'Outbox', icon: 'M3 8l9 6 9-6M3 8v9a1 1 0 001 1h16a1 1 0 001-1V8l-9-6-9 6z' },
  { key: 'dashboard', label: 'Dashboard', icon: 'M4 20V10M10 20V4M16 20v-7M22 20H2' },
  { key: 'linkedin', label: 'LinkedIn', icon: 'M16 8a6 6 0 016 6v7h-4v-7a2 2 0 00-4 0v7h-4v-11h4v2a4 4 0 014-2zM2 9h4v12H2zM4 2a2 2 0 110 4 2 2 0 010-4z' },
  { key: 'whatsapp', label: 'WhatsApp', icon: 'M21 11.5a8.4 8.4 0 01-12 7.6L3 21l1.9-5.7A8.4 8.4 0 1121 11.5z' },
  { key: 'leads', label: 'Leads', icon: 'M3 6h18M3 12h18M3 18h12' },
  { key: 'campaigns', label: 'Campaigns', icon: 'M12 2v4m0 12v4M2 12h4m12 0h4M6 6l3 3m6 6l3 3m0-12l-3 3m-6 6l-3 3' },
  { key: 'settings', label: 'Settings', icon: 'M12 15a3 3 0 100-6 3 3 0 000 6zM3 12h3m12 0h3M12 3v3m0 12v3' },
];

function renderNav(pending) {
  $('#nav').innerHTML = NAV.map(n => `
    <button data-v="${n.key}" class="${VIEW === n.key ? 'on' : ''}">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="${n.icon}"/></svg>
      ${n.label}${n.key === 'outbox' && pending ? `<span class="pip">${pending}</span>` : ''}
    </button>`).join('');
  $('#nav').querySelectorAll('button').forEach(b => b.onclick = () => go(b.dataset.v));
}

function renderFoot() {
  const c = (ok, on, off) => `<span class="chip ${ok ? 'ok' : 'off'}"><span class="dot"></span>${ok ? on : off}</span>`;
  $('#foot').innerHTML =
    c(META.ai_ready, 'AI writer on', 'AI writer off') +
    c(META.email_ready, 'Email ready', 'No SMTP') +
    c(META.scheduler_enabled, 'Scheduler on', 'Scheduler off') +
    c(META.telegram_ready, 'Telegram on', 'No Telegram') +
    c(META.inbox_ready, 'Reply watch on', 'No IMAP') +
    `<div style="margin-top:8px">Email only to the countries listed in Settings. All others are manual outreach only.</div>`;
}

const VIEWS = {};
async function go(v) {
  VIEW = v;
  if (location.hash !== '#' + v) history.replaceState(null, '', '#' + v);
  renderNav();
  $('#view').innerHTML = '<div class="empty">Loading…</div>';
  try { await VIEWS[v](); } catch (e) { $('#view').innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
  renderNav(window._pending);
}

/* ---------------------------------------------------------- workflow view */

const NODE_ICONS = {
  trigger: 'M13 2L4.09 12.11a1 1 0 00.76 1.64H11l-1 8 8.91-10.11a1 1 0 00-.76-1.64H12l1-8z',
  search:  'M11 19a8 8 0 100-16 8 8 0 000 16zM21 21l-4.35-4.35',
  merge:   'M7 3v6a4 4 0 004 4h2a4 4 0 014 4v4M7 21V9M17 3v4',
  globe:   'M12 21a9 9 0 100-18 9 9 0 000 18zM3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 010 18a15 15 0 010-18z',
  doc:     'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8l-6-6zM14 2v6h6M8 13h8M8 17h5',
  brain:   'M9.5 2A2.5 2.5 0 007 4.5v.5a2.5 2.5 0 000 5v.5a2.5 2.5 0 002.5 2.5H12V2H9.5zM14.5 2A2.5 2.5 0 0117 4.5v.5a2.5 2.5 0 010 5v.5a2.5 2.5 0 01-2.5 2.5H12V2h2.5zM12 13v9',
  gauge:   'M12 21a9 9 0 110-18 9 9 0 010 18zM12 12l4-4',
  pen:     'M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.586 7.586',
  inbox:   'M22 12h-6l-2 3h-4l-2-3H2M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z',
};

let _wfTimer = null;

VIEWS.workflow = async function () {
  clearTimeout(_wfTimer);
  const [wf, camps] = await Promise.all([api('/api/workflow'), api('/api/campaigns')]);
  const running = wf.run && wf.run.status === 'running';

  $('#view').innerHTML = `
    <div class="head">
      <div><h2>Agent pipeline</h2>
      <p>Nine stages, run in order for every prospect. What you see below is the real
         run — each number is work that actually happened, and every drop-off is a
         business the system decided <em>not</em> to contact.</p></div>
      <div class="row">
        <select id="wfCamp" class="btn" style="padding:7px 10px">
          ${camps.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}
        </select>
        <button class="btn primary" id="wfRun" ${running ? 'disabled' : ''}>
          ${running ? '<span class="spin"></span> Running' : '▶ Run agent'}</button>
      </div>
    </div>
    <div id="wfCanvas"></div>
    <div class="grid2" style="margin-top:18px">
      <div class="card"><h3>The gates that protect you</h3>
        <p class="hint">Each of these can stop a prospect. They are enforced in code,
           not by prompt instructions.</p>
        ${[
          ['Research', 'Ignores websites that are not provably theirs — a portal or a Deliveroo page never becomes "your website".'],
          ['Evidence', 'Zero signals means no lead. The writer physically cannot be reached without a quotable fact.'],
          ['Qualify', 'Drops competitors — a rival web agency is worse than no email.'],
          ['Scoring', 'Below the campaign threshold, nothing is drafted.'],
          ['Personalize', 'Sees only signal rows, must cite them, and cannot claim they asked unless they did.'],
          ['Outbox', 'Country policy, suppression list and the 90-day cooling-off all checked before it leaves.'],
        ].map(([k, v]) => `<div class="row" style="align-items:flex-start;gap:8px;padding:6px 0;border-top:1px solid var(--line);font-size:12.5px">
            <span class="tag" style="flex-shrink:0">${k}</span><span class="muted" style="flex:1;min-width:0">${v}</span></div>`).join('')}
      </div>
      <div class="card"><h3>Where the drop-offs happen</h3>
        <div id="wfDrops"></div></div>
    </div>`;

  $('#wfRun').onclick = async () => {
    const id = $('#wfCamp').value;
    try {
      await post(`/api/campaigns/${id}/run`);
      toast('Agent started');
      paintWorkflow();
    } catch (e) { toast(e.message, 'bad'); }
  };
  paintWorkflow(wf);
};

async function paintWorkflow(pre) {
  const canvas = $('#wfCanvas');
  if (!canvas) return;
  let wf = pre;
  if (!wf) { try { wf = await api('/api/workflow'); } catch (e) { return; } }

  const run = wf.run, nodes = wf.nodes || [];
  const running = run && run.status === 'running';
  const activeKey = wf.active;
  const activeIdx = nodes.findIndex(n => n.key === activeKey);

  const statusChip = !run
    ? '<span class="wf-live idle"><span class="bl"></span>NEVER RUN</span>'
    : running ? '<span class="wf-live run"><span class="bl"></span>RUNNING</span>'
    : run.status === 'failed' ? '<span class="wf-live fail"><span class="bl"></span>FAILED</span>'
    : '<span class="wf-live done"><span class="bl"></span>COMPLETED</span>';

  const nodeHtml = nodes.map((n, i) => {
    const icon = NODE_ICONS[n.icon] || NODE_ICONS.doc;
    const edgeCls = i === 0 ? '' : (activeIdx === i && running) ? 'on'
      : (activeIdx === -1 || i <= activeIdx) && nodes[i - 1] && nodes[i - 1].passed > 0 ? 'past' : '';
    const edge = i === 0 ? '' : `<div class="edge ${edgeCls}"></div>`;
    return `${edge}
      <div class="node ${esc(n.status)}${running && n.key === activeKey ? ' running' : ''}" title="${esc(n.detail || n.note)}">
        ${n.engine === 'llm' ? '<span class="eng llm">LLM</span>'
          : n.engine === 'rules' ? '<span class="eng">RULES</span>'
          : n.engine === 'http' ? '<span class="eng">HTTP</span>'
          : n.engine === 'api' ? '<span class="eng">API</span>' : ''}
        <div class="ntop">
          <span class="nico"><svg width="14" height="14" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="${icon}"/></svg></span>
          <span class="nname">${esc(n.label)}</span>
        </div>
        <div class="nnote">${esc(n.note)}</div>
        <div class="nfoot">
          <span class="cnt ${n.passed ? '' : 'zero'}">${n.passed}</span>
          ${n.dropped ? `<span class="drop" title="rejected here">✕ ${n.dropped}</span>`
            : n.ms ? `<span class="ms">${n.ms > 1000 ? (n.ms / 1000).toFixed(1) + 's' : n.ms + 'ms'}</span>` : ''}
        </div>
      </div>`;
  }).join('');

  const active = nodes.find(n => n.key === activeKey)
    || [...nodes].reverse().find(n => n.detail || n.drop_detail);

  canvas.innerHTML = `
    <div class="wf">
      <div class="wf-head">
        ${statusChip}
        <div><h3>${run ? esc(run.campaign) : 'No run yet'}</h3>
          <div class="wf-sub">${run
            ? `${esc(run.trigger)} · started ${fmtTime(run.started_at)}${run.stats && run.stats.ai_cost_usd ? ' · $' + run.stats.ai_cost_usd.toFixed(3) + ' AI' : ''}`
            : 'Pick a campaign above and press Run agent.'}</div></div>
      </div>

      <div class="wf-flow">${nodeHtml}</div>

      <div class="wf-now">
        ${running ? '<span class="spin" style="border-top-color:#22D3EE;border-color:rgba(34,211,238,.3)"></span>' : ''}
        <div style="min-width:0">
          <div><b>${active ? esc(active.label) : 'Idle'}</b>${
            run && run.current_item ? ' — ' + esc(run.current_item) : running ? '' : run ? ' — finished' : ''}</div>
          ${active && (active.detail || active.drop_detail) ? `<div class="wf-detail">${esc(active.detail || active.drop_detail)}</div>` : ''}
        </div>
      </div>

      <div class="wf-term" id="wfTerm">${(wf.log || []).map(colorLog).join('\n') || 'Waiting for a run…'}</div>

      <div class="legend">
        <span><i style="background:#06B6D4"></i>Running</span>
        <span><i style="background:#2F6B45"></i>Completed</span>
        <span><i style="background:#F87171"></i>✕ Rejected here</span>
        <span><i style="background:#FBBF24"></i>LLM stage (costs money)</span>
        <span><i style="background:#56697A"></i>Idle / skipped</span>
      </div>
    </div>`;

  const term = $('#wfTerm');
  if (term) term.scrollTop = term.scrollHeight;
  renderDrops(nodes);

  const btn = $('#wfRun');
  if (btn) {
    btn.disabled = !!running;
    btn.innerHTML = running ? '<span class="spin"></span> Running' : '▶ Run agent';
  }

  clearTimeout(_wfTimer);
  if (running && VIEW === 'workflow') _wfTimer = setTimeout(() => paintWorkflow(), 1200);
}

function colorLog(line) {
  const e = esc(line);
  if (/\bx\s|error|FAILED|refus/i.test(line)) return `<span class="no">${e}</span>`;
  if (/^\s*\d\d:\d\d:\d\d\s+\s*\+/.test(line) || /scored|drafted/i.test(line)) return `<span class="ok">${e}</span>`;
  if (/starting|Discovery finished|Done\./i.test(line)) return `<span class="hl">${e}</span>`;
  return e;
}

function renderDrops(nodes) {
  const box = $('#wfDrops');
  if (!box) return;
  const drops = nodes.filter(n => n.dropped > 0);
  const total = drops.reduce((a, n) => a + n.dropped, 0);
  if (!total) { box.innerHTML = '<p class="hint" style="margin:0">Nothing has been rejected yet.</p>'; return; }
  const max = Math.max(...drops.map(n => n.dropped));
  box.innerHTML = drops.map(n => `
    <div style="margin-bottom:9px">
      <div class="row" style="justify-content:space-between;font-size:12.5px;margin-bottom:3px">
        <span>${esc(n.label)}</span><b class="mono">${n.dropped}</b></div>
      <div class="bar"><i style="width:${Math.round(n.dropped / max * 100)}%;background:var(--warn)"></i></div>
      ${n.drop_detail ? `<div class="muted" style="font-size:11px;margin-top:3px">Last: ${esc(n.drop_detail)}</div>` : ''}
    </div>`).join('');
}

/* ------------------------------------------------------------- dashboard */

VIEWS.dashboard = async function () {
  const [s, byService, bySource, byIntent, runs] = await Promise.all([
    api('/api/stats/summary'), api('/api/stats/breakdown?by=service'),
    api('/api/stats/breakdown?by=source'), api('/api/stats/breakdown?by=intent'),
    api('/api/runs'),
  ]);
  window._pending = s.pending_approval;

  const tile = (k, v, sub, cls = '') => `<div class="tile ${cls}"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;
  const bars = (rows, label) => {
    const max = Math.max(1, ...rows.map(r => r.count));
    return rows.length ? rows.map(r => `
      <div style="margin-bottom:9px">
        <div class="row" style="justify-content:space-between;font-size:12.5px;margin-bottom:3px">
          <span>${esc(label(r.key))}</span><b class="mono">${r.count}</b></div>
        <div class="bar"><i style="width:${Math.round(r.count / max * 100)}%"></i></div>
      </div>`).join('') : '<p class="muted" style="font-size:12.5px">No data yet.</p>';
  };

  $('#view').innerHTML = `
    <div class="head"><div>
      <h2>Dashboard</h2>
      <p>Every number here counts real records. Leads only exist where the system found
         verifiable evidence of a reason to make contact.</p>
    </div></div>

    <div class="tiles">
      ${tile('Total leads', s.total_leads, `${s.companies} companies`)}
      ${tile('New today', s.new_today)}
      ${tile('High intent', s.high_intent, 'score 70+', 'hot')}
      ${tile('Awaiting approval', s.pending_approval, 'in Outbox', s.pending_approval ? 'act' : '')}
      ${tile('Emails sent', s.emails_sent, `${s.sent_today} today of ${s.daily_cap} cap`)}
      ${tile('Replies', s.replies)}
      ${tile('Interested', s.interested, 'replied yes', s.interested ? 'hot' : '')}
      ${tile('WhatsApp queued', s.whatsapp_pending)}
      ${tile('Unsubscribed', s.unsubscribed)}
      ${tile('AI spend', '$' + (s.ai_cost_usd || 0).toFixed(2), 'all time')}
    </div>

    <div class="grid2">
      <div class="card"><h3>Leads by service</h3><div style="margin-top:12px">${bars(byService, k => SERVICE_LABEL[k] || k)}</div></div>
      <div class="card"><h3>Leads by source</h3><div style="margin-top:12px">${bars(bySource, k => SOURCE_LABEL[k] || k)}</div></div>
    </div>
    <div class="grid2">
      <div class="card"><h3>Intent</h3><div style="margin-top:12px">${bars(byIntent, k => k)}</div></div>
      <div class="card"><h3>Recent campaign runs</h3>
        ${runs.length ? `<div style="margin-top:8px">${runs.slice(0, 8).map(r => `
          <div class="row" style="padding:7px 0;border-bottom:1px solid var(--line);font-size:12.5px">
            <span class="pill ${r.status === 'completed' ? 'sent' : r.status === 'failed' ? 'rejected' : 'queued'}">${r.status}</span>
            <span>${esc(r.campaign)}</span>
            <span class="sp"></span>
            <span class="muted mono">${(r.stats && r.stats.leads_created) || 0} leads</span>
            <button class="btn sm ghost" onclick="showRun(${r.id})">log</button>
          </div>`).join('')}</div>` : '<p class="muted" style="font-size:12.5px">No runs yet. Start one from Campaigns.</p>'}
      </div>
    </div>`;
};

/* ---------------------------------------------------------------- outbox */

VIEWS.outbox = async function () {
  const msgs = await api('/api/outbox?status=pending_approval');
  window._pending = msgs.length;

  const warn = [];
  if (!META.email_ready) warn.push('SMTP is not configured, so nothing can actually be sent yet. Add SMTP settings to your .env file.');
  if (!META.sender_configured) warn.push('SENDER_EMAIL and COMPANY_POSTAL_ADDRESS must both be set before sending — the postal address is required by US CAN-SPAM law.');

  $('#view').innerHTML = `
    <div class="head">
      <div><h2>Outbox</h2>
      <p>Each draft sits beside the evidence it was written from. Check the claim is
         true, then approve. Highlighted evidence is what the email actually cites.</p></div>
      <div class="row">
        <button class="btn" onclick="go('outbox')">Refresh</button>
        ${msgs.length ? `<button class="btn primary" id="sendAll">Approve &amp; send all (${msgs.length})</button>` : ''}
      </div>
    </div>
    ${warn.map(w => `<div class="banner">${esc(w)}</div>`).join('')}
    <div id="obList">${msgs.length ? msgs.map(obCard).join('') : `
      <div class="empty"><h3>Nothing waiting for approval</h3>
      <p>Run a campaign to generate drafts, or draft one from a lead.</p></div>`}</div>`;

  if ($('#sendAll')) $('#sendAll').onclick = async () => {
    if (!confirm(`Approve and send all ${msgs.length} emails?\n\nThey will go out immediately.`)) return;
    let ok = 0, fail = 0;
    for (const m of msgs) {
      try { const r = await post(`/api/outbox/${m.id}/approve?send_now=true`); r.ok ? ok++ : fail++; }
      catch (e) { fail++; }
    }
    toast(`Sent ${ok}${fail ? `, ${fail} failed` : ''}`, fail ? 'bad' : 'good');
    go('outbox');
  };
};

function obCard(m) {
  const ev = m.evidence.map(e => `
    <div class="ev ${e.cited ? 'cited' : ''}">
      <div class="t">${esc(e.type.replace(/_/g, ' '))}
        <span class="badge ${e.evidence_class === 'explicit_request' ? 'exp' : 'inf'}">
          ${e.evidence_class === 'explicit_request' ? 'THEY ASKED' : 'WE OBSERVED'}</span>
        ${e.cited ? '<span class="badge cit">CITED</span>' : ''}
      </div>
      <div class="q">${esc(e.quote)}</div>
    </div>`).join('') || '<p class="muted">No evidence — this should not have been drafted.</p>';

  return `<div class="card" id="ob${m.id}">
    <div class="row" style="margin-bottom:12px">
      <span class="score ${scoreClass(m.score)}">${m.score}</span>
      <div><div style="font-weight:600">${esc(m.company)}</div>
        <div class="muted" style="font-size:12px">${esc(m.to_email)} · ${esc(m.country)} · ${esc(SERVICE_LABEL[m.service_category] || m.service_category)}</div></div>
      <span class="sp"></span>
      ${m.website ? `<a class="btn sm" href="${esc(m.website)}" target="_blank" rel="noopener noreferrer">Their site ↗</a>` : ''}
      <button class="btn sm" onclick="openLead(${m.lead_id})">Full lead</button>
    </div>
    <div class="ob">
      <div>
        <div class="inp" style="margin-bottom:10px"><label>Subject</label>
          <input id="sub${m.id}" value="${esc(m.subject)}"></div>
        <div class="inp"><label>Body</label><textarea id="bod${m.id}">${esc(m.body)}</textarea></div>
        <div class="row" style="margin-top:12px">
          <button class="btn good" onclick="approve(${m.id})">Approve &amp; send</button>
          <button class="btn" onclick="saveDraft(${m.id})">Save edit</button>
          <button class="btn danger" onclick="rejectMsg(${m.id})">Reject</button>
        </div>
      </div>
      <div>
        <div class="muted" style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:8px">
          Evidence on file</div>
        ${ev}
      </div>
    </div></div>`;
}

async function saveDraft(id) {
  try {
    await patch(`/api/outbox/${id}`, { subject: $('#sub' + id).value, body: $('#bod' + id).value });
    toast('Saved', 'good');
  } catch (e) { toast(e.message, 'bad'); }
}
async function approve(id) {
  try {
    await patch(`/api/outbox/${id}`, { subject: $('#sub' + id).value, body: $('#bod' + id).value });
    const r = await post(`/api/outbox/${id}/approve?send_now=true`);
    if (r.ok) { toast('Sent', 'good'); const c = $('#ob' + id); if (c) c.remove(); }
    else toast(r.detail || 'Could not send', 'bad');
  } catch (e) { toast(e.message, 'bad'); }
}
async function rejectMsg(id) {
  try { await post(`/api/outbox/${id}/reject`); toast('Rejected'); const c = $('#ob' + id); if (c) c.remove(); }
  catch (e) { toast(e.message, 'bad'); }
}

/* -------------------------------------------------------------- linkedin */

let LI_ONLY = true;

VIEWS.linkedin = async function () {
  const d = await api('/api/linkedin?only_unemailable=' + LI_ONLY);
  const s = d.stats;
  $('#view').innerHTML = `
    <div class="head">
      <div><h2>LinkedIn outreach</h2>
      <p>Opened by you, in your own browser. The app never logs into LinkedIn or
         fetches anything from it.</p></div>
      <label class="row" style="font-size:13px;gap:7px">
        <input type="checkbox" id="liOnly" ${LI_ONLY ? 'checked' : ''}>
        Only where email isn't an option</label>
    </div>

    <div class="banner">
      <b>Why this is a manual lane.</b> LinkedIn publishes no API for post or
      profile search, and scraping public pages breaches their User Agreement —
      they enforce it with bans and civil claims. The links below come from two
      safe places: the LinkedIn URL a company publishes <em>on its own website</em>,
      or a search you run yourself. Nothing is fetched from LinkedIn by this app.
    </div>

    <div class="tiles">
      <div class="tile"><div class="k">Published a LinkedIn URL</div><div class="v">${s.with_linkedin_url}</div></div>
      <div class="tile"><div class="k">Email unlawful there</div><div class="v">${s.email_blocked_country}</div>
        <div class="s">LinkedIn is the only route</div></div>
      <div class="tile"><div class="k">No email found</div><div class="v">${s.no_email_address}</div></div>
    </div>

    ${d.leads.length ? d.leads.map(l => `
      <div class="card">
        <div class="row" style="margin-bottom:9px">
          <span class="score ${scoreClass(l.score)}">${l.score}</span>
          <div><div style="font-weight:600">${esc(l.company)}</div>
            <div class="muted" style="font-size:12px">${esc(l.city || '')} ${esc(l.country)}
              · ${esc(SERVICE_LABEL[l.service_category] || l.service_category)}
              · ${esc(l.why_linkedin)}</div></div>
          <span class="sp"></span>
          ${l.linkedin_url
            ? `<a class="btn primary" href="${esc(l.linkedin_url)}" target="_blank" rel="noopener noreferrer">Their page ↗</a>`
            : `<a class="btn" href="${esc(l.search_url)}" target="_blank" rel="noopener noreferrer">Search LinkedIn ↗</a>`}
          <button class="btn sm" onclick="openLead(${l.lead_id})">Evidence</button>
        </div>
        <div style="font-size:12.5px"><b>Angle:</b> ${esc(l.angle)}</div>
        ${l.evidence ? `<div class="ev" style="margin-top:8px"><div class="q">${esc(l.evidence)}</div></div>` : ''}
      </div>`).join('') : `
      <div class="empty"><h3>Nothing queued</h3>
      <p>Leads appear here once a campaign has found companies with evidence.</p></div>`}`;

  $('#liOnly').onchange = e => { LI_ONLY = e.target.checked; VIEWS.linkedin(); };
};

/* -------------------------------------------------------------- whatsapp */

VIEWS.whatsapp = async function () {
  const msgs = await api('/api/whatsapp?status=pending');
  $('#view').innerHTML = `
    <div class="head">
      <div><h2>WhatsApp queue</h2>
      <p>One click opens WhatsApp with the message already typed — you press send.
         It is not sent automatically, and that is deliberate: see below.</p></div>
      <button class="btn" onclick="go('whatsapp')">Refresh</button>
    </div>

    <div class="banner">
      <b>Why this needs one click from you.</b> WhatsApp's official API only delivers
      pre-approved templates to people who already opted in, so a scraped number has
      no compliant automated route. Tools that drive WhatsApp Web get the number banned,
      usually within days. A human pressing send keeps the number alive and keeps you
      on the right side of the rules.
    </div>

    ${msgs.length ? msgs.map(m => `
      <div class="card" id="wa${m.id}">
        <div class="row" style="margin-bottom:10px">
          <span class="score ${scoreClass(m.score)}">${m.score}</span>
          <div><div style="font-weight:600">${esc(m.company)}</div>
            <div class="muted" style="font-size:12px">${esc(m.phone)} · ${esc(m.country)}
              · ${esc(SERVICE_LABEL[m.service_category] || m.service_category)}</div></div>
          <span class="sp"></span>
          <button class="btn sm" onclick="openLead(${m.lead_id})">Evidence</button>
        </div>
        <div class="inp"><label>Message</label><textarea id="wb${m.id}" style="min-height:110px">${esc(m.body)}</textarea></div>
        <div class="row" style="margin-top:11px">
          <a class="btn good" href="${esc(m.link)}" target="_blank" rel="noopener noreferrer"
             onclick="waMark(${m.id},'opened')">Open in WhatsApp ↗</a>
          <button class="btn" onclick="waMark(${m.id},'sent',true)">Mark as sent</button>
          <button class="btn danger" onclick="waMark(${m.id},'skipped',true)">Skip</button>
        </div>
      </div>`).join('') : `
      <div class="empty"><h3>Nothing queued</h3>
      <p>Messages appear here when a lead has a usable phone number.</p></div>`}`;
};

async function waMark(id, action, remove) {
  try {
    await post(`/api/whatsapp/${id}/${action}`);
    if (remove) { const c = $('#wa' + id); if (c) c.remove(); toast('Updated', 'good'); }
  } catch (e) { toast(e.message, 'bad'); }
}

/* ----------------------------------------------------------------- leads */

VIEWS.leads = async function () {
  const qs = new URLSearchParams(Object.fromEntries(
    Object.entries(FILTERS).filter(([, v]) => v !== '' && v !== 0)));
  const data = await api('/api/leads?' + qs);
  const opts = (list, cur, all) => `<option value="">${all}</option>` +
    list.map(([v, l]) => `<option value="${v}" ${cur === v ? 'selected' : ''}>${esc(l)}</option>`).join('');

  $('#view').innerHTML = `
    <div class="head">
      <div><h2>Leads</h2><p>${data.total} matching. Click any row for the evidence behind it.</p></div>
      <div class="row">
        <a class="btn" href="/api/export/leads.xlsx">Export Excel</a>
      </div>
    </div>
    <div class="filters">
      <input type="search" id="fq" placeholder="Search company…" value="${esc(FILTERS.q)}">
      <select id="fservice">${opts(Object.entries(SERVICE_LABEL), FILTERS.service, 'All services')}</select>
      <select id="fsource">${opts(Object.entries(SOURCE_LABEL), FILTERS.source, 'All sources')}</select>
      <select id="fcountry">${opts([['GB', 'United Kingdom'], ['US', 'United States'], ['AE', 'UAE'], ['IN', 'India']], FILTERS.country, 'All countries')}</select>
      <select id="fintent">${opts([['high', 'High intent'], ['medium', 'Medium'], ['low', 'Low']], FILTERS.intent, 'Any intent')}</select>
      <select id="fmail">${opts([['yes', 'Has email'], ['no', 'No email']], FILTERS.has_email, 'Email: any')}</select>
      <input type="date" id="ffrom" value="${FILTERS.date_from}" title="Generated from">
      <input type="date" id="fto" value="${FILTERS.date_to}" title="Generated to">
      <select id="fsort">${opts([['score', 'Top score'], ['created', 'Newest'], ['oldest', 'Oldest'], ['company', 'Company A–Z']], FILTERS.sort, 'Sort')}</select>
      <button class="btn ghost" id="fclear">Clear</button>
    </div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>Score</th><th>Company</th><th>Service</th><th>Country</th>
        <th>Email</th><th>Source</th><th>Stage</th><th>Generated</th></tr></thead>
      <tbody>${data.leads.map(rowHtml).join('') || '<tr><td colspan="8"><div class="empty"><h3>No leads match</h3><p>Try clearing the filters, or run a campaign.</p></div></td></tr>'}</tbody>
    </table></div>
    ${data.total > data.page_size ? `<div class="row" style="margin-top:12px;justify-content:center">
      <button class="btn" ${FILTERS.page <= 1 ? 'disabled' : ''} id="prev">Previous</button>
      <span class="muted">Page ${FILTERS.page} of ${Math.ceil(data.total / data.page_size)}</span>
      <button class="btn" ${FILTERS.page * data.page_size >= data.total ? 'disabled' : ''} id="next">Next</button>
    </div>` : ''}`;

  const bind = (id, key, ev = 'change') => {
    const node = $('#' + id);
    if (node) node.addEventListener(ev, e => { FILTERS[key] = e.target.value; FILTERS.page = 1; VIEWS.leads(); });
  };
  bind('fservice', 'service'); bind('fsource', 'source'); bind('fcountry', 'country');
  bind('fintent', 'intent'); bind('fmail', 'has_email'); bind('fsort', 'sort');
  bind('ffrom', 'date_from'); bind('fto', 'date_to');
  const q = $('#fq');
  q.addEventListener('keydown', e => { if (e.key === 'Enter') { FILTERS.q = q.value; FILTERS.page = 1; VIEWS.leads(); } });
  $('#fclear').onclick = () => { Object.assign(FILTERS, { service: '', source: '', country: '', intent: '', status: '', has_email: '', q: '', date_from: '', date_to: '', page: 1 }); VIEWS.leads(); };
  if ($('#prev')) $('#prev').onclick = () => { FILTERS.page--; VIEWS.leads(); };
  if ($('#next')) $('#next').onclick = () => { FILTERS.page++; VIEWS.leads(); };
  $('#view').querySelectorAll('tbody tr[data-id]').forEach(tr => tr.onclick = () => openLead(+tr.dataset.id));
};

function rowHtml(l) {
  return `<tr data-id="${l.id}">
    <td><span class="score ${scoreClass(l.score)}">${l.score}</span></td>
    <td class="biz">${esc(l.company)}<small>${esc(l.city || '')}${l.industry ? ' · ' + esc(l.industry.slice(0, 40)) : ''}</small></td>
    <td><span class="tag">${esc(SERVICE_LABEL[l.service_category] || l.service_category)}</span></td>
    <td>${esc(l.country)}${l.email_policy !== 'allowed' ? ' <span class="tag grey" title="Cold email not permitted here">no email</span>' : ''}</td>
    <td>${l.email ? `<span class="mono" style="font-size:12px">${esc(l.email)}</span>` : '<span class="muted">—</span>'}</td>
    <td><span class="tag grey">${esc(SOURCE_LABEL[l.source_kind] || l.source_kind || '—')}</span></td>
    <td><span class="pill ${esc(l.stage)}">${esc(l.stage.replace(/_/g, ' '))}</span></td>
    <td class="muted mono" style="font-size:12px">${fmtDate(l.created_at)}</td>
  </tr>`;
}

/* ----------------------------------------------------------- lead drawer */

function closeDrawer() { $('#drawer').classList.remove('on'); $('#drawerBg').classList.remove('on'); }
$('#drawerBg').onclick = closeDrawer;
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

async function openLead(id) {
  const d = $('#drawer');
  d.classList.add('on'); $('#drawerBg').classList.add('on');
  d.innerHTML = '<div class="empty">Loading…</div>';
  let L;
  try { L = await api('/api/leads/' + id); }
  catch (e) { d.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }

  const rules = (L.score_breakdown && L.score_breakdown.rules) || [];
  d.innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <span class="score ${scoreClass(L.score)}" style="height:34px;min-width:46px;font-size:16px">${L.score}</span>
      <div><h2 style="margin:0;font-size:19px">${esc(L.company)}</h2>
        <div class="muted" style="font-size:12.5px">${esc(L.city || '')} ${esc(L.country)} · ${esc(SERVICE_LABEL[L.service_category] || '')} · ${esc(L.intent_level)} intent</div></div>
      <span class="sp"></span>
      <button class="btn ghost" onclick="closeDrawer()">Close</button>
    </div>

    ${L.email_policy !== 'allowed' ? `<div class="banner">Cold email is not permitted for ${esc(L.country)}: ${esc(L.policy_reason || '')}. This lead is for manual outreach only.</div>` : ''}

    <div class="card">
      <h3>Why this lead was selected</h3>
      <p class="hint">${esc(L.intent_reason || 'No reasoning recorded.')}</p>
      ${rules.length ? `<div style="margin-top:6px">${rules.map(r => `
        <div class="row" style="padding:5px 0;border-bottom:1px solid var(--line);font-size:12.5px">
          <b class="mono" style="min-width:38px;color:${r.points < 0 ? 'var(--warn)' : 'var(--good)'}">${r.points > 0 ? '+' : ''}${r.points}</b>
          <span>${esc(r.description)}</span></div>`).join('')}</div>` : ''}
    </div>

    <div class="card">
      <h3>Evidence <span class="muted" style="font-weight:400;font-size:12px">(${L.signals.length})</span></h3>
      <p class="hint">Every fact the system holds, with its source. Outreach may only reference these.</p>
      ${L.signals.map(s => `
        <div class="ev">
          <div class="t">${esc(s.type.replace(/_/g, ' '))}
            <span class="badge ${s.evidence_class === 'explicit_request' ? 'exp' : 'inf'}">
            ${s.evidence_class === 'explicit_request' ? 'THEY ASKED' : 'WE OBSERVED'}</span>
            <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">${fmtDate(s.observed_at)} · ${esc(s.detected_by)}</span></div>
          <div class="q">${esc(s.quote)}</div>
          ${s.source_url ? `<a href="${esc(s.source_url)}" target="_blank" rel="noopener noreferrer">${esc(s.source_url)}</a>` : ''}
        </div>`).join('') || '<p class="muted">No evidence on file.</p>'}
    </div>

    ${L.audit ? `<div class="card"><h3>Website audit</h3>
      <p class="hint">${esc(L.audit.url || '')} — checked ${fmtDate(L.audit.audited_at)}</p>
      <div class="row" style="gap:6px">
        ${[['Live', L.audit.has_site], ['HTTPS', L.audit.has_ssl], ['Mobile', L.audit.is_mobile_friendly],
           ['Booking', L.audit.has_booking], ['Shop', L.audit.has_ecommerce], ['Contact form', L.audit.has_contact_form]]
          .map(([k, v]) => `<span class="pill ${v ? 'sent' : 'rejected'}">${k}: ${v ? 'yes' : 'no'}</span>`).join('')}
      </div>
      ${L.audit.notes ? `<p class="hint" style="margin:12px 0 0"><b>Findings:</b> ${esc(L.audit.notes)}</p>` : ''}
      ${Object.keys(L.audit.tech || {}).length ? `<p class="hint" style="margin:6px 0 0"><b>Tech:</b> ${esc(Object.keys(L.audit.tech).join(', '))}</p>` : ''}
    </div>` : ''}

    <div class="card"><h3>Contact</h3>
      ${L.contacts.length ? L.contacts.map(c => `<div class="row" style="padding:5px 0;font-size:13px">
        <span class="mono">${esc(c.email)}</span>${c.name ? `<span class="muted">${esc(c.name)}${c.title ? ', ' + esc(c.title) : ''}</span>` : ''}</div>`).join('')
      : '<p class="hint" style="margin:0">No email address found. This lead cannot be emailed.</p>'}
      ${L.phone ? `<div class="row" style="font-size:13px"><span class="muted">Phone</span><span class="mono">${esc(L.phone)}</span></div>` : ''}
      ${L.website ? `<div class="row" style="font-size:13px;margin-top:4px"><a href="${esc(L.website)}" target="_blank" rel="noopener noreferrer">${esc(L.website)}</a></div>` : ''}
      ${L.contacts.length && L.email_policy === 'allowed' ? `<div class="row" style="margin-top:12px">
        <button class="btn primary" onclick="draftFor(${L.id})">Write outreach</button></div>` : ''}
    </div>

    ${L.messages.length ? `<div class="card"><h3>Email history</h3>
      ${L.messages.map(m => `<div style="border-bottom:1px solid var(--line);padding:9px 0">
        <div class="row"><span class="pill ${esc(m.status)}">${esc(m.status.replace(/_/g, ' '))}</span>
          <b style="font-size:13px">${esc(m.subject)}</b><span class="sp"></span>
          <span class="muted mono" style="font-size:11.5px">${fmtTime(m.sent_at || m.created_at)}</span></div>
        ${m.failure_reason ? `<div class="muted" style="font-size:12px;color:var(--warn)">${esc(m.failure_reason)}</div>` : ''}
        <pre style="white-space:pre-wrap;font-family:inherit;font-size:12.5px;color:var(--muted);margin:6px 0 0">${esc(m.body)}</pre>
      </div>`).join('')}</div>` : ''}`;
}

async function draftFor(id) {
  toast('Writing…');
  try {
    const r = await post(`/api/leads/${id}/draft`);
    toast(r.used_fallback ? 'Drafted from template (no AI key)' : 'Drafted — see Outbox', 'good');
    closeDrawer(); go('outbox');
  } catch (e) { toast(e.message, 'bad'); }
}

/* ------------------------------------------------------------- campaigns */

VIEWS.campaigns = async function () {
  const list = await api('/api/campaigns');
  const srcAvail = Object.fromEntries((META.sources || []).map(s => [s.key, s]));

  $('#view').innerHTML = `
    <div class="head"><div><h2>Campaigns</h2>
      <p>Campaigns research on schedule. Nothing is emailed at that moment — approved
         mail goes out through the dispatcher, which runs every 10 minutes.</p></div>
      <button class="btn" onclick="dispatchNow()">Send queued now</button>
    </div>
    ${list.map(c => `
      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <b style="font-size:15px">${esc(c.name)}</b>
          <span class="tag">${esc(SERVICE_LABEL[c.service_category] || c.service_category)}</span>
          <span class="pill ${c.enabled ? 'sent' : 'rejected'}">${c.enabled ? 'enabled' : 'disabled'}</span>
          <span class="muted mono" style="font-size:12.5px">daily at ${esc(c.schedule_time)}</span>
          <span class="sp"></span>
          <span class="muted" style="font-size:12.5px">${c.total_leads} leads</span>
          <button class="btn sm primary" ${c.running ? 'disabled' : ''} onclick="runCampaign(${c.id})">
            ${c.running ? '<span class="spin"></span> Running' : 'Run now'}</button>
          ${c.last_run ? `<button class="btn sm" onclick="showRun(${c.last_run.id})">Last log</button>` : ''}
        </div>
        <div class="grid2">
          <div class="inp"><label>Sources</label>
            <div class="row" style="gap:5px">${(c.sources || []).map(s => {
              const a = srcAvail[s];
              return `<span class="pill ${a && a.available ? 'sent' : 'rejected'}" title="${a && a.available ? 'ready' : 'API key missing'}">${esc(SOURCE_LABEL[s] || s)}</span>`;
            }).join('') || '<span class="muted">all available</span>'}</div></div>
          <div class="inp"><label>Locations · countries</label>
            <div class="muted" style="font-size:12.5px">${esc((c.locations || []).join(', ') || 'default')} · ${esc((c.countries || []).join(', '))}</div></div>
        </div>
        <div class="row" style="margin-top:12px">
          <label class="row" style="font-size:12.5px;gap:6px"><input type="checkbox" ${c.enabled ? 'checked' : ''} onchange="toggleCampaign(${c.id},this.checked)"> Enabled</label>
          <label class="row" style="font-size:12.5px;gap:6px">Time <input type="time" value="${esc(c.schedule_time)}" onchange="setTime(${c.id},this.value)" style="border:1px solid var(--line);border-radius:6px;padding:4px 7px"></label>
          <label class="row" style="font-size:12.5px;gap:6px">Min score <input type="number" min="0" max="100" value="${c.min_score}" onchange="setMinScore(${c.id},this.value)" style="width:70px;border:1px solid var(--line);border-radius:6px;padding:4px 7px"></label>
          <label class="row" style="font-size:12.5px;gap:6px">Daily leads <input type="number" min="1" max="200" value="${c.daily_lead_limit}" onchange="setLimit(${c.id},this.value)" style="width:70px;border:1px solid var(--line);border-radius:6px;padding:4px 7px"></label>
          <label class="row" style="font-size:12.5px;gap:6px">Approval
            <select onchange="setApproval(${c.id},this.value)" style="border:1px solid var(--line);border-radius:6px;padding:4px 7px">
              <option value="manual" ${c.approval_mode === 'manual' ? 'selected' : ''}>Manual</option>
              <option value="auto" ${c.approval_mode === 'auto' ? 'selected' : ''}>Auto-send</option></select></label>
        </div>
        ${c.last_run && c.last_run.stats ? `<div class="muted" style="font-size:12px;margin-top:10px">
          Last run: ${c.last_run.stats.leads_created || 0} leads, ${c.last_run.stats.drafted || 0} drafted,
          ${c.last_run.stats.rejected || 0} rejected, ${c.last_run.stats.errors || 0} errors
          · ${fmtTime(c.last_run.started_at)}</div>` : ''}
      </div>`).join('')}`;
};

let _campaigns = {};
async function updateCampaign(id, changes) {
  const list = await api('/api/campaigns');
  const c = list.find(x => x.id === id);
  if (!c) return;
  const body = {
    name: c.name, service_category: c.service_category, sources: c.sources,
    keywords: c.keywords, locations: c.locations, countries: c.countries,
    schedule_time: c.schedule_time, enabled: c.enabled,
    daily_lead_limit: c.daily_lead_limit, daily_email_limit: c.daily_email_limit,
    approval_mode: c.approval_mode, min_score: c.min_score, ...changes,
  };
  await api(`/api/campaigns/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  toast('Saved', 'good');
}
const toggleCampaign = (id, v) => updateCampaign(id, { enabled: v });
const setTime = (id, v) => updateCampaign(id, { schedule_time: v });
const setMinScore = (id, v) => updateCampaign(id, { min_score: +v });
const setLimit = (id, v) => updateCampaign(id, { daily_lead_limit: +v });
const setApproval = (id, v) => updateCampaign(id, { approval_mode: v });

async function runCampaign(id) {
  try {
    await post(`/api/campaigns/${id}/run`);
    toast('Campaign started — watch the log');
    const list = await api('/api/campaigns');
    const c = list.find(x => x.id === id);
    if (c && c.last_run) showRun(c.last_run.id);
    setTimeout(() => VIEWS.campaigns(), 1500);
  } catch (e) { toast(e.message, 'bad'); }
}

async function dispatchNow() {
  try {
    const r = await post('/api/outbox/dispatch');
    toast(`Sent ${r.sent}, failed ${r.failed}, skipped ${r.skipped}`, r.failed ? 'bad' : 'good');
  } catch (e) { toast(e.message, 'bad'); }
}

async function showRun(runId) {
  const d = $('#drawer');
  d.classList.add('on'); $('#drawerBg').classList.add('on');
  const paint = async () => {
    let r;
    try { r = await api('/api/runs/' + runId); } catch (e) { return; }
    d.innerHTML = `
      <div class="row" style="margin-bottom:14px">
        <h2 style="margin:0;font-size:18px">Campaign run #${r.id}</h2>
        <span class="pill ${r.status === 'completed' ? 'sent' : r.status === 'failed' ? 'rejected' : 'queued'}">${esc(r.status)}</span>
        <span class="sp"></span><button class="btn ghost" onclick="closeDrawer()">Close</button></div>
      ${r.error ? `<div class="banner bad">${esc(r.error.slice(0, 600))}</div>` : ''}
      <div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(105px,1fr))">
        ${Object.entries(r.stats || {}).map(([k, v]) => `<div class="tile"><div class="k">${esc(k.replace(/_/g, ' '))}</div>
          <div class="v" style="font-size:21px">${typeof v === 'number' && k.includes('cost') ? '$' + v.toFixed(3) : esc(v)}</div></div>`).join('')}
      </div>
      <div class="card"><h3>Log</h3><div class="log" id="runLog">${(r.log || []).map(esc).join('\n') || 'No output yet…'}</div></div>`;
    const lg = $('#runLog'); if (lg) lg.scrollTop = lg.scrollHeight;
    if (r.status === 'running' && d.classList.contains('on')) setTimeout(paint, 2000);
  };
  d.innerHTML = '<div class="empty">Loading…</div>';
  paint();
}

/* -------------------------------------------------------------- settings */

VIEWS.settings = async function () {
  const [s, sup, boxes, sched, me] = await Promise.all([
    api('/api/settings'), api('/api/suppressions'), api('/api/mailboxes'), api('/api/scheduler'),
    api('/auth/me'),
  ]);
  const yn = v => v ? '<span class="pill sent">configured</span>' : '<span class="pill rejected">not set</span>';

  $('#view').innerHTML = `
    <div class="head"><div><h2>Settings</h2>
      <p>Secrets live in the <code>.env</code> file, never in the database. Edit that file
         and restart to change them.</p></div></div>

    <div class="card"><h3>Security</h3>
      <p class="hint">Password sign-in${me.login_required ? '' : ' is off — set ADMIN_PASSWORD in .env before putting this online'}.</p>
      <div class="row" style="gap:18px">
        <div><div class="muted" style="font-size:11.5px">Login</div>
          <b class="mono">${me.login_required ? 'required' : 'disabled (local only)'}</b></div>
        ${me.login_required ? `<div><div class="muted" style="font-size:11.5px">Google Authenticator</div>
          <b class="mono">${me.totp_required ? 'not finished' : 'on'}</b></div>` : ''}
      </div>
      ${me.login_required ? `<div class="save-row">
        ${me.totp_required ? `<button class="btn" onclick="location.href='/auth/login-page'">Finish 2FA setup</button>`
          : `<button class="btn danger" id="disable2fa">Disable 2FA</button>`}
        <button class="btn" id="logoutBtn">Log out</button>
        <button class="btn ghost" id="logoutAllBtn">Log out everywhere</button>
      </div>` : ''}
    </div>

    <div class="card"><h3>Free keys — get these in order</h3>
      <p class="hint">All free. The top one matters most: it feeds all four
         campaigns and every result it returns is a live website the audit can
         mine for an email address.</p>
      <div id="freeKeys">Loading…</div>
      <div class="save-row"><button class="btn" id="applyRec">Use recommended sources</button>
        <span class="muted" style="font-size:12px">Resets each campaign to the best free source list</span></div>
    </div>

    <div class="card"><h3>Credentials</h3>
      <p class="hint">Each of these unlocks a capability. The app runs without any of them,
         but can only use free sources and cannot send.</p>
      <div style="display:grid;gap:8px">
        ${[['ANTHROPIC_API_KEY', 'anthropic_api_key', 'AI-written outreach (falls back to templates)'],
           ['GOOGLE_PLACES_KEY', 'google_places_key', 'Business listings — best coverage'],
           ['COMPANIES_HOUSE_KEY', 'companies_house_key', 'New UK companies — free key'],
           ['ADZUNA_APP_ID / KEY', 'adzuna', 'Job-ad hiring signals — free tier'],
           ['SMTP_HOST / USER / PASS', 'smtp', 'Sending email']]
          .map(([k, key, why]) => `<div class="row" style="padding:7px 0;border-bottom:1px solid var(--line)">
            <code style="font-size:12px">${k}</code>
            <span class="muted" style="font-size:12px">${why}</span>
            <span class="sp"></span>${yn(s.configured[key])}</div>`).join('')}
      </div>
    </div>

    <div class="card"><h3>Sender identity</h3>
      <p class="hint">A real postal address is legally required in every marketing email
         sent to the United States. The app refuses to send without one.</p>
      <div class="grid2">
        ${[['Company name', s.company_name], ['Sender name', s.sender_name],
           ['Sender email', s.sender_email], ['Portfolio URL', s.portfolio_url],
           ['Postal address', s.company_postal_address], ['Privacy policy', s.privacy_policy_url]]
          .map(([k, v]) => `<div class="inp"><label>${k}</label>
            <div style="padding:8px 0;font-size:13px">${v ? esc(v) : '<span class="pill rejected">not set</span>'}</div></div>`).join('')}
      </div>
    </div>

    <div class="card"><h3>Sending limits</h3>
      <div class="row" style="gap:22px;font-size:13px">
        <div><div class="muted" style="font-size:11.5px">Global daily cap</div><b class="mono">${s.daily_email_cap_global}</b></div>
        <div><div class="muted" style="font-size:11.5px">Per mailbox</div><b class="mono">${s.per_mailbox_daily_cap}</b></div>
        <div><div class="muted" style="font-size:11.5px">Cooling-off</div><b class="mono">${s.cooling_off_days} days</b></div>
        <div><div class="muted" style="font-size:11.5px">Min score to email</div><b class="mono">${s.min_score_to_email}</b></div>
      </div>
      ${boxes.length ? `<div style="margin-top:14px">${boxes.map(m => `
        <div class="row" style="padding:7px 0;border-top:1px solid var(--line);font-size:12.5px">
          <span class="mono">${esc(m.address)}</span>
          <span class="muted">sent ${m.sent_today} of ${m.effective_cap_today} today</span>
          ${m.effective_cap_today < m.daily_cap ? '<span class="tag">warming up</span>' : ''}
        </div>`).join('')}</div>` : '<p class="hint" style="margin:12px 0 0">No mailbox registered yet. Set SENDER_EMAIL and restart.</p>'}
    </div>

    <div class="card"><h3>Scheduled jobs</h3>
      ${sched.jobs.length ? sched.jobs.map(j => `<div class="row" style="padding:5px 0;font-size:12.5px">
        <code style="font-size:12px">${esc(j.id)}</code><span class="sp"></span>
        <span class="muted">next ${j.next_run ? fmtTime(j.next_run) : '—'}</span></div>`).join('')
      : '<p class="hint" style="margin:0">Scheduler is off.</p>'}
    </div>

    <div class="card"><h3>Suppression list <span class="muted" style="font-weight:400;font-size:12px">(${sup.length})</span></h3>
      <p class="hint">Anyone who unsubscribes or hard-bounces lands here permanently and is
         checked before every single send, even if they are rediscovered later.</p>
      <div class="row" style="margin-bottom:10px">
        <input id="supVal" placeholder="email@example.com or example.com" style="flex:1;border:1px solid var(--line);border-radius:8px;padding:8px 11px">
        <select id="supScope" style="border:1px solid var(--line);border-radius:8px;padding:8px"><option value="email">Email</option><option value="domain">Domain</option></select>
        <button class="btn" id="supAdd">Add</button>
      </div>
      ${sup.slice(0, 40).map(x => `<div class="row" style="padding:5px 0;border-top:1px solid var(--line);font-size:12.5px">
        <span class="tag grey">${esc(x.scope)}</span><span class="mono">${esc(x.value)}</span>
        <span class="muted">${esc(x.reason)}</span><span class="sp"></span>
        <button class="btn sm ghost" onclick="rmSup(${x.id})">remove</button></div>`).join('')}
    </div>

    <div class="card"><h3>Where email is permitted</h3>
      <p class="hint">These are the only countries we email. Companies elsewhere are kept for
         manual outreach and can never be emailed by any route in this application.</p>
      ${Object.entries(META.country_policy || {}).map(([code, p]) => `
        <div class="row" style="padding:6px 0;border-top:1px solid var(--line);font-size:12.5px">
          <b style="min-width:150px">${esc(p.name || code)} <span class="muted">(${esc(code)})</span></b>
          <span class="pill ${p.policy === 'allowed' ? 'sent' : p.policy === 'blocked' ? 'rejected' : 'pending_approval'}">${esc(p.policy.replace('_', ' '))}</span>
          <span class="muted">${esc(p.reason)}</span></div>`).join('')}
    </div>`;

  try {
    const g = await api('/api/sources');
    const avail = Object.fromEntries(g.sources.map(x => [x.key, x]));
    $('#freeKeys').innerHTML = g.free_keys.map((k, i) => `
      <div class="row" style="align-items:flex-start;gap:9px;padding:9px 0;border-top:1px solid var(--line)">
        <b class="mono" style="min-width:18px;color:var(--muted)">${i + 1}</b>
        <div style="flex:1;min-width:0">
          <div class="row" style="gap:7px">
            <b style="font-size:13px">${esc(k.name)}</b>
            <span class="tag grey">${esc(k.free)}</span>
            ${k.configured ? '<span class="pill sent">configured</span>' : '<span class="pill rejected">not set</span>'}
          </div>
          <div class="muted" style="font-size:12px;margin-top:3px">${esc(k.why)}</div>
          <code style="font-size:11px">${esc(k.key)}</code>
        </div>
        <a class="btn sm" href="${esc(k.url)}" target="_blank" rel="noopener noreferrer">Get it ↗</a>
      </div>`).join('') + `
      <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
        <div class="muted" style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:7px">Sources per campaign</div>
        ${Object.entries(g.recommended).map(([svc, list]) => `
          <div class="row" style="align-items:flex-start;gap:8px;padding:5px 0;font-size:12.5px">
            <span class="tag" style="flex-shrink:0">${esc(SERVICE_LABEL[svc] || svc)}</span>
            <span style="flex:1;min-width:0">${list.map(k => {
              const a = avail[k];
              return `<span class="pill ${a && a.available ? 'sent' : 'rejected'}" style="margin:0 3px 3px 0">${esc(SOURCE_LABEL[k] || k)}</span>`;
            }).join('')}</span>
          </div>`).join('')}
      </div>`;
    $('#applyRec').onclick = async () => {
      try {
        const r = await post('/api/sources/apply-recommended');
        toast(`Updated ${r.campaigns_updated} campaigns`, 'good');
      } catch (e) { toast(e.message, 'bad'); }
    };
  } catch (e) { $('#freeKeys').innerHTML = `<span class="muted">${esc(e.message)}</span>`; }

  $('#supAdd').onclick = async () => {
    const value = $('#supVal').value.trim();
    if (!value) return;
    try { await post('/api/suppressions', { value, scope: $('#supScope').value, reason: 'added manually' }); toast('Added', 'good'); VIEWS.settings(); }
    catch (e) { toast(e.message, 'bad'); }
  };

  if ($('#logoutBtn')) $('#logoutBtn').onclick = async () => {
    await post('/auth/logout'); location.href = '/auth/login-page';
  };
  if ($('#logoutAllBtn')) $('#logoutAllBtn').onclick = async () => {
    if (!confirm('Log out every signed-in device?')) return;
    await api('/auth/logout-everywhere'); location.href = '/auth/login-page';
  };
  if ($('#disable2fa')) $('#disable2fa').onclick = async () => {
    if (!confirm('Turn off Google Authenticator? Only the password will be needed to sign in.')) return;
    try { await post('/auth/totp/disable'); toast('2FA disabled', 'good'); VIEWS.settings(); }
    catch (e) { toast(e.message, 'bad'); }
  };
};

async function rmSup(id) {
  try { await api('/api/suppressions/' + id, { method: 'DELETE' }); VIEWS.settings(); }
  catch (e) { toast(e.message, 'bad'); }
}

/* ------------------------------------------------------------------ boot */

(async () => {
  try {
    META = await api('/api/meta');
    const s = await api('/api/stats/summary');
    window._pending = s.pending_approval;
  } catch (e) {
    $('#view').innerHTML = `<div class="banner bad">Could not reach the API: ${esc(e.message)}</div>`;
    return;
  }
  renderFoot();
  const hash = (location.hash || '').replace('#', '');
  go(VIEWS[hash] ? hash : 'workflow');
})();
