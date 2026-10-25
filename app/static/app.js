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
  ai_agent: 'AI Agents', ai_automation: 'AI Automation', digital_marketing: 'Digital Marketing',
};
const SOURCE_LABEL = {
  tender: 'Public tender', google_places: 'Google Places', companies_house: 'Companies House',
  job_board: 'Job ad', hacker_news: 'Hacker News', osm: 'OpenStreetMap', website: 'Website', legacy: 'Imported',
  serp: 'Web search', serper: 'Web search', yelp: 'Yelp', github: 'GitHub',
  contracts_finder: 'Contracts Finder', find_a_tender: 'Find a Tender',
  sam_gov: 'SAM.gov', adzuna: 'Adzuna', openstreetmap: 'OpenStreetMap',
  linkedin_search: 'LinkedIn (via search)',
};
const BUILTIN_SERVICE = { ...SERVICE_LABEL };

// Services the user added live in the database. Fold them into the same lookup
// tables the built-in four use, so every screen shows their real names.
function syncServices() {
  Object.keys(SERVICE_LABEL).forEach(k => { if (!(k in BUILTIN_SERVICE)) delete SERVICE_LABEL[k]; });
  (META.services || []).forEach(s => { if (!s.builtin) SERVICE_LABEL[s.key] = s.label; });
}
async function refreshServices() {
  META.services = (await api('/api/meta')).services;
  syncServices();
}
const DAYS_LABEL = { 'mon-fri': 'Mon–Fri', daily: 'Every day', weekends: 'Weekends' };

function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast on' + (kind ? ' ' + kind : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = 'toast', 3800);
}

// Instant feedback. The database is a network hop away, so a click can take a
// moment to come back: the clicked button shows it is working straight away,
// and a thin bar runs along the top while anything is loading. Background
// polls pass {quiet: true} and show nothing.
let _inflight = 0, _lastClick = null, _busyClear = null;
const _busyBtns = new Set();
document.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b) _lastClick = { b, t: performance.now() };
}, true);

function busyStart() {
  _inflight++;
  clearTimeout(_busyClear);
  document.body.classList.add('busy');
  if (_lastClick && performance.now() - _lastClick.t < 400 && document.contains(_lastClick.b)) {
    _lastClick.b.classList.add('is-busy');
    _busyBtns.add(_lastClick.b);
    _lastClick = null;
  }
}

function busyEnd() {
  if (--_inflight > 0) return;
  _inflight = 0;
  // A short grace period, so a click that makes several requests in a row
  // stays busy for the whole sequence instead of flickering between them.
  _busyClear = setTimeout(() => {
    if (_inflight) return;
    document.body.classList.remove('busy');
    _busyBtns.forEach(b => b.classList.remove('is-busy'));
    _busyBtns.clear();
  }, 150);
}

async function api(path, opts = {}) {
  if (!opts.quiet) busyStart();
  try { return await _api(path, opts); } finally { if (!opts.quiet) busyEnd(); }
}

async function _api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let m = `Error ${r.status}`;
    try {
      const d = (await r.json()).detail;
      // FastAPI validation errors arrive as a list of {loc, msg}.
      m = Array.isArray(d)
        ? d.map(x => `${(x.loc || []).slice(-1)[0] || 'value'}: ${String(x.msg).replace(/^Value error, /, '')}`).join('; ')
        : d || m;
    } catch (e) { /* non-JSON error */ }
    // This page was opened as one account but the browser is now signed in as
    // another (a second tab signed in). Reload once to pick up who it really is.
    if (r.status === 403 && /Only the platform admin/.test(m)) {
      try { if (!sessionStorage.getItem('mm_reloaded')) { sessionStorage.setItem('mm_reloaded', '1'); location.reload(); } } catch (e) { /* private mode */ }
    }
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
  { group: 'Workspace', ws: true },
  { key: 'workflow', ws: true, label: 'Agent', icon: 'M5 3v4a2 2 0 002 2h3m4 0h3a2 2 0 002-2V3M5 21v-4a2 2 0 012-2h10a2 2 0 012 2v4M12 9v6' },
  { key: 'outbox', ws: true, label: 'Outbox', icon: 'M3 8l9 6 9-6M3 8v9a1 1 0 001 1h16a1 1 0 001-1V8l-9-6-9 6z' },
  { key: 'dashboard', ws: true, label: 'Dashboard', icon: 'M4 20V10M10 20V4M16 20v-7M22 20H2' },
  { key: 'linkedin', ws: true, label: 'LinkedIn', icon: 'M16 8a6 6 0 016 6v7h-4v-7a2 2 0 00-4 0v7h-4v-11h4v2a4 4 0 014-2zM2 9h4v12H2zM4 2a2 2 0 110 4 2 2 0 010-4z' },
  { key: 'whatsapp', ws: true, label: 'WhatsApp', icon: 'M21 11.5a8.4 8.4 0 01-12 7.6L3 21l1.9-5.7A8.4 8.4 0 1121 11.5z' },
  { key: 'leads', ws: true, label: 'Leads', icon: 'M3 6h18M3 12h18M3 18h12' },
  { key: 'campaigns', ws: true, label: 'Campaigns', icon: 'M12 2v4m0 12v4M2 12h4m12 0h4M6 6l3 3m6 6l3 3m0-12l-3 3m-6 6l-3 3' },
  { group: 'Account', customer: true },
  { key: 'bots', label: 'My bots', customer: true, icon: 'M12 2l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 14.4 7.2 16.9l.9-5.4L4.2 7.7l5.4-.8L12 2z' },
  { key: 'billing', label: 'Billing', customer: true, icon: 'M2 7h20v12H2zM2 11h20M6 15h4' },
  { key: 'account', label: 'Settings', customer: true, icon: 'M12 15a3 3 0 100-6 3 3 0 000 6zM3 12h3m12 0h3M12 3v3m0 12v3' },
  { group: 'Platform', admin: true },
  { key: 'admin', label: 'Overview', admin: true, icon: 'M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z' },
  { key: 'customers', label: 'Customers', admin: true, icon: 'M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75' },
  { key: 'adminbots', label: 'Bots & pricing', admin: true, icon: 'M20.6 13.4l-7.2 7.2a2 2 0 01-2.8 0L2 12V2h10l8.6 8.6a2 2 0 010 2.8zM7 7h.01' },
  { key: 'payments', label: 'Payments', admin: true, icon: 'M12 1v22M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6' },
  { key: 'settings', label: 'Settings', admin: true, icon: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6' },
];

const isAdmin = () => !!(META.user && META.user.role === 'admin');

// The admin runs the platform and has no workspace of its own: its sidebar is
// the admin panel only. Customers never see the admin pages.
const navItems = () => NAV.filter(n => isAdmin() ? n.admin : !n.admin);
const ADMIN_VIEWS = new Set(NAV.filter(n => n.admin && n.key).map(n => n.key));

function renderNav(pending) {
  $('#nav').innerHTML = navItems().map(n => n.group
    ? `<div class="grp">${esc(n.group)}</div>`
    : `<button data-v="${n.key}" class="${VIEW === n.key ? 'on' : ''}">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round"><path d="${n.icon}"/></svg>
      ${n.label}${n.key === 'outbox' && pending ? `<span class="pip">${pending}</span>` : ''}
    </button>`).join('');
  $('#nav').querySelectorAll('button').forEach(b => b.onclick = () => go(b.dataset.v));
}

// What the platform can do right now. The admin sees one line of it in the
// sidebar and the full list on Overview, where the payment checks are added.
function systemChecks(o) {
  const pcSender = META.send_on_server === false;
  const list = [
    { name: 'Sending email', ok: pcSender || !!META.email_ready, icon: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z',
      on: pcSender ? 'Approved mail goes out from your computer sender' : 'SMTP is set, so approved mail can go out',
      off: 'No SMTP yet, so approved mail cannot leave. Set SMTP_HOST, SMTP_USER and SMTP_PASS.' },
    { name: 'Reply watch', ok: !!META.inbox_ready, icon: 'M21 11.5a8.4 8.4 0 01-12 7.6L3 21l1.9-5.7A8.4 8.4 0 1121 11.5z',
      on: 'The inbox is read every 5 minutes for replies and bounces',
      off: 'Replies and bounces are not read. Set IMAP_HOST.' },
    { name: 'Scheduler', ok: !!META.scheduler_enabled, icon: NODE_ICONS.clock,
      on: 'Agents start by themselves at their set times', off: 'Agents only run when you start them by hand' },
    { name: 'AI writing', ok: !!META.ai_ready, icon: NODE_ICONS.brain,
      on: 'Claude qualifies leads and writes drafts, billed per use',
      off: 'Free mode: rules and templates. Set ANTHROPIC_API_KEY to switch on.' },
    { name: 'Telegram alerts', ok: !!META.telegram_ready, icon: 'M15 17h5l-1.4-1.4A2 2 0 0118 14.2V11a6 6 0 00-12 0v3.2a2 2 0 01-.6 1.4L4 17h5m6 0a3 3 0 01-6 0',
      on: 'A message reaches you the moment someone replies',
      off: 'No reply alerts. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.' },
  ];
  if (o) list.unshift(
    { name: 'Account emails', ok: !!o.account_mail_ready, icon: 'M3 8l9 6 9-6M3 8v9a1 1 0 001 1h16a1 1 0 001-1V8l-9-6-9 6z',
      on: 'Sign-up confirmations and password resets can be sent',
      off: 'Not set, so customers can\'t reset passwords or confirm their email. Set SMTP_HOST, SMTP_USER, SMTP_PASS and SENDER_EMAIL.' },
    { name: 'Payments', ok: !!o.razorpay_ready, icon: 'M2 7h20v12H2zM2 11h20M6 15h4',
      on: 'Razorpay is connected, so customers can subscribe online',
      off: 'Customers cannot pay yet. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.' },
    { name: 'Payment webhook', ok: !!o.webhook_ready, icon: 'M10 13a5 5 0 007.5.5l3-3a5 5 0 00-7-7l-1.7 1.7M14 11a5 5 0 00-7.5-.5l-3 3a5 5 0 007 7l1.7-1.7',
      on: 'Payments are confirmed even if the customer closes the tab',
      off: 'Set RAZORPAY_WEBHOOK_SECRET so no payment is missed.' });
  return list;
}

function renderFoot() {
  const c = (ok, on, off) => `<span class="chip ${ok ? 'ok' : 'off'}"><span class="dot"></span>${ok ? on : off}</span>`;
  if (!isAdmin()) {  // customers see only what concerns their own setup
    $('#foot').innerHTML = c(META.email_ready, 'Mailbox connected', 'No mailbox connected') +
      c(META.inbox_ready, 'Replies are watched', 'Replies not watched');
    return;
  }
  const checks = systemChecks(), ready = checks.filter(x => x.ok).length, all = ready === checks.length;
  $('#foot').innerHTML = `<button class="chip ${all ? 'ok' : 'off'}" onclick="go('admin')" title="Open system health on Overview">
    <span class="dot"></span>${all ? 'All systems ready' : `${ready} of ${checks.length} systems ready`}<small>Details</small></button>`;
}

const VIEWS = {};
async function go(v) {
  if (isAdmin() && !ADMIN_VIEWS.has(v)) v = 'admin';
  const view = $('#view'), changed = VIEW !== v || !view.childElementCount;
  VIEW = v;
  if (location.hash !== '#' + v) history.replaceState(null, '', '#' + v);
  renderNav();
  // Keep the current page on screen (faded) until the next one is ready,
  // rather than blanking it to a "Loading…" message.
  if (!view.childElementCount) view.innerHTML = '<div class="empty">Loading…</div>';
  view.classList.add('loading');
  try { await VIEWS[v](); } catch (e) { view.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
  view.classList.remove('loading');
  if (changed) $('.main').scrollTop = 0;
  renderNav(window._pending);
}

// Every page header carries its sidebar icon. Views redraw themselves (filters,
// refreshes), so this watches the view instead of running once per navigation.
function decorateHead() {
  const t = $('#view > .head > div:first-child');
  const n = NAV.find(x => x.key === VIEW);
  if (!t || !n || t.classList.contains('head-t') || !t.querySelector('h2')) return;
  t.classList.add('head-t');
  t.insertAdjacentHTML('afterbegin', `<span class="head-ico">${svgIcon(n.icon, 21)}</span>`);
}
new MutationObserver(decorateHead).observe($('#view'), { childList: true });

// One KPI tile. `v2` shows a second figure under the first, e.g. another currency.
function kpi(label, value, { sub = '', icon = NODE_ICONS.doc, tone = '', act = false, v2 = '', i = 0 } = {}) {
  return `<div class="kpi ${tone}${act ? ' act' : ''}" style="--i:${i}">
    <div class="kpi-top"><span class="kpi-ico">${svgIcon(icon, 16)}</span><span class="kpi-k">${esc(label)}</span></div>
    <div class="kpi-v">${value}</div>${v2 ? `<div class="kpi-v2">${v2}</div>` : ''}${sub ? `<div class="kpi-s">${sub}</div>` : ''}
  </div>`;
}

// A card title with its icon, an optional subtitle and an optional control on the right.
function cardHead(icon, title, sub = '', extra = '') {
  return `<div class="card-h"><span class="card-ico">${svgIcon(icon, 16)}</span>
    <div><h3>${title}</h3>${sub ? `<div class="sub">${sub}</div>` : ''}</div>${extra ? `<span class="sp"></span>${extra}` : ''}</div>`;
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
  x:       'M18 6L6 18M6 6l12 12',
  clock:   'M12 21a9 9 0 100-18 9 9 0 000 18zM12 7v5l3 2',
  plus:    'M12 5v14M5 12h14',
  trash:   'M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6M10 11v6M14 11v6',
  rocket:  'M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 00-2.91-.09zM12 15l-3-3a22 22 0 012-3.95A12.88 12.88 0 0122 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 01-4 2zM9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5',
};

// A soft tint per step. The panel stays dark and the text white; only the
// icons, packages and step animations pick these up.
const STAGE_TINT = {
  trigger: '#FCD34D', discovery: '#67E8F9', resolution: '#7DD3FC', research: '#A5B4FC',
  signals: '#C4B5FD', qualify: '#F0ABFC', scoring: '#F9A8D4', personalize: '#FDBA74', outbox: '#86EFAC',
};
const tint = k => STAGE_TINT[k] || '#E8EEF4';

// Headline numbers from the latest run. They stay on screen after the run ends
// and only reset when the next agent starts.
const RUN_STATS = [
  ['found', 'Found', 'search'], ['new_companies', 'New companies', 'merge'],
  ['audited', 'Sites audited', 'globe'], ['signals', 'Evidence', 'doc'],
  ['qualified', 'Qualified', 'brain'], ['leads_created', 'Leads', 'gauge'],
  ['drafted', 'Drafted', 'pen'], ['rejected', 'Rejected', 'x'],
];
const RUN_LABEL = { ...Object.fromEntries(RUN_STATS.map(([k, label]) => [k, label])), ai_cost_usd: 'AI cost' };

const svgIcon = (d, size = 14) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>`;

const REDUCED_MOTION = matchMedia('(prefers-reduced-motion: reduce)').matches;
const pad2 = n => String(n).padStart(2, '0');

// "Today 13:00", "Tomorrow 07:00", "Wed 21 Oct, 07:00" - in the viewer's clock.
function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso), now = new Date();
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate())
    - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 864e5);
  const t = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (days === 0) return `Today ${t}`;
  if (days === 1) return `Tomorrow ${t}`;
  if (days === -1) return `Yesterday ${t}`;
  return `${d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })}, ${t}`;
}

function fmtIn(ms) {
  if (ms <= 0) return 'starting now';
  const s = Math.floor(ms / 1000), d = Math.floor(s / 86400),
    h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return 'in ' + (d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`);
}

const runEnded = run => ({ failed: 'failed', cancelled: 'stopped' }[run.status] || 'finished');
const runPill = s => ({ completed: 'sent', failed: 'rejected', cancelled: 'cancelled' }[s] || 'queued');

const fmtDur = ms => {
  const s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600);
  return (h ? h + ':' : '') + pad2(Math.floor(s % 3600 / 60)) + ':' + pad2(s % 60);
};

// Only touch the DOM when the markup really changed, so running animations
// (spinners, sheens) are not restarted by every poll.
const setHtml = (node, html) => { if (node && node._html !== html) { node._html = html; node.innerHTML = html; } };

// Count from the number on screen to the new one. A fresh element counts up
// from zero, which is what makes a finished run "land" on the screen.
function animateNumber(node, to) {
  if (!node) return;
  const from = node.dataset.v == null ? 0 : +node.dataset.v;
  node.dataset.v = to;
  node.classList.toggle('zero', !to);
  const fmt = v => Math.round(v).toLocaleString();
  if (REDUCED_MOTION || from === to) { node.textContent = fmt(to); return; }
  cancelAnimationFrame(node._raf);
  const t0 = performance.now(), dur = from ? 600 : 1100;
  const step = now => {
    const p = Math.min(1, (now - t0) / dur);
    node.textContent = fmt(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) node._raf = requestAnimationFrame(step);
  };
  node._raf = requestAnimationFrame(step);
  if (from) { node.classList.remove('bump'); void node.offsetWidth; node.classList.add('bump'); }
}

let _wfTimer = null, _wfTick = null, _wf = null, _heroKey = '', _flowKey = '', _wfSeq = 0;

VIEWS.workflow = async function () {
  clearTimeout(_wfTimer);
  clearInterval(_wfTick);
  _heroKey = _flowKey = '';
  const [wf, camps] = await Promise.all([api('/api/workflow'), api('/api/campaigns')]);

  $('#view').innerHTML = `
    <div class="head">
      <div><h2>Agent control room</h2>
      <p>Every agent runs these nine stages for each prospect it finds. The numbers are real
         work from the latest run, and they stay here until the next agent starts.</p></div>
      <div class="row">
        ${camps.length ? `
        <select id="wfCamp" class="btn" style="padding:7px 10px" aria-label="Agent to run">
          ${camps.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}
        </select>
        <button class="btn go" id="wfRun">▶ Run agent</button>` : ''}
      </div>
    </div>
    <div id="wfHero"></div>
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

  const runBtn = $('#wfRun');
  if (runBtn) runBtn.onclick = async () => {
    if (runBtn.dataset.stop) { stopRun(+runBtn.dataset.stop); return; }
    try {
      await post(`/api/campaigns/${$('#wfCamp').value}/run`);
      toast('Agent started', 'good');
      paintWorkflow();
    } catch (e) { toast(e.message, 'bad'); }
  };
  _wfTick = setInterval(tickWorkflow, 1000);
  paintWorkflow(wf);
};

async function paintWorkflow(pre) {
  if (!$('#wfCanvas')) return;
  let wf = pre;
  const seq = ++_wfSeq;
  if (!wf) {
    try { wf = await api('/api/workflow', { quiet: true }); } catch (e) { if (seq === _wfSeq) pollWorkflow(_wf); return; }
    if (!$('#wfCanvas')) return;  // left the page while the request was in flight
    if (seq !== _wfSeq) return;   // a newer refresh started; never paint an older state over it
  }
  _wf = wf;
  const running = !!wf.run && wf.run.status === 'running';
  paintHero(wf, running);
  paintFlow(wf, running);
  renderDrops(wf.nodes || []);

  const btn = $('#wfRun');
  if (btn) {
    const stopping = running && wf.run.stopping;
    btn.disabled = !!stopping;
    btn.classList.toggle('stop', running);
    btn.dataset.stop = running ? wf.run.campaign_id : '';
    btn.innerHTML = stopping ? '<span class="spin"></span> Stopping…' : running ? '■ Stop agent' : '▶ Run agent';
  }
  tickWorkflow();
  pollWorkflow(wf);
}

// Fast while an agent works, quick around a scheduled start, slow otherwise.
function pollWorkflow(wf) {
  clearTimeout(_wfTimer);
  if (VIEW !== 'workflow' || !wf) return;
  const running = wf.run && wf.run.status === 'running';
  const next = (wf.upcoming || [])[0];
  const due = next && new Date(next.at) - Date.now() < 5000;
  _wfTimer = setTimeout(() => paintWorkflow(), running ? 1200 : due ? 2500 : 20000);
}

function paintHero(wf, running) {
  const box = $('#wfHero');
  if (!box) return;
  const run = wf.run, up = wf.upcoming || [];
  // Rebuilt only when what it describes changes; numbers and labels in
  // between are patched in place so the animations keep running.
  const key = [running ? 'live' : 'idle', run ? run.id + run.status : 'none', wf.scheduler_enabled,
    up.map(u => u.campaign_id + '@' + u.at).join(',')].join('|');
  if (key !== _heroKey) {
    _heroKey = key;
    box.innerHTML = heroHtml(wf, running);
  }
  const stats = (run && run.stats) || {};
  box.querySelectorAll('.hx-t .tv').forEach(n => animateNumber(n, +stats[n.dataset.k] || 0));
  if (running) {
    const active = (wf.nodes || []).find(n => n.key === wf.active);
    const stage = $('#hxStage');
    if (stage) stage.textContent = active ? active.label : 'Starting';
    const item = $('#hxItem');
    if (item) item.textContent = run.current_item ? '→ ' + run.current_item : '';
    const stop = $('#hxStop');
    if (stop && run.stopping && !stop.disabled) { stop.disabled = true; stop.textContent = 'Stopping…'; }
  }
}

function heroHtml(wf, running) {
  const run = wf.run, up = wf.upcoming || [], next = up[0];
  const svc = k => esc(SERVICE_LABEL[k] || k || 'Agent');
  let main;
  if (running) {
    main = `
      <span class="pchip live"><span class="pd"></span>LIVE NOW</span>
      <h2 class="hx-title">${esc(run.campaign)}</h2>
      <div class="hx-meta"><span class="tag">${svc(run.service)}</span><span class="sep"></span>
        ${run.trigger === 'scheduled' ? 'Scheduled run' : 'Started by hand'}<span class="sep"></span>since ${fmtWhen(run.started_at)}</div>
      <div class="hx-live">
        <div class="hx-stage"><span>Current stage</span><b id="hxStage">Starting</b></div>
        <div class="hx-stage"><span>Running for</span><b id="hxElapsed" class="mono">00:00</b></div>
        <button class="hx-stop" id="hxStop" onclick="stopRun(${run.campaign_id})"${run.stopping ? ' disabled' : ''}>
          ${run.stopping ? 'Stopping…' : '■ Stop this run'}</button>
      </div>
      <div class="hx-item" id="hxItem"></div>`;
  } else if (next) {
    main = `
      <span class="pchip${wf.scheduler_enabled ? '' : ' off'}"><span class="pd"></span>${wf.scheduler_enabled ? 'NEXT AGENT' : 'SCHEDULER OFF'}</span>
      <h2 class="hx-title">${esc(next.name)}</h2>
      <div class="hx-meta"><span class="tag">${svc(next.service)}</span><span class="sep"></span>
        ${fmtWhen(next.at)}<span class="sep"></span>${esc(DAYS_LABEL[next.schedule_days] || next.schedule_days)}</div>
      ${wf.scheduler_enabled ? countdownHtml(next.at)
        : '<p class="hx-note">Scheduled runs are switched off (SCHEDULER_ENABLED=false), so this will not start by itself. Use Run agent.</p>'}`;
  } else {
    main = `
      <span class="pchip off"><span class="pd"></span>NO AGENT SCHEDULED</span>
      <h2 class="hx-title">Nothing queued</h2>
      <div class="hx-meta">Your agents run on their schedule. Switch one on in Campaigns, or unlock a bot.</div>
      <button class="btn go" style="margin-top:16px" onclick="go('campaigns')">Open agents</button>`;
  }

  const queue = up.length ? up.slice(0, 6).map((u, i) => `
      <button class="hx-q${i === 0 && !running ? ' first' : ''}"
              onclick="openCampaign(${u.campaign_id})" title="Edit ${esc(u.name)}">
        <span class="qd"></span>
        <span class="qn">${esc(u.name)}<small>${svc(u.service)}</small></span>
        <span class="qt">${fmtWhen(u.at)}<small class="js-in" data-at="${esc(u.at)}">${fmtIn(new Date(u.at) - Date.now())}</small></span>
      </button>`).join('') : '<div class="hx-empty">No agents are switched on.</div>';

  const cap = running ? '<b>Live numbers</b> — updating as the agent works'
    : run ? `<b>Last run · ${esc(run.campaign)}</b> — ${runEnded(run)} ${fmtWhen(run.finished_at || run.started_at)}. These stay until the next agent starts.`
    : '<b>No runs yet</b> — the numbers fill in when the first agent runs.';

  return `
    <section class="hx${running ? ' live' : ''}">
      <div class="hx-top">
        <div class="hx-main">${main}</div>
        <div class="hx-queue"><div class="hx-qh">Agent queue<span>${up.length} scheduled</span></div>${queue}</div>
      </div>
      <div class="hx-stats">
        <div class="hx-cap">${cap}</div>
        <div class="hx-tiles">${RUN_STATS.map(([k, label, icon], i) => `
          <div class="hx-t" style="--i:${i}">
            <span class="ti">${svgIcon(NODE_ICONS[icon])}</span>
            <b class="tv zero" data-k="${k}">0</b><span class="tk">${label}</span>
          </div>`).join('')}</div>
      </div>
    </section>`;
}

function countdownHtml(at) {
  const s = Math.max(0, Math.floor((new Date(at) - Date.now()) / 1000)), d = Math.floor(s / 86400);
  const unit = (u, v, label, hide) =>
    `<div class="cd-u"${hide ? ' hidden' : ''}><b data-u="${u}">${pad2(v)}</b><span>${label}</span></div>`;
  return `<div class="cd" id="wfCd" data-at="${esc(at)}" role="timer" aria-label="Time until the next agent starts">
    ${unit('d', d, 'days', !d)}${unit('h', Math.floor(s % 86400 / 3600), 'hours')}<i>:</i>${unit('m', Math.floor(s % 3600 / 60), 'min')}<i>:</i>${unit('s', s % 60, 'sec')}
  </div>`;
}

// Once a second: countdown, "in 3h 12m" labels and the elapsed clock. No API calls.
function tickWorkflow() {
  if (VIEW !== 'workflow' || !$('#wfHero')) { clearInterval(_wfTick); _wfTick = null; return; }
  const now = Date.now();
  document.querySelectorAll('#view .js-in').forEach(n => { n.textContent = fmtIn(new Date(n.dataset.at) - now); });
  const elapsed = $('#hxElapsed');
  if (elapsed && _wf && _wf.run) elapsed.textContent = fmtDur(now - new Date(_wf.run.started_at));

  const cd = $('#wfCd');
  if (!cd) return;
  const left = Math.floor((new Date(cd.dataset.at) - now) / 1000);
  if (left <= 0) {
    if (!cd.classList.contains('go')) {
      cd.classList.add('go');
      cd.innerHTML = '<span class="cd-go">Starting now…</span>';
      pollWorkflow(_wf);
    }
    return;
  }
  const set = (u, v) => {
    const b = cd.querySelector(`[data-u="${u}"]`);
    if (!b) return;
    if (u === 'd') b.parentElement.hidden = !v;
    const t = pad2(v);
    if (b.textContent !== t) { b.textContent = t; b.classList.remove('tick'); void b.offsetWidth; b.classList.add('tick'); }
  };
  set('d', Math.floor(left / 86400));
  set('h', Math.floor(left % 86400 / 3600));
  set('m', Math.floor(left % 3600 / 60));
  set('s', left % 60);
}

function paintFlow(wf, running) {
  const canvas = $('#wfCanvas');
  if (!canvas) return;
  const run = wf.run, nodes = wf.nodes || [];
  const key = run ? 'run' + run.id : 'none';
  if (key !== _flowKey || !canvas.querySelector('.wf')) {
    _flowKey = key;
    canvas.innerHTML = `
      <div class="wf">
        <div class="wf-head">
          <span id="wfChip"></span>
          <div><h3>Pipeline</h3><div class="wf-sub" id="wfSub"></div></div>
        </div>
        <div class="wf-rail" aria-hidden="true"><i></i></div>
        <div class="wf-flow">${nodes.map((n, i) => `
          ${i ? `<div class="conn" data-i="${i}" style="--c1:${tint(nodes[i - 1].key)};--c2:${tint(n.key)}"><span class="line"></span><b class="pkg"></b><b class="pkg"></b></div>` : ''}
          <div class="node" data-k="${esc(n.key)}" style="--i:${i};--c:${tint(n.key)}">
            <div class="nc">
              <svg class="ring" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="30" transform="rotate(-90 32 32)"/></svg>
              <span class="fx"><i></i><i></i><i></i></span>
              ${svgIcon(NODE_ICONS[n.icon] || NODE_ICONS.doc, 20)}
              <span class="okb">✓</span><span class="gain"></span>
              ${n.engine === 'llm' ? '<span class="eng">LLM</span>' : ''}
            </div>
            <div class="nname">${esc(n.label)}</div>
            <div class="nnote">${esc(n.note)}</div>
            <div class="nfoot"><b class="cnt zero">0</b><span class="nx"></span></div>
          </div>`).join('')}</div>
        <div class="wf-now" id="wfNow"></div>
        <div class="legend">
          <span><i style="background:var(--acc)"></i>Working now</span>
          <span><i style="background:#E8EEF4"></i>Done</span>
          <span><i class="lg-pkg"></i>A real prospect moving on — only while an agent runs</span>
          <span style="color:#E58E8E">✕ Rejected at this step</span>
          ${META.ai_ready ? '<span><b class="lg-llm">AI</b>Claude step, billed by Anthropic per use</span>'
            : '<span>Free mode: rules and templates, no paid AI</span>'}
        </div>
      </div>`;
  }

  const activeIdx = nodes.findIndex(n => n.key === wf.active);
  // The last step the latest run reached.
  let reach = -1;
  nodes.forEach((n, i) => { if (n.status === 'done' || n.status === 'error' || n.passed > 0) reach = i; });
  canvas.querySelector('.wf').classList.toggle('live', running);

  // Progress along the stages: up to the working step while live, and where
  // the last run got to otherwise.
  const rail = canvas.querySelector('.wf-rail i');
  const share = !run || !nodes.length ? 0 : running ? (Math.max(activeIdx, 0) + .5) / nodes.length
    : (reach + 1) / nodes.length;
  if (rail) rail.style.width = (share * 100).toFixed(1) + '%';

  // Real progress only: compare each step's count with the previous poll of
  // the same run, and animate the steps whose count went up.
  const prev = canvas._runId === (run && run.id) ? canvas._counts || null : null;
  canvas._runId = run && run.id;
  canvas._counts = Object.fromEntries(nodes.map(n => [n.key, n.passed || 0]));
  let burst = 0;

  nodes.forEach((n, i) => {
    const node = canvas.querySelector(`.node[data-k="${n.key}"]`);
    if (!node) return;
    const keep = node.classList.contains('work') ? ' work' : '';
    const status = !running && n.status === 'running' ? (n.passed ? 'done' : 'idle') : n.status;
    node.className = `node ${status}${running && n.key === wf.active ? ' running' : ''}${keep}`;
    node.title = n.note;
    animateNumber(node.querySelector('.cnt'), n.passed || 0);
    setHtml(node.querySelector('.nx'), n.dropped ? `<span class="drop" title="rejected here">✕ ${n.dropped}</span>` : '');
    const conn = i && canvas.querySelector(`.conn[data-i="${i}"]`);
    if (conn) {
      // While live, packages keep flowing into the step that is working.
      const state = running && i === activeIdx ? ' flow' : i <= reach && nodes[i - 1].passed > 0 ? ' past' : '';
      conn.className = `conn${state}${conn.classList.contains('send') ? ' send' : ''}`;
    }
    // Includes the poll where the run ends, so the last burst of work plays
    // too; steps that moved together play one after another, in pipeline order.
    const gained = prev ? (n.passed || 0) - (prev[n.key] || 0) : 0;
    if (gained > 0 && !REDUCED_MOTION) pulseStep(canvas, node, i, gained, 380 * burst++);
  });

  setHtml($('#wfChip'), !run ? '<span class="wf-live idle"><span class="bl"></span>NO RUNS YET</span>'
    : running ? '<span class="wf-live run"><span class="bl"></span>RUNNING</span>'
    : run.status === 'failed' ? '<span class="wf-live fail"><span class="bl"></span>FAILED</span>'
    : run.status === 'cancelled' ? '<span class="wf-live idle"><span class="bl"></span>STOPPED</span>'
    : '<span class="wf-live done"><span class="bl"></span>COMPLETED</span>');
  setHtml($('#wfSub'), run
    ? `${esc(run.campaign)} · ${esc(run.trigger)} · started ${fmtTime(run.started_at)}${run.stats && run.stats.ai_cost_usd ? ' · $' + run.stats.ai_cost_usd.toFixed(3) + ' AI' : ''}`
    : 'The stages light up as soon as an agent starts.');

  // Where "Idle" used to be: what is happening now, or which agent is next.
  const next = (wf.upcoming || [])[0];
  let now;
  if (running) {
    const active = nodes.find(n => n.key === wf.active);
    now = `<span class="spin" style="border-top-color:#22D3EE;border-color:rgba(34,211,238,.3)"></span>
      <div style="min-width:0"><div><b>${esc(active ? active.label : 'Starting')}</b>${run.current_item ? ' — ' + esc(run.current_item) : ''}</div>
      </div>`;
  } else if (next) {
    now = `<span class="nx-ico">${svgIcon(NODE_ICONS.rocket, 15)}</span>
      <div style="min-width:0"><div>Next up: <b>${esc(next.name)}</b> · ${fmtWhen(next.at)} ·
        <span class="js-in" data-at="${esc(next.at)}">${fmtIn(new Date(next.at) - Date.now())}</span>${wf.scheduler_enabled ? '' : ' (scheduler off)'}</div>
      ${run ? `<div class="wf-detail">Last run: ${esc(run.campaign)} ${runEnded(run)} ${fmtTime(run.finished_at || run.started_at)}</div>` : ''}</div>`;
  } else {
    now = `<span class="nx-ico">${svgIcon(NODE_ICONS.rocket, 15)}</span>
      <div>No agent is scheduled. Switch one on under <b>Campaigns</b>.</div>`;
  }
  setHtml($('#wfNow'), now);
}

// One real event: prospects reached this step since the last poll. A package
// travels in along the connector, the step plays its animation and a "+N"
// floats up from it.
function pulseStep(canvas, node, i, gained, delay = 0) {
  const conn = i && canvas.querySelector(`.conn[data-i="${i}"]`);
  const land = () => {
    flash(node, 'work', 1300);
    const g = node.querySelector('.gain');
    if (g) { g.textContent = '+' + gained.toLocaleString(); flash(g, 'show', 1500); }
  };
  setTimeout(() => {
    if (conn) { flash(conn, 'send', 850); setTimeout(land, 650); } else land();
  }, delay);
}

// Put a class on just long enough for its animation to play once, restarting
// the animation if the class was already there.
function flash(el, cls, ms) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  clearTimeout(el['_' + cls]);
  el['_' + cls] = setTimeout(() => el.classList.remove(cls), ms);
}

function renderDrops(nodes) {
  const box = $('#wfDrops');
  if (!box) return;
  const drops = nodes.filter(n => n.dropped > 0);
  const total = drops.reduce((a, n) => a + n.dropped, 0);
  if (!total) { setHtml(box, '<p class="hint" style="margin:0">Nothing has been rejected yet.</p>'); return; }
  const max = Math.max(...drops.map(n => n.dropped));
  setHtml(box, drops.map(n => `
    <div style="margin-bottom:9px">
      <div class="row" style="justify-content:space-between;font-size:12.5px;margin-bottom:3px">
        <span>${esc(n.label)}</span><b class="mono">${n.dropped}</b></div>
      <div class="bar drop"><i style="width:${Math.round(n.dropped / max * 100)}%"></i></div>
    </div>`).join(''));
}

/* ------------------------------------------------------------- dashboard */

VIEWS.dashboard = async function () {
  const [s, byService, bySource, byIntent] = await Promise.all([
    api('/api/stats/summary'), api('/api/stats/breakdown?by=service'),
    api('/api/stats/breakdown?by=source'), api('/api/stats/breakdown?by=intent'),
  ]);
  window._pending = s.pending_approval;

  const n = v => (+v || 0).toLocaleString();
  const pct = (a, b) => b ? Math.round(a / b * 100) + '%' : '—';
  const none = msg => `<p class="muted" style="font-size:12.5px;margin:0">${msg}</p>`;

  // One series, so one colour; each bar is measured against the first step.
  const steps = [
    ['Leads found', s.total_leads, `${n(s.companies)} companies`],
    ['With an email', s.with_email, `${pct(s.with_email, s.total_leads)} of leads`],
    ['Emails written', s.pending_approval + s.queued + s.emails_sent, `${n(s.pending_approval)} waiting for you`],
    ['Sent', s.emails_sent, `${n(s.sent_today)} today`],
    ['Replied', s.replies, `${pct(s.replies, s.emails_sent)} of sent`],
    ['Interested', s.interested, 'replied yes'],
  ];
  const top = Math.max(1, ...steps.map(x => +x[1] || 0));
  const funnel = `<div class="fun">${steps.map(([label, v, sub]) => `
    <div class="fun-row bar-hit" data-tip="${esc(label)}: ${n(v)}">
      <div class="lbl">${label}<small>${sub}</small></div>
      <div class="fun-bar"><i style="--p:${(+v || 0) / top}"></i><b>${n(v)}</b></div>
    </div>`).join('')}</div>`;

  // Ordered classes, so an ordered ramp of the brand hue (validated light to dark).
  const iv = Object.fromEntries(byIntent.map(r => [r.key, r.count]));
  const INTENT = [['high', 'High intent', 'var(--int-high)'], ['medium', 'Medium', 'var(--int-med)'], ['low', 'Low', 'var(--int-low)']];
  const scored = INTENT.reduce((a, [k]) => a + (iv[k] || 0), 0);
  const mix = scored ? `
    <p class="mix-h"><b>${pct(iv.high || 0, scored)}</b>of scored leads show high intent</p>
    <div class="mix" role="img" aria-label="${INTENT.map(([k, l]) => `${l} ${iv[k] || 0}`).join(', ')}">${INTENT.filter(([k]) => iv[k]).map(([k, l, c]) =>
      `<i class="bar-hit" style="flex:${iv[k]};background:${c}" data-tip="${l}: ${iv[k]} (${pct(iv[k], scored)})"></i>`).join('')}</div>
    <div class="legend-rows">${INTENT.map(([k, l, c]) => `
      <div><span class="swt" style="background:${c}"></span><span>${l}</span><b>${n(iv[k])}</b><span class="pct">${pct(iv[k] || 0, scored)}</span></div>`).join('')}</div>`
    : none('No scored leads yet.');

  const blist = (rows, label, icon) => {
    if (!rows.length) return none('No data yet.');
    const max = Math.max(1, ...rows.map(r => r.count)), total = rows.reduce((a, r) => a + r.count, 0);
    return `<div class="blist">${rows.map(r => `
      <div class="bar-hit" data-tip="${esc(label(r.key))}: ${r.count} (${pct(r.count, total)})">
        <div class="bl-top">${icon ? `<span class="mini-ico">${svgIcon(icon(r.key), 13)}</span>` : ''}<span>${esc(label(r.key))}</span>
          <b>${n(r.count)}</b><small>${pct(r.count, total)}</small></div>
        <div class="bl-bar"><i style="width:${Math.round(r.count / max * 100)}%"></i></div>
      </div>`).join('')}</div>`;
  };

  const CHAT = 'M21 11.5a8.4 8.4 0 01-12 7.6L3 21l1.9-5.7A8.4 8.4 0 1121 11.5z';
  $('#view').innerHTML = `
    <div class="head">
      <div><h2>Dashboard</h2>
        <p>Every number here counts real records. Leads only exist where the system found
           verifiable evidence of a reason to make contact.</p></div>
      <div class="row"><button class="btn" onclick="go('leads')">All leads</button>
        <button class="btn primary" onclick="go('outbox')">Review outbox${s.pending_approval ? ` (${n(s.pending_approval)})` : ''}</button></div>
    </div>

    <div class="kpis k10">
      ${kpi('Total leads', n(s.total_leads), { sub: `${n(s.companies)} companies`, icon: NODE_ICONS.gauge, i: 0 })}
      ${kpi('New today', n(s.new_today), { sub: 'found since midnight', icon: NODE_ICONS.plus, tone: 'slate', i: 1 })}
      ${kpi('High intent', n(s.high_intent), { sub: 'score 70+', icon: NODE_ICONS.trigger, tone: 'gold', i: 2 })}
      ${kpi('Awaiting approval', n(s.pending_approval), { sub: 'in your Outbox', icon: NODE_ICONS.inbox, tone: 'gold', act: s.pending_approval > 0, i: 3 })}
      ${kpi('Emails sent', n(s.emails_sent), { sub: `${n(s.sent_today)} today of ${n(s.daily_cap)} cap`, icon: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z', i: 4 })}
      ${kpi('Replies', n(s.replies), { sub: `${pct(s.replies, s.emails_sent)} of emails sent`, icon: CHAT, tone: 'good', i: 5 })}
      ${kpi('Interested', n(s.interested), { sub: 'replied yes', icon: 'M7 10v11M15 5.9L14 10h5.8a2 2 0 012 2.3l-1.4 7A2 2 0 0118.4 21H7V10l4-8a2.5 2.5 0 013 3.9z', tone: 'good', i: 6 })}
      ${kpi('WhatsApp queued', n(s.whatsapp_pending), { sub: 'one tap each to send', icon: CHAT, tone: 'slate', i: 7 })}
      ${kpi('Unsubscribed', n(s.unsubscribed), { sub: 'never emailed again', icon: NODE_ICONS.x, tone: 'slate', i: 8 })}
      ${kpi('AI spend', '$' + (s.ai_cost_usd || 0).toFixed(2), { sub: 'all time', icon: NODE_ICONS.brain, tone: 'violet', i: 9 })}
    </div>

    <div class="dash-grid">
      <div class="card">${cardHead('M3 4h18l-7 9v6l-4 2v-8L3 4z', 'From lead to reply', 'How far your leads have got, step by step')}${funnel}</div>
      <div class="card">${cardHead(NODE_ICONS.gauge, 'Intent mix', 'How strong each lead\'s reason to buy is')}${mix}</div>
    </div>
    <div class="grid2">
      <div class="card">${cardHead(NODE_ICONS.rocket, 'Leads by service')}${blist(byService, k => SERVICE_LABEL[k] || k, svcIcon)}</div>
      <div class="card">${cardHead(NODE_ICONS.search, 'Leads by source')}${blist(bySource, k => SOURCE_LABEL[k] || k)}</div>
    </div>`;
  wireTips($('#view'));
};

/* ---------------------------------------------------------------- outbox */

VIEWS.outbox = async function () {
  const [msgs, sum] = await Promise.all([
    api('/api/outbox?status=pending_approval'), api('/api/outbox/summary'),
  ]);
  window._pending = msgs.length;
  const pc = sum.pc_sender;
  const remote = !sum.send_on_server;

  const warn = [];
  if (!META.email_ready && !remote) warn.push('SMTP is not configured, so nothing can actually be sent yet. Add SMTP settings to your .env file.');
  if (!META.sender_configured) warn.push('SENDER_EMAIL and COMPANY_POSTAL_ADDRESS must both be set before sending — the postal address is required by US CAN-SPAM law.');

  $('#view').innerHTML = `
    <div class="head">
      <div><h2>Outbox</h2>
      <p>Each draft sits beside the evidence it was written from. Check the claim is
         true, then approve. Highlighted evidence is what the email actually cites.</p></div>
      <div class="row">
        <button class="btn" onclick="go('outbox')">Refresh</button>
        ${msgs.length ? `<button class="btn primary" id="sendAll">${remote ? 'Approve' : 'Approve &amp; send'} all (${msgs.length})</button>` : ''}
      </div>
    </div>
    ${warn.map(w => `<div class="banner">${esc(w)}</div>`).join('')}
    ${remote || sum.queued || sum.failed ? `
    <div class="card sendbar">
      ${remote ? `<span class="pcdot ${pc.alive ? 'on' : ''}"></span>
        <div style="flex:1;min-width:220px">
          <b>${pc.alive ? 'Your computer is sending' : 'Your computer sender is not running'}</b>
          <div class="muted" style="font-size:12.5px">${pc.alive
            ? `Checked in ${fmtWhen(pc.last_seen)}${pc.host ? ' from ' + esc(pc.host) : ''}. Approved emails go out within a minute.`
            : `Approved emails wait here until you start it. On your computer run <code>python run.py --send-only</code>${pc.last_seen ? ` · last seen ${fmtWhen(pc.last_seen)}` : ''}.`}</div>
        </div>` : '<div style="flex:1"></div>'}
      <span class="tag grey">${sum.queued} queued</span>
      ${sum.failed ? `<span class="pill failed">${sum.failed} failed</span>
        <button class="btn sm" onclick="retryFailed()">Retry failed</button>` : ''}
    </div>` : ''}
    <div id="obList">${msgs.length ? msgs.map(obCard).join('') : `
      <div class="empty"><h3>Nothing waiting for approval</h3>
      <p>Run a campaign to generate drafts, or draft one from a lead.</p></div>`}</div>`;

  if ($('#sendAll')) $('#sendAll').onclick = async () => {
    if (!confirm(`Approve ${remote ? '' : 'and send '}all ${msgs.length} emails?\n\n${remote
      ? 'Your computer sender will send them.' : 'They will go out immediately.'}`)) return;
    let ok = 0, fail = 0;
    for (const m of msgs) {
      try { const r = await post(`/api/outbox/${m.id}/approve?send_now=true`); r.ok ? ok++ : fail++; }
      catch (e) { fail++; }
    }
    toast(`${remote ? 'Approved' : 'Sent'} ${ok}${fail ? `, ${fail} failed` : ''}`, fail ? 'bad' : 'good');
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
        <div class="ob-label">Evidence on file</div>
        ${ev}
      </div>
    </div></div>`;
}

async function retryFailed() {
  try {
    const r = await post('/api/outbox/retry-failed');
    toast(`${r.requeued} email${r.requeued === 1 ? '' : 's'} back in the queue`, 'good');
    go('outbox');
  } catch (e) { toast(e.message, 'bad'); }
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
    if (r.ok) {
      toast(r.status === 'sent' ? 'Sent'
        : r.detail && r.detail !== 'queued' ? 'Approved and queued. This server cannot send email, so start the sender on your computer.'
        : 'Approved — your computer sender will send it', 'good');
      const c = $('#ob' + id); if (c) c.remove();
    }
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

const SERVICE_ICON = {
  web_development: NODE_ICONS.globe,
  computer_vision: 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 15a3 3 0 100-6 3 3 0 000 6z',
  ai_agent: NODE_ICONS.brain,
  ai_automation: NODE_ICONS.trigger,
  digital_marketing: 'M3 11l18-8v18l-18-8zM11 13v6a2 2 0 01-4 0v-7',
};
const svcIcon = k => SERVICE_ICON[k] || NODE_ICONS.rocket;

let _campCache = [];

VIEWS.campaigns = async function () {
  const list = _campCache = await api('/api/campaigns');

  $('#view').innerHTML = `
    <div class="head"><div><h2>Campaigns</h2>
      <p>Each bot comes with its own agent. Click a card to change its start time, countries and
         keywords within what your plan allows. Nothing is emailed when an agent runs — approved
         mail goes out through the dispatcher, which runs every 10 minutes.</p></div>
      <div class="row">
        ${META.send_on_server === false ? '' : '<button class="btn" onclick="dispatchNow()">Send queued now</button>'}
      </div>
    </div>
    ${list.length ? `<div class="camps">${list.map(campCard).join('')}</div>` : `
      <div class="card empty"><h3>No agents yet</h3>
        <p>Your agent appears as soon as you unlock a bot.</p>
        <button class="btn primary" onclick="go('bots')">Choose a bot</button></div>`}`;

  // The whole card opens the editor, except the controls inside it.
  $('#view').querySelectorAll('.camp').forEach(card => {
    const open = () => openCampaign(+card.dataset.id);
    card.onclick = e => { if (!e.target.closest('button, input, label, select, a')) open(); };
    card.onkeydown = e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === card) { e.preventDefault(); open(); }
    };
  });
};

function campCard(c, i) {
  const src = Object.fromEntries((META.sources || []).map(s => [s.key, s]));
  const names = META.country_policy || {};
  const countries = c.countries || [];
  const st = c.last_run && c.last_run.stats;
  return `
    <div class="camp${c.enabled ? '' : ' off'}" data-id="${c.id}" tabindex="0" role="button"
         aria-label="Edit ${esc(c.name)}" style="--i:${i}">
      <div class="camp-top">
        <span class="camp-ico">${svgIcon(svcIcon(c.service_category), 18)}</span>
        <div class="camp-name"><b>${esc(c.name)}</b><small>${esc(SERVICE_LABEL[c.service_category] || c.service_category)}${c.locked ? ' · <span class="lockt">locked — subscription ended</span>' : ''}</small></div>
        <label class="sw" title="${c.enabled ? 'On — click to pause' : 'Paused — click to switch on'}">
          <input type="checkbox" ${c.enabled ? 'checked' : ''} onchange="toggleCampaign(${c.id}, this.checked)" aria-label="Enabled"><i></i></label>
      </div>
      <div class="camp-when">
        ${svgIcon(NODE_ICONS.clock)} ${esc(DAYS_LABEL[c.schedule_days] || c.schedule_days)} · ${esc(c.schedule_time)}
        ${c.running ? '<span class="nx live">Running now</span>'
          : c.enabled && c.next_run ? `<span class="nx">Next: ${fmtWhen(c.next_run)}</span>`
          : '<span class="nx off">Paused</span>'}
      </div>
      <div class="camp-chips">${countries.slice(0, 8).map(k =>
        `<span class="tag" title="${esc((names[k] || {}).name || k)}">${esc(k)}</span>`).join('')}${
        countries.length > 8 ? `<span class="tag grey">+${countries.length - 8}</span>` : ''}</div>
      <div class="camp-chips">${(c.sources || []).length ? c.sources.map(s => {
          const a = src[s];
          return `<span class="pill ${a && a.available ? 'sent' : 'rejected'}" title="${a && a.available ? 'ready' : 'API key missing'}">${esc(SOURCE_LABEL[s] || s)}</span>`;
        }).join('') : '<span class="pill scored">All available sources</span>'}</div>
      <div class="camp-stats">
        <div><b>${c.total_leads}</b><span>leads</span></div>
        <div><b>${st ? st.drafted || 0 : '—'}</b><span>drafted last run</span></div>
        <div><b>${c.min_score}</b><span>min score</span></div>
        <div><b>${c.approval_mode === 'auto' ? 'Auto' : 'Manual'}</b><span>approval</span></div>
      </div>
      <div class="camp-foot">
        ${c.last_run ? `<span class="muted">Last run ${fmtTime(c.last_run.started_at)}</span>` : '<span class="muted">Never run</span>'}
        <span class="sp"></span>
        <button class="btn sm" onclick="openCampaign(${c.id})">Edit</button>
        ${c.locked
          ? `<button class="btn sm primary" onclick="go('bots')">🔒 Unlock bot</button>`
          : c.running
          ? `<button class="btn sm danger" ${c.stopping ? 'disabled' : ''} onclick="stopRun(${c.id})">${c.stopping ? '<span class="spin dark"></span> Stopping…' : '■ Stop'}</button>`
          : `<button class="btn sm primary" onclick="runCampaign(${c.id})">▶ Run now</button>`}
      </div>
    </div>`;
}

async function updateCampaign(id, changes) {
  const c = _campCache.find(x => x.id === id) || (await api('/api/campaigns')).find(x => x.id === id);
  if (!c) return;
  const body = {
    name: c.name, service_category: c.service_category, sources: c.sources,
    keywords: c.keywords, locations: c.locations, countries: c.countries,
    schedule_time: c.schedule_time, schedule_days: c.schedule_days, enabled: c.enabled,
    rotate_cities: c.rotate_cities,
    daily_lead_limit: c.daily_lead_limit, daily_email_limit: c.daily_email_limit,
    approval_mode: c.approval_mode, min_score: c.min_score, ...changes,
  };
  await api(`/api/campaigns/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  toast('Saved', 'good');
}
const toggleCampaign = (id, v) => updateCampaign(id, { enabled: v })
  .then(() => { if (VIEW === 'campaigns') VIEWS.campaigns(); })
  .catch(e => toast(e.message, 'bad'));


/* --------------------------------------------------------------- services */

// What the agent will look for, shown under the service picker so nobody has to guess.
function ceServiceHint() {
  const box = $('#ceSvcHint');
  if (!box) return;
  const key = $('#ceService').value, s = _svcs.find(x => x.key === key);
  if (!s) { box.innerHTML = ''; return; }
  const kws = s.keywords || [];
  box.innerHTML = `
    <b>Searches for:</b> ${esc(kws.slice(0, 6).join(', '))}${kws.length > 6 ? ` <span class="muted">+${kws.length - 6} more</span>` : ''}
    ${s.builtin ? '<span class="tag grey">built in</span>'
      : `<button type="button" class="linkbtn" onclick="serviceFromEditor('${esc(s.key)}')">Edit this service</button>`}`;
}

// Open the service form from inside the agent editor without losing what was typed there.
function serviceFromEditor(key) {
  Object.assign(_ce, ceRead());
  openServiceForm(key, 'editor');
}

let _sf = null;  // the service open in the form

function openServiceForm(key, back) {
  const d = $('#drawer');
  d.classList.add('on'); $('#drawerBg').classList.add('on');
  const s = key ? _svcs.find(x => x.key === key) : null;
  if (key && !s) { toast('That service no longer exists', 'bad'); return; }
  _sf = { key: key || null, back };
  const isNew = !key;
  d.innerHTML = `
    <div class="ce-head">
      <span class="ce-ico">${svgIcon(isNew ? NODE_ICONS.plus : svcIcon(key), 20)}</span>
      <div style="min-width:0"><span class="ce-kicker">${isNew ? 'New service' : 'Edit service'}</span>
        <h2>${isNew ? 'Add a service' : esc(s.label)}</h2></div>
      <span class="sp"></span><button class="btn ghost" onclick="serviceBack()">Back</button>
    </div>
    <div class="ce-sec">
      <p class="fine" style="margin:0 0 14px;font-size:12.5px;line-height:1.55">A service is something you sell. Tell the agent what it is and
        what to look for, and it finds businesses with a provable reason to need it, then writes emails about it.</p>
      <div class="inp"><label for="sfLabel">Name</label>
        <input id="sfLabel" maxlength="80" value="${esc(s ? s.label : '')}" placeholder="e.g. Payroll &amp; HR software"></div>
      <div class="inp" style="margin-top:14px"><label for="sfPitch">What you sell</label>
        <textarea id="sfPitch" rows="3" maxlength="600" style="min-height:84px;font-family:inherit;font-size:13px"
          placeholder="e.g. Cloud payroll and HR software for small and mid-sized businesses">${esc(s ? s.pitch : '')}</textarea>
        <span class="fine">One or two sentences. The email writer uses this to explain what you offer.</span></div>
    </div>
    <div class="ce-sec">
      <div class="inp"><label for="sfKeywords">Keywords <span class="req">required</span></label>
        <input id="sfKeywords" value="${esc(s ? s.keywords.join(', ') : '')}" placeholder="payroll software, HR software, employee onboarding">
        <span class="fine">What the agent searches for in job ads, public tenders and posts. Separate with commas.</span></div>
      <div class="inp" style="margin-top:14px"><label for="sfPhrases">What buyers say when they need it <span class="opt">optional</span></label>
        <textarea id="sfPhrases" rows="3" style="min-height:84px;font-family:inherit;font-size:13px"
          placeholder="looking for a payroll provider&#10;need HR software">${esc(s ? s.intent_phrases.join('\n') : '')}</textarea>
        <span class="fine">One phrase per line, as someone would write it in a post. Leave empty and the agent builds “looking for …” and “need …” from your keywords.</span></div>
      <div class="inp" style="margin-top:14px"><label for="sfIndustries">Businesses that buy this <span class="opt">optional</span></label>
        <input id="sfIndustries" value="${esc(s ? s.industries.join(', ') : '')}" placeholder="accounting, recruitment, retail">
        <span class="fine">Kinds of business, separated by commas. Leads in these sectors score higher, and the agent searches for them on the map.</span></div>
    </div>
    <div class="ce-foot">
      <button class="btn primary" id="sfSave" onclick="saveService()">${isNew ? 'Add service' : 'Save service'}</button>
      <button class="btn" onclick="serviceBack()">Cancel</button>
      <span class="sp"></span>
      ${isNew ? '' : `<button class="btn danger" onclick="deleteService('${esc(key)}')">Delete</button>`}
    </div>`;
  d.scrollTop = 0;
  if (isNew) $('#sfLabel').focus();
}

function serviceBack() {
  if (_sf && _sf.back === 'editor' && _ce) paintCampaignEditor();
  else openServices();
}

async function saveService() {
  const split = (id, sep) => $(id).value.split(sep).map(x => x.trim()).filter(Boolean);
  const body = {
    label: $('#sfLabel').value.trim(), pitch: $('#sfPitch').value.trim(),
    keywords: split('#sfKeywords', /[,\n]/), intent_phrases: split('#sfPhrases', /\n/),
    industries: split('#sfIndustries', /[,\n]/),
  };
  if (body.label.length < 2) { toast('Give the service a name', 'bad'); $('#sfLabel').focus(); return; }
  if (body.pitch.length < 10) { toast('Describe what you sell in a sentence', 'bad'); $('#sfPitch').focus(); return; }
  if (!body.keywords.length) { toast('Add at least one keyword', 'bad'); $('#sfKeywords').focus(); return; }

  const btn = $('#sfSave');
  btn.disabled = true;
  try {
    let key = _sf.key;
    if (key) {
      await api(`/api/services/${key}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
    } else {
      key = (await post('/api/services', body)).key;
    }
    await refreshServices();
    _svcs = await api('/api/services');
    toast(_sf.key ? 'Service saved' : 'Service added', 'good');
    if (_sf.back === 'editor' && _ce) { _ce.service_category = key; paintCampaignEditor(); }
    else openServices();
  } catch (e) {
    toast(e.message, 'bad');
    btn.disabled = false;
  }
}

async function deleteService(key) {
  const s = _svcs.find(x => x.key === key);
  if (!s || !confirm(`Delete the service "${s.label}"?\n\nLeads already found for it are kept.`)) return;
  try {
    await api(`/api/services/${key}`, { method: 'DELETE' });
    await refreshServices();
    _svcs = await api('/api/services');
    if (_ce && _ce.service_category === key) _ce.service_category = 'web_development';
    toast('Service deleted', 'good');
    serviceBack();
  } catch (e) { toast(e.message, 'bad'); }
}

async function openServices() {
  const d = $('#drawer');
  d.classList.add('on'); $('#drawerBg').classList.add('on');
  d.innerHTML = '<div class="empty">Loading…</div>';
  try { _svcs = await api('/api/services'); } catch (e) { d.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  _sf = null;
  d.innerHTML = `
    <div class="ce-head">
      <span class="ce-ico">${svgIcon(NODE_ICONS.rocket, 20)}</span>
      <div style="min-width:0"><span class="ce-kicker">What you sell</span><h2>Services</h2></div>
      <span class="sp"></span><button class="btn ghost" onclick="closeDrawer()">Close</button>
    </div>
    <p class="fine" style="margin:0 0 14px;font-size:12.5px">Every agent sells one service. The four built-in ones come ready to use; add your own for anything else.</p>
    <button class="btn primary" onclick="openServiceForm(null, 'list')" style="margin-bottom:14px">${svgIcon(NODE_ICONS.plus)} Add a service</button>
    ${_svcs.map(s => `
      <div class="svc-card">
        <span class="camp-ico" style="width:36px;height:36px">${svgIcon(svcIcon(s.key), 16)}</span>
        <div style="flex:1;min-width:0">
          <div class="row" style="gap:8px"><b>${esc(s.label)}</b>${s.builtin ? '<span class="tag grey">built in</span>' : ''}
            <span class="muted" style="font-size:11.5px">${s.campaigns} ${s.campaigns === 1 ? 'agent' : 'agents'}</span></div>
          <div class="muted" style="font-size:12px;margin-top:3px">${esc((s.keywords || []).slice(0, 6).join(', '))}${(s.keywords || []).length > 6 ? '…' : ''}</div>
        </div>
        ${s.builtin ? '' : `<button class="btn sm" onclick="openServiceForm('${esc(s.key)}', 'list')">Edit</button>`}
      </div>`).join('')}`;
  d.scrollTop = 0;
}

/* -------------------------------------------------------- campaign editor */

let _ce = null;  // the campaign open in the editor; id null while creating
let _svcs = [];  // /api/services - what each service searches for

async function openCampaign(id) {
  if (!id) { toast('Agents come with your bots, so you can\'t create new ones. Unlock a bot to get its agent.', 'bad'); return; }
  const d = $('#drawer');
  d.classList.add('on'); $('#drawerBg').classList.add('on');
  d.innerHTML = '<div class="empty">Loading…</div>';
  let c = null;
  try { _svcs = await api('/api/services'); } catch (e) { _svcs = []; }
  if (id) {
    try { c = (await api('/api/campaigns')).find(x => x.id === id); }
    catch (e) { d.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
    if (!c) { closeDrawer(); toast('That campaign no longer exists', 'bad'); return; }
  }
  _ce = c ? { ...c } : {
    id: null, name: '', service_category: (isAdmin() ? null : Object.keys(META.access || {})[0]) || 'web_development', enabled: true,
    schedule_time: '07:00', schedule_days: 'mon-fri', approval_mode: 'manual',
    countries: ['GB', 'US'], rotate_cities: true, locations: [], keywords: [], sources: [],
    min_score: 55, daily_lead_limit: 25, daily_email_limit: 20,
  };
  paintCampaignEditor();
}

const seg = (id, opts, val) => `<div class="seg" id="${id}" role="group">${Object.entries(opts).map(([v, l]) =>
  `<button type="button" data-v="${esc(v)}" class="${v === val ? 'on' : ''}" aria-pressed="${v === val}">${esc(l)}</button>`).join('')}</div>`;

const chk = (value, label, on, sub = '', dim = false) =>
  `<button type="button" class="chk${on ? ' on' : ''}${dim ? ' dim' : ''}" data-v="${esc(value)}" aria-pressed="${on}">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ''}</button>`;

function paintCampaignEditor() {
  const c = _ce;
  // The bot sets what its agent may do; the editor only offers what fits.
  const limits = ((META.bots || []).find(b => b.key === c.service_category) || {}).agent
    || { countries: Object.keys(META.country_policy || {}), sources: [], schedule_days: c.schedule_days, leads_per_run: 500, min_score: 0 };
  const myBot = ((META.bots || []).find(b => b.key === c.service_category) || {}).name
    || SERVICE_LABEL[c.service_category] || c.service_category;
  const sec = (n, title, body) => `<div class="ce-sec" style="--i:${n}"><h4><span>${n}</span>${title}</h4>${body}</div>`;

  $('#drawer').innerHTML = `
    <div class="ce-head" id="ceHead">
      <span class="ce-ico">${svgIcon(svcIcon(c.service_category), 20)}</span>
      <div style="min-width:0"><span class="ce-kicker">Edit agent</span>
        <h2>${esc(c.name)}</h2></div>
      <span class="sp"></span><button class="btn ghost" onclick="closeDrawer()">Close</button>
    </div>

    ${sec(1, 'Basics', `
      <div class="grid2">
        <div class="inp"><label for="ceName">Name</label>
          <input id="ceName" value="${esc(c.name)}" maxlength="120" placeholder="e.g. UK restaurants — websites"></div>
        <div class="inp"><label for="ceService">Bot</label>
          <select id="ceService" disabled><option value="${esc(c.service_category)}" selected>${esc(myBot)}</option></select>
          <span class="fine">Each agent stays with the bot it came with.</span></div>
      </div>
      <div class="svc-hint" id="ceSvcHint"></div>
      <label class="ce-sw"><span class="sw"><input type="checkbox" id="ceEnabled" ${c.enabled ? 'checked' : ''}><i></i></span>
        Enabled — runs automatically on its schedule</label>`)}

    ${sec(2, 'Schedule', `
      <div class="grid2">
        <div class="inp"><label for="ceTime">Start time${META.timezone ? ` (${esc(META.timezone)})` : ''}</label>
          <input type="time" id="ceTime" value="${esc(c.schedule_time)}"></div>
        <div class="inp"><label>Days</label>
          <div style="pointer-events:none;opacity:.75">${seg('ceDays', DAYS_LABEL, limits.schedule_days)}</div>
          <span class="fine">Set by your plan: ${esc(DAYS_LABEL[limits.schedule_days] || limits.schedule_days)}.</span></div>
      </div>
      <div class="inp" style="margin-top:14px"><label>Approval</label>
        ${seg('ceApproval', { manual: 'Manual — I approve each email', auto: 'Auto-send' }, c.approval_mode)}
        <span class="fine">Manual holds every draft in the Outbox until you approve it. Auto-send queues drafts straight for sending.</span></div>`)}

    ${sec(3, 'Where to look', `
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <span class="ce-lbl">Countries</span>
        <span><button type="button" class="btn sm ghost" onclick="ceAllCountries(true)">Select all</button>
          <button type="button" class="btn sm ghost" onclick="ceAllCountries(false)">Clear</button></span>
      </div>
      <div class="chips" id="ceCountries">${Object.entries(META.country_policy || {})
        .filter(([code]) => limits.countries.includes(code)).map(([code, p]) =>
        chk(code, p.name || code, (c.countries || []).includes(code), code)).join('')}</div>
      <span class="fine" style="display:block;margin:10px 0 12px">Your plan covers these countries. Pick the ones this agent should search.</span>
      <label class="ce-sw"><span class="sw"><input type="checkbox" id="ceRotate" ${c.rotate_cities ? 'checked' : ''} onchange="ceRotateChanged()"><i></i></span>
        Rotate cities — search new cities from these countries every run</label>
      <div class="inp" style="margin-top:12px"><label for="ceLocations">Locations</label>
        <input id="ceLocations" value="${esc((c.locations || []).join(', '))}" placeholder="London, Manchester" ${c.rotate_cities ? 'disabled' : ''}>
        <span class="fine" id="ceLocHint"></span></div>
      <div class="inp" style="margin-top:12px"><label for="ceKeywords">Keywords</label>
        <input id="ceKeywords" value="${esc((c.keywords || []).join(', '))}" placeholder="Leave empty to use the built-in keywords for this service">
        <span class="fine">Comma-separated search phrases, e.g. new website, online booking system.</span></div>`)}

    ${sec(4, 'Lead sources', `
      <p class="fine" style="margin:0 0 10px">Where this agent looks is set by your plan.</p>
      <div class="camp-chips">${(limits.sources || []).length ? limits.sources.map(k =>
        `<span class="pill sent">${esc(SOURCE_LABEL[k] || k)}</span>`).join('') : '<span class="pill scored">All available sources</span>'}</div>`)}

    ${sec(5, 'Quality &amp; volume', `
      <div class="grid2">
        <div class="inp"><label for="ceScore">Minimum score to draft: <b id="ceScoreV">${Math.max(c.min_score, limits.min_score)}</b></label>
          <input type="range" id="ceScore" min="${limits.min_score}" max="100" value="${Math.max(c.min_score, limits.min_score)}" oninput="$('#ceScoreV').textContent=this.value">
          <span class="fine">Leads scoring below this are kept but never drafted. Your plan's lowest is ${limits.min_score}.</span></div>
        <div class="inp"><label for="ceLeads">Leads per run</label>
          <input type="number" id="ceLeads" min="1" max="${limits.leads_per_run}" value="${Math.min(c.daily_lead_limit, limits.leads_per_run)}">
          <span class="fine">The run stops once it has created this many new leads. Up to ${limits.leads_per_run} on your plan. It is not the number of emails sent.</span></div>
      </div>`)}

    <div class="ce-foot">
      <button class="btn primary" id="ceSave" onclick="saveCampaign()">Save changes</button>
      <button class="btn" onclick="closeDrawer()">Cancel</button>
      <span class="sp"></span>
      <button class="btn" onclick="runCampaign(${c.id})">▶ Run now</button>
    </div>`;

  const d = $('#drawer');
  d.scrollTop = 0;
  d.querySelectorAll('.seg').forEach(g => g.onclick = e => {
    const b = e.target.closest('button');
    if (!b) return;
    g.querySelectorAll('button').forEach(x => { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', x === b); });
  });
  d.querySelectorAll('.chips').forEach(box => box.onclick = e => {
    const b = e.target.closest('.chk');
    if (!b) return;
    b.classList.toggle('on');
    b.setAttribute('aria-pressed', b.classList.contains('on'));
  });
  ceServiceHint();
  ceRotateChanged();
}

function ceAllCountries(on) {
  document.querySelectorAll('#ceCountries .chk').forEach(b => {
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on);
  });
}

function ceRotateChanged() {
  const rotate = $('#ceRotate').checked;
  $('#ceLocations').disabled = rotate;
  $('#ceLocHint').textContent = rotate
    ? 'Not used while city rotation is on.'
    : 'Comma-separated. Leave empty to use each source’s default area.';
}

function ceRead() {
  const c = _ce;
  const picked = id => [...document.querySelectorAll(`#${id} .chk.on`)].map(b => b.dataset.v);
  const listOf = id => $(id).value.split(',').map(s => s.trim()).filter(Boolean);
  return {
    name: $('#ceName').value.trim(),
    service_category: $('#ceService').value,
    enabled: $('#ceEnabled').checked,
    schedule_time: $('#ceTime').value,
    schedule_days: $('#ceDays .on').dataset.v,
    approval_mode: $('#ceApproval .on').dataset.v,
    countries: picked('ceCountries'),
    rotate_cities: $('#ceRotate').checked,
    locations: listOf('#ceLocations'),
    keywords: listOf('#ceKeywords'),
    sources: c.sources || [],
    min_score: +$('#ceScore').value,
    daily_lead_limit: +$('#ceLeads').value,
    daily_email_limit: c.daily_email_limit ?? 20,
  };
}

async function saveCampaign() {
  const c = _ce;
  const body = ceRead();
  if (!body.name) { toast('Give the agent a name', 'bad'); $('#ceName').focus(); return; }
  if (!body.schedule_time) { toast('Pick a start time', 'bad'); $('#ceTime').focus(); return; }
  if (!body.countries.length) { toast('Pick at least one country', 'bad'); return; }
  if (!(body.daily_lead_limit >= 1)) { toast('Leads per run must be at least 1', 'bad'); $('#ceLeads').focus(); return; }

  const btn = $('#ceSave');
  btn.disabled = true;
  try {
    if (c.id) {
      await api(`/api/campaigns/${c.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
    } else {
      await post('/api/campaigns', body);
    }
    toast(c.id ? 'Agent saved' : 'Agent created', 'good');
    closeDrawer();
    if (VIEWS[VIEW]) VIEWS[VIEW]();
  } catch (e) {
    toast(e.message, 'bad');
    btn.disabled = false;
  }
}

async function deleteCampaign(id) {
  const c = (await api('/api/campaigns')).find(x => x.id === id);
  if (!c) return;
  if (!confirm(`Delete campaign "${c.name}"?\n\nIts schedule and run history are removed. Leads it already found are kept.`)) return;
  try {
    await api(`/api/campaigns/${id}`, { method: 'DELETE' });
    toast('Campaign deleted', 'good');
    closeDrawer();
    if (VIEWS[VIEW]) VIEWS[VIEW]();
  } catch (e) { toast(e.message, 'bad'); }
}

async function runCampaign(id) {
  try {
    await post(`/api/campaigns/${id}/run`);
    toast('Agent started', 'good');
    go('workflow');
    setTimeout(() => {
      if (VIEW === 'campaigns') VIEWS.campaigns();
      else if (VIEW === 'workflow') paintWorkflow();
    }, 1500);
  } catch (e) { toast(e.message, 'bad'); }
}

// Stops a running agent at its next safe point, or closes a run that is still
// marked running but no longer alive (left behind by a restart).
async function stopRun(campaignId) {
  if (!confirm('Stop this agent?\n\nIt finishes the step it is on, then stops. Leads and drafts it already made are kept.')) return;
  try {
    const r = await post(`/api/campaigns/${campaignId}/stop`);
    toast(r.stopping ? 'Stopping — the agent finishes its current step, then stops'
      : r.reset ? 'Reset — that run was no longer active and has been closed' : 'Nothing was running', 'good');
    if (VIEW === 'workflow') paintWorkflow();
    else if (VIEW === 'campaigns') VIEWS.campaigns();
  } catch (e) { toast(e.message, 'bad'); }
}

async function dispatchNow() {
  try {
    const r = await post('/api/outbox/dispatch');
    toast(`Sent ${r.sent}, failed ${r.failed}, skipped ${r.skipped}`, r.failed ? 'bad' : 'good');
  } catch (e) { toast(e.message, 'bad'); }
}

/* -------------------------------------------------------------- settings */

VIEWS.settings = async function () {
  if (!isAdmin()) return go('account');  // server-wide settings are the admin's
  const [s, sched, me, mailStatus] = await Promise.all([
    api('/api/settings'), api('/api/scheduler'), api('/auth/me'), api('/api/admin/mail-status'),
  ]);
  const yn = v => v ? '<span class="pill sent">configured</span>' : '<span class="pill rejected">not set</span>';

  $('#view').innerHTML = `
    <div class="head"><div><h2>Platform</h2>
      <p>Server-wide configuration. Secrets live in the environment (Render's Environment
         tab or <code>.env</code>), never in the database; change them there and restart.</p></div></div>

    ${mailCard(mailStatus)}

    <div class="card"><h3>Security</h3>
      <p class="hint">Password sign-in${me.login_required ? '' : ' is off — set ADMIN_PASSWORD in .env before putting this online'}.</p>
      <div class="row" style="gap:18px">
        <div><div class="muted" style="font-size:11.5px">Login</div>
          <b class="mono">${me.login_required ? 'required' : 'disabled (local only)'}</b></div>
        ${me.login_required ? `<div><div class="muted" style="font-size:11.5px">Google Authenticator</div>
          <b class="mono">${me.totp_required ? 'not finished' : 'on'}</b></div>` : ''}
      </div>
      ${me.login_required ? `<div class="save-row">
        ${me.totp_required ? `<button class="btn" onclick="location.href='/login'">Finish 2FA setup</button>`
          : `<button class="btn danger" id="disable2fa">Disable 2FA</button>`}
        <button class="btn" id="logoutBtn">Log out</button>
        <button class="btn ghost" id="logoutAllBtn">Log out everywhere</button>
      </div>` : ''}
    </div>

    <div class="card"><h3>Free keys — get these in order</h3>
      <p class="hint">All free, and shared by every customer's agents. The top one matters most:
         every result it returns is a live website the audit can mine for an email address.</p>
      <div id="freeKeys">Loading…</div>
    </div>

    <div class="card"><h3>Credentials</h3>
      <p class="hint">Each of these unlocks a capability. The app runs without any of them,
         but can only use free sources and cannot send.</p>
      <div style="display:grid;gap:8px">
        ${[['ANTHROPIC_API_KEY', 'anthropic_api_key', 'AI-written outreach (falls back to templates)'],
           ['COMPANIES_HOUSE_KEY', 'companies_house_key', 'New UK companies — free key'],
           ['ADZUNA_APP_ID / KEY', 'adzuna', 'Job-ad hiring signals — free tier'],
           ['SMTP_HOST / USER / PASS', 'smtp', 'Sending email']]
          .map(([k, key, why]) => `<div class="row" style="padding:7px 0;border-bottom:1px solid var(--line)">
            <code style="font-size:12px">${k}</code>
            <span class="muted" style="font-size:12px">${why}</span>
            <span class="sp"></span>${yn(s.configured[key])}</div>`).join('')}
      </div>
    </div>

    <div class="card"><h3>Sending limits</h3>
      <div class="row" style="gap:22px;font-size:13px">
        <div><div class="muted" style="font-size:11.5px">Global daily cap</div><b class="mono">${s.daily_email_cap_global}</b></div>
        <div><div class="muted" style="font-size:11.5px">Per mailbox</div><b class="mono">${s.per_mailbox_daily_cap}</b></div>
        <div><div class="muted" style="font-size:11.5px">Cooling-off</div><b class="mono">${s.cooling_off_days} days</b></div>
        <div><div class="muted" style="font-size:11.5px">Min score to email</div><b class="mono">${s.min_score_to_email}</b></div>
      </div>
      <p class="hint" style="margin:12px 0 0">Each customer sends from the mailbox they connect in their own Settings; these limits apply on top.</p>
    </div>

    <div class="card"><h3>Scheduled jobs</h3>
      ${sched.jobs.length ? sched.jobs.map(j => `<div class="row" style="padding:5px 0;font-size:12.5px">
        <code style="font-size:12px">${esc(j.id)}</code><span class="sp"></span>
        <span class="muted">next ${j.next_run ? fmtTime(j.next_run) : '—'}</span></div>`).join('')
      : '<p class="hint" style="margin:0">Scheduler is off.</p>'}
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
  } catch (e) { $('#freeKeys').innerHTML = `<span class="muted">${esc(e.message)}</span>`; }

  if ($('#logoutBtn')) $('#logoutBtn').onclick = async () => {
    await post('/auth/logout'); location.href = '/login';
  };
  if ($('#logoutAllBtn')) $('#logoutAllBtn').onclick = async () => {
    if (!confirm('Log out every signed-in device?')) return;
    await api('/auth/logout-everywhere'); location.href = '/login';
  };
  if ($('#disable2fa')) $('#disable2fa').onclick = async () => {
    if (!confirm('Turn off Google Authenticator? Only the password will be needed to sign in.')) return;
    try { await post('/auth/totp/disable'); toast('2FA disabled', 'good'); VIEWS.settings(); }
    catch (e) { toast(e.message, 'bad'); }
  };
};

async function rmSup(id) {
  try { await api('/api/suppressions/' + id, { method: 'DELETE' }); VIEWS[VIEW](); }
  catch (e) { toast(e.message, 'bad'); }
}

/* ------------------------------------------------------------------ boot */
/* Startup lives at the end of product.js, which loads after this file. */
