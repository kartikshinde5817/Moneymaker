/* The product layer: accounts, bots & billing, and the admin panel.
 *
 * Loaded after app.js and reuses its helpers ($, esc, api, post, toast, go,
 * VIEWS, META, svgIcon, NODE_ICONS, SERVICE_LABEL, svcIcon, isAdmin).
 * Startup lives at the bottom of this file.
 */

const CHART = '#0891B2';  // single-series bar colour, validated against the card surface

const fmtMoney = (whole, cur, digits = 0) => new Intl.NumberFormat(cur === 'INR' ? 'en-IN' : 'en-US',
  { style: 'currency', currency: cur, maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(whole || 0);
const fmtMinor = (minor, cur) => fmtMoney((minor || 0) / 100, cur, (minor || 0) % 100 ? 2 : 0);
const moneyMap = m => Object.entries(m || {}).map(([c, v]) => fmtMoney(v, c)).join(' + ') || '—';

function prefCurrency() {
  try { const c = localStorage.getItem('mm_cur'); if (c) return c; } catch (e) { /* private mode */ }
  const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || '');
  return /Calcutta|Kolkata/.test(tz) ? 'INR' : 'USD';
}
function setPref(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

function timeLeft(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return 'ended';
  const h = Math.floor(ms / 36e5), m = Math.floor(ms % 36e5 / 6e4), d = Math.floor(h / 24);
  return d >= 2 ? `${d} days` : h ? `${h}h ${m}m` : `${m}m`;
}
const fmtDay = iso => iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '—';
const botName = key => ((META.bots || []).find(b => b.key === key) || {}).name || SERVICE_LABEL[key] || key;
const botIcon = (key, size = 20) => svgIcon(svcIcon(key), size);
const initials = s => (s || '?').split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(x => x[0].toUpperCase()).join('');

async function refreshMeta() {
  META = await api('/api/meta', { quiet: true });
  syncServices();
  renderFoot(); renderMe(); renderAccessBar(); renderNav(window._pending);
}

/* ----------------------------------------------------------- sidebar user */

function renderMe() {
  const u = META.user || {};
  $('#meCard').innerHTML = `
    <span class="av">${esc(initials(u.name || u.email))}</span>
    <div style="min-width:0"><b>${esc(u.name || u.email || 'You')}</b>
      <small>${u.role === 'admin' ? 'Platform admin' : esc(u.company || u.email || '')}</small></div>
    <button class="out" onclick="signOut()" title="Sign out">Sign out</button>`;
}

async function signOut() {
  try { await post('/auth/logout'); } catch (e) { /* already signed out */ }
  location.href = '/login';
}

/* ---------------------------------------------------- trial / plan banner */

function renderAccessBar() {
  const bar = $('#accessBar');
  if (!bar) return;
  if (isAdmin()) { bar.innerHTML = ''; return; }
  const access = META.access || {}, keys = Object.keys(access);
  let html = '';
  const trial = keys.find(k => access[k].kind === 'trial');
  const ending = keys.filter(k => access[k].kind !== 'trial' && access[k].expires_at
    && new Date(access[k].expires_at) - Date.now() < 3 * 864e5);
  if (!keys.length) {
    html = META.trial && META.trial.used
      ? `<b>Your access has ended.</b> Subscribe to a bot to keep finding leads.<a class="btn sm primary" href="#bots">See plans</a>`
      : `<b>Welcome!</b> Try any one bot free for ${META.trial ? META.trial.hours : 24} hours — no card needed.<a class="btn sm primary" href="#bots">Choose a bot</a>`;
  } else if (trial) {
    html = `<b>Free trial:</b> ${esc(timeLeft(access[trial].expires_at))} left on ${esc(botName(trial))}. Subscribe to keep it running.<a class="btn sm primary" href="#bots">Subscribe</a>`;
  } else if (ending.length) {
    html = `<b>${esc(botName(ending[0]))}</b> ends in ${esc(timeLeft(access[ending[0]].expires_at))}. Renew now — the time adds on to what's left.<a class="btn sm primary" href="#bots">Renew</a>`;
  } else if (!META.email_ready || !META.sender_configured) {
    html = `<b>One step left:</b> connect your mailbox and add your sender details so approved emails can go out.<a class="btn sm primary" href="#account">Connect mailbox</a>`;
  }
  bar.innerHTML = html ? `<div class="abar">${html}</div>` : '';
}

/* ------------------------------------------------------------- My bots */

async function loadRazorpay() {
  if (window.Razorpay) return;
  await new Promise((ok, fail) => {
    const s = document.createElement('script');
    s.src = 'https://checkout.razorpay.com/v1/checkout.js';
    s.onload = ok; s.onerror = () => fail(new Error('Could not load the payment window. Check your connection.'));
    document.head.appendChild(s);
  });
}

async function startTrial(key) {
  if (!confirm(`Start your free ${META.trial.hours}-hour trial of ${botName(key)}?\n\nYou get one free trial per account.`)) return;
  try {
    await post('/api/billing/trial', { bot: key });
    toast(`${botName(key)} unlocked — your first agent is ready`, 'good');
    await refreshMeta();
    go('campaigns');
  } catch (e) { toast(e.message, 'bad'); }
}

async function buyBot(key, period, currency) {
  let order;
  try {
    order = await post('/api/billing/checkout', { bot: key, period, currency });
    await loadRazorpay();
  } catch (e) { toast(e.message, 'bad'); return; }
  const rzp = new window.Razorpay({
    key: order.key_id, order_id: order.order_id, amount: order.amount, currency: order.currency,
    name: order.name, description: order.description, prefill: order.prefill,
    theme: { color: '#0E7490' },
    handler: async resp => {
      try {
        await post('/api/billing/verify', {
          order_id: resp.razorpay_order_id, payment_id: resp.razorpay_payment_id, signature: resp.razorpay_signature,
        });
        toast(`Payment received — ${botName(key)} is active`, 'good');
        await refreshMeta();
        go('bots');
      } catch (e) { toast(e.message, 'bad'); }
    },
    modal: { ondismiss: () => toast('Payment window closed — nothing was charged') },
  });
  rzp.on('payment.failed', r => {
    post('/api/billing/failed', { order_id: order.order_id, reason: (r.error && r.error.description) || '' }).catch(() => {});
    toast((r.error && r.error.description) || 'The payment failed', 'bad');
  });
  rzp.open();
}

VIEWS.bots = async function () {
  const b = await api('/api/billing');
  META.access = b.access;
  let cur = prefCurrency(), period = 'month';
  let pending = null;
  try { pending = localStorage.getItem('mm_pending_trial'); localStorage.removeItem('mm_pending_trial'); } catch (e) { /* private mode */ }
  const none = !Object.keys(b.access).length && !isAdmin();

  const paint = () => {
    $('#view').innerHTML = `
      <div class="head"><div><h2>${none ? 'Choose your first bot' : 'My bots'}</h2>
        <p>${none
          ? `Each bot finds leads for one service you sell. Try any one free for ${b.trial.hours} hours, then subscribe to keep it.`
          : 'Each bot is its own subscription. Renewing early adds time on top of what you have left.'}</p></div>
        <div class="row">
          <div class="seg" id="bCur"><button data-v="INR" class="${cur === 'INR' ? 'on' : ''}">₹ INR</button><button data-v="USD" class="${cur === 'USD' ? 'on' : ''}">$ USD</button></div>
          <div class="seg" id="bPer"><button data-v="month" class="${period === 'month' ? 'on' : ''}">Monthly</button><button data-v="year" class="${period === 'year' ? 'on' : ''}">Yearly · 2 months free</button></div>
        </div>
      </div>
      ${!b.razorpay_ready && !isAdmin() ? '<div class="banner">Online payments are being set up. You can still start your free trial.</div>' : ''}
      <div class="store">${b.bots.map((bot, i) => storeCard(bot, b, cur, period, i, pending)).join('')}</div>`;
    $('#bCur').onclick = e => { const x = e.target.closest('button'); if (x) { cur = x.dataset.v; setPref('mm_cur', cur); paint(); } };
    $('#bPer').onclick = e => { const x = e.target.closest('button'); if (x) { period = x.dataset.v; paint(); } };
  };
  paint();
};

function storeCard(bot, b, cur, period, i, pending) {
  const a = b.access[bot.key];
  const price = bot.prices[cur][period];
  const monthly = period === 'year' ? Math.round(price / 12) : null;
  let status, actions;
  if (isAdmin()) {
    status = '<span class="pill sent">Included for admin</span>';
    actions = `<button class="btn" onclick="go('campaigns')">Open agents</button>`;
  } else if (a) {
    status = a.kind === 'trial'
      ? `<span class="pill pending_approval">Trial · ${esc(timeLeft(a.expires_at))} left</span>`
      : `<span class="pill sent">Active until ${esc(fmtDay(a.expires_at))}</span>`;
    actions = `<button class="btn primary" onclick="buyBot('${bot.key}','${period}','${cur}')">${a.kind === 'trial' ? 'Subscribe' : 'Renew'} · ${fmtMoney(price, cur)}</button>
      <button class="btn ghost" onclick="go('campaigns')">Open agents</button>`;
  } else {
    status = '<span class="pill new">Not active</span>';
    actions = `${!b.trial.used ? `<button class="btn good" onclick="startTrial('${bot.key}')">Start free trial</button>` : ''}
      <button class="btn ${b.trial.used ? 'primary' : ''}" onclick="buyBot('${bot.key}','${period}','${cur}')">Subscribe · ${fmtMoney(price, cur)}</button>`;
  }
  return `<div class="sbot${pending === bot.key ? ' hl' : ''}" style="--i:${i}">
    <div class="row" style="align-items:flex-start">
      <span class="sbot-ico">${botIcon(bot.key, 22)}</span>
      <div style="flex:1;min-width:0"><h3>${esc(bot.name)}</h3><p>${esc(bot.tagline)}</p></div>
      ${status}
    </div>
    <ul>${(bot.features || []).map(f => `<li>${esc(f)}</li>`).join('')}</ul>
    <div class="sbot-foot">
      <div class="price"><b>${fmtMoney(price, cur)}</b><span>/${period === 'year' ? 'year' : 'month'}</span>
        ${monthly ? `<small>≈ ${fmtMoney(monthly, cur)}/month</small>` : ''}</div>
      <div class="row" style="gap:8px">${actions}</div>
    </div>
  </div>`;
}

/* -------------------------------------------------------------- Billing */

VIEWS.billing = async function () {
  const b = await api('/api/billing');
  const subs = b.subscriptions.filter(s => !s.revoked);
  const kind = s => ({ trial: 'Free trial', paid: s.period === 'year' ? 'Yearly plan' : 'Monthly plan', grant: 'Added by support' }[s.kind] || s.kind);
  const live = s => new Date(s.expires_at) > Date.now() && new Date(s.starts_at) <= Date.now();
  $('#view').innerHTML = `
    <div class="head"><div><h2>Billing</h2>
      <p>Your plans and payment history. Plans are prepaid and simply end if you don't renew — nothing renews behind your back.</p></div>
      <a class="btn primary" href="#bots">Add or renew a bot</a></div>

    <div class="card"><h3>Plans</h3>
      ${subs.length ? `<div class="tbl-wrap" style="margin-top:10px"><table>
        <thead><tr><th>Bot</th><th>Plan</th><th>From</th><th>Until</th><th>Status</th></tr></thead>
        <tbody>${subs.map(s => `<tr style="cursor:default">
          <td><b>${esc(botName(s.bot))}</b></td><td>${esc(kind(s))}</td>
          <td>${fmtDay(s.starts_at)}</td><td>${fmtDay(s.expires_at)}</td>
          <td>${live(s) ? '<span class="pill sent">Active</span>' : new Date(s.starts_at) > Date.now()
            ? '<span class="pill queued">Starts later</span>' : '<span class="pill new">Ended</span>'}</td></tr>`).join('')}
        </tbody></table></div>` : '<p class="hint" style="margin:6px 0 0">No plans yet. <a href="#bots">Start with a free trial.</a></p>'}
    </div>

    <div class="card"><h3>Payments</h3>
      ${b.payments.length ? `<div class="tbl-wrap" style="margin-top:10px"><table>
        <thead><tr><th>Date</th><th>Bot</th><th>Plan</th><th>Amount</th><th>Status</th><th>Reference</th></tr></thead>
        <tbody>${b.payments.map(p => `<tr style="cursor:default">
          <td>${fmtDay(p.paid_at || p.created_at)}</td><td>${esc(botName(p.bot))}</td>
          <td>${p.period === 'year' ? 'Yearly' : 'Monthly'}</td><td class="mono"><b>${fmtMinor(p.amount, p.currency)}</b></td>
          <td><span class="pill ${p.status === 'paid' ? 'sent' : p.status === 'failed' ? 'failed' : 'new'}">${p.status === 'created' ? 'not completed' : esc(p.status)}</span></td>
          <td class="mono muted" style="font-size:12px">${esc(p.payment_id || p.order_id)}</td></tr>`).join('')}
        </tbody></table></div>` : '<p class="hint" style="margin:6px 0 0">No payments yet.</p>'}
    </div>`;
};

/* ---------------------------------------------------- Settings (account) */

const MAIL_PRESETS = {
  'Google Workspace / Gmail': { smtp_host: 'smtp.gmail.com', smtp_port: 465, smtp_use_ssl: true, imap_host: 'imap.gmail.com', imap_port: 993 },
  'Microsoft 365 / Outlook': { smtp_host: 'smtp.office365.com', smtp_port: 587, smtp_use_ssl: false, imap_host: 'outlook.office365.com', imap_port: 993 },
  'Hostinger': { smtp_host: 'smtp.hostinger.com', smtp_port: 465, smtp_use_ssl: true, imap_host: 'imap.hostinger.com', imap_port: 993 },
  'Zoho Mail': { smtp_host: 'smtp.zoho.com', smtp_port: 465, smtp_use_ssl: true, imap_host: 'imap.zoho.com', imap_port: 993 },
};
const TIMEZONES = ['Asia/Kolkata', 'Europe/London', 'America/New_York', 'America/Chicago', 'America/Los_Angeles',
  'Asia/Dubai', 'Asia/Singapore', 'Australia/Sydney', 'Europe/Berlin', 'UTC'];

VIEWS.account = async function () {
  const [a, sup] = await Promise.all([api('/api/account'), api('/api/suppressions')]);
  const v = f => a.saved[f] != null ? a.saved[f] : '';
  // Only the admin falls back to the server's settings, so only they see them as hints.
  const ph = f => a.role === 'admin' && a.saved[f] == null && a.effective[f] !== null
    && a.effective[f] !== '' ? `server default: ${a.effective[f]}` : '';
  const field = (id, label, opts = {}) => `<div class="inp${opts.wide ? ' wide' : ''}"><label for="ac_${id}">${label}</label>
    ${opts.area ? `<textarea id="ac_${id}" rows="2" style="min-height:62px;font-family:inherit;font-size:13px" placeholder="${esc(opts.ph || ph(id))}">${esc(v(id))}</textarea>`
      : `<input id="ac_${id}" type="${opts.type || 'text'}" value="${esc(v(id))}" placeholder="${esc(opts.ph || ph(id))}" ${opts.attrs || ''}>`}
    ${opts.fine ? `<span class="fine">${opts.fine}</span>` : ''}</div>`;
  const tzs = [...new Set([a.effective.timezone, ...TIMEZONES])];
  const ready = (ok, on, off) => `<span class="pill ${ok ? 'sent' : 'rejected'}">${ok ? on : off}</span>`;

  $('#view').innerHTML = `
    <div class="head"><div><h2>Settings</h2>
      <p>Your profile, the identity your emails are sent under, and the mailbox they are sent from.</p></div>
      <div class="row">${ready(a.ready.smtp, 'Mailbox connected', 'No mailbox')} ${ready(a.ready.sender, 'Sender ready', 'Sender incomplete')}</div></div>
    ${a.role === 'admin' ? '<div class="banner">Admin: any field you leave empty uses the server\'s environment settings (shown as placeholders).</div>' : ''}

    <div class="card"><h3>Profile</h3>
      <div class="grid2">
        <div class="inp"><label for="ac_name">Your name</label><input id="ac_name" value="${esc(a.name || '')}"></div>
        <div class="inp"><label for="ac_company">Company</label><input id="ac_company" value="${esc(a.company || '')}"></div>
        <div class="inp"><label>Sign-in email</label><input value="${esc(a.email)}" disabled></div>
        <div class="inp"><label>Member since</label><input value="${esc(fmtDay(a.created_at))}" disabled></div>
      </div>
    </div>

    <div class="card"><h3>Sender identity</h3>
      <p class="hint">Shown in every email. A real postal address is required by law for marketing email to the US and is
        shown in the footer — the app will not send without one.</p>
      <div class="grid2">
        ${field('sender_name', 'Sender name', { ph: 'Asha Mehta' })}
        ${field('sender_email', 'Sender email', { type: 'email', ph: 'asha@yourcompany.com', fine: 'The address emails come from. Use the mailbox you connect below.' })}
        ${field('company_name', 'Company name')}
        ${field('company_website', 'Company website', { ph: 'https://yourcompany.com', fine: 'Linked in each email so people can see your work.' })}
        ${field('postal_address', 'Postal address', { area: true })}
        ${field('privacy_url', 'Privacy policy URL', { ph: 'https://yourcompany.com/privacy' })}
      </div>
    </div>

    <div class="card"><h3>Mailbox</h3>
      <p class="hint">Emails are sent from your own mailbox, so replies come straight to you and your reputation stays yours.
        Passwords are stored encrypted. For Gmail and Microsoft 365 use an <b>app password</b>.</p>
      <div class="row" style="margin-bottom:14px;gap:6px"><span class="muted" style="font-size:12.5px">Quick setup:</span>
        ${Object.keys(MAIL_PRESETS).map(n => `<button class="btn sm" data-preset="${esc(n)}">${esc(n)}</button>`).join('')}</div>
      <div class="grid2">
        ${field('smtp_host', 'SMTP server', { ph: ph('smtp_host') || 'smtp.yourprovider.com' })}
        <div class="grid2" style="gap:10px">
          ${field('smtp_port', 'SMTP port', { type: 'number', ph: ph('smtp_port') || '465' })}
          <div class="inp"><label>Security</label><select id="ac_smtp_use_ssl">
            <option value="">${a.effective.smtp_use_ssl ? 'SSL (465)' : 'STARTTLS (587)'} — default</option>
            <option value="true" ${a.saved.smtp_use_ssl === true ? 'selected' : ''}>SSL (port 465)</option>
            <option value="false" ${a.saved.smtp_use_ssl === false ? 'selected' : ''}>STARTTLS (port 587)</option></select></div>
        </div>
        ${field('smtp_user', 'Username', { ph: ph('smtp_user') || 'usually your email address' })}
        <div class="inp"><label for="ac_smtp_pass">Password</label><input id="ac_smtp_pass" type="password" autocomplete="new-password"
          placeholder="${a.has_smtp_pass ? '•••••••• saved — type to replace' : 'mailbox or app password'}"></div>
        ${field('imap_host', 'IMAP server (for replies)', { ph: ph('imap_host') || 'imap.yourprovider.com', fine: 'Lets the app see replies, bounces and opt-outs.' })}
        ${field('imap_port', 'IMAP port', { type: 'number', ph: ph('imap_port') || '993' })}
        ${field('imap_user', 'IMAP username', { ph: ph('imap_user') || 'leave empty to reuse the SMTP username' })}
        <div class="inp"><label for="ac_imap_pass">IMAP password</label><input id="ac_imap_pass" type="password" autocomplete="new-password"
          placeholder="${a.has_imap_pass ? '•••••••• saved — type to replace' : 'leave empty to reuse the SMTP password'}"></div>
      </div>
      <div class="save-row" style="margin-top:14px"><button class="btn" id="acTest">Test connection</button><span id="acTestOut" class="muted" style="font-size:12.5px"></span></div>
    </div>

    <div class="card"><h3>Sending</h3>
      <div class="grid2">
        ${field('daily_cap', 'Daily sending limit', { type: 'number', ph: ph('daily_cap') || '50', attrs: 'min="1" max="500"',
          fine: 'New mailboxes warm up automatically: 8 a day in week one, rising to this limit.' })}
        <div class="inp"><label for="ac_timezone">Timezone</label><select id="ac_timezone">
          ${tzs.map(z => `<option ${z === (a.saved.timezone || a.effective.timezone) ? 'selected' : ''}>${esc(z)}</option>`).join('')}</select>
          <span class="fine">Your agents run at their scheduled time in this timezone.</span></div>
      </div>
    </div>

    <div class="save-bar"><button class="btn primary" id="acSave">Save settings</button><span class="muted" style="font-size:12.5px">Saved settings apply to the next send.</span></div>

    ${a.role !== 'admin' ? `<div class="card"><h3>Password</h3>
      <div class="grid2">
        <div class="inp"><label for="pwCur">Current password</label><input id="pwCur" type="password" autocomplete="current-password"></div>
        <div class="inp"><label for="pwNew">New password</label><input id="pwNew" type="password" autocomplete="new-password" placeholder="At least 8 characters"></div>
      </div>
      <div class="save-row"><button class="btn" id="pwSave">Change password</button></div>
    </div>` : ''}

    <div class="card"><h3>Unsubscribe list <span class="muted" style="font-weight:400;font-size:12px">(${sup.length})</span></h3>
      <p class="hint">Anyone who unsubscribes from your emails lands here permanently and is never emailed by you again,
        even if a bot finds them later. Add addresses or whole domains by hand too.</p>
      <div class="row" style="margin-bottom:10px">
        <input id="supVal" placeholder="email@example.com or example.com" style="flex:1;border:1px solid var(--line);border-radius:8px;padding:8px 11px">
        <select id="supScope" style="border:1px solid var(--line);border-radius:8px;padding:8px"><option value="email">Email</option><option value="domain">Domain</option></select>
        <button class="btn" id="supAdd">Add</button>
      </div>
      ${sup.slice(0, 60).map(x => `<div class="row" style="padding:5px 0;border-top:1px solid var(--line);font-size:12.5px">
        <span class="tag grey">${esc(x.scope)}</span><span class="mono">${esc(x.value)}</span>
        <span class="muted">${esc(x.reason)}</span><span class="sp"></span>
        <button class="btn sm ghost" onclick="rmSup(${x.id})">remove</button></div>`).join('')}
    </div>`;

  document.querySelectorAll('[data-preset]').forEach(btn => btn.onclick = () => {
    const p = MAIL_PRESETS[btn.dataset.preset];
    Object.entries(p).forEach(([k, val]) => { const el = $('#ac_' + k); if (el) el.value = String(val); });
    toast(`${btn.dataset.preset} settings filled in — add your username and password`);
  });
  const read = () => {
    const out = {};
    const keys = ['sender_name', 'sender_email', 'company_name', 'company_website', 'postal_address', 'privacy_url',
      'smtp_host', 'smtp_user', 'imap_host', 'imap_user'];
    keys.forEach(k => { out[k] = $('#ac_' + k).value.trim() || null; });
    ['smtp_port', 'imap_port', 'daily_cap'].forEach(k => { const x = $('#ac_' + k).value.trim(); out[k] = x ? +x : null; });
    const ssl = $('#ac_smtp_use_ssl').value; out.smtp_use_ssl = ssl === '' ? null : ssl === 'true';
    out.timezone = $('#ac_timezone').value;
    out.smtp_pass = $('#ac_smtp_pass').value || null;
    out.imap_pass = $('#ac_imap_pass').value || null;
    out.name = $('#ac_name').value.trim(); out.company = $('#ac_company').value.trim();
    return out;
  };
  const save = async (quiet) => {
    await api('/api/account', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(read()) });
    if (!quiet) toast('Settings saved', 'good');
    await refreshMeta();
  };
  $('#acSave').onclick = async () => { try { await save(); VIEWS.account(); } catch (e) { toast(e.message, 'bad'); } };
  $('#acTest').onclick = async () => {
    const out = $('#acTestOut');
    try {
      await save(true);
      out.textContent = 'Connecting…';
      const r = await post('/api/account/test-mail');
      const line = (n, x) => `${n}: ${x.ok ? '✓' : '✕'} ${x.detail}`;
      out.innerHTML = `<span style="color:${r.smtp.ok ? 'var(--good)' : 'var(--warn)'}">${esc(line('Sending', r.smtp))}</span> · ` +
        `<span style="color:${r.imap.ok ? 'var(--good)' : 'var(--muted)'}">${esc(line('Replies', r.imap))}</span>`;
    } catch (e) { out.textContent = ''; toast(e.message, 'bad'); }
  };
  if ($('#pwSave')) $('#pwSave').onclick = async () => {
    try {
      await post('/api/account/password', { current: $('#pwCur').value, new: $('#pwNew').value });
      toast('Password changed', 'good'); $('#pwCur').value = $('#pwNew').value = '';
    } catch (e) { toast(e.message, 'bad'); }
  };
  $('#supAdd').onclick = async () => {
    const value = $('#supVal').value.trim();
    if (!value) return;
    try { await post('/api/suppressions', { value, scope: $('#supScope').value, reason: 'added manually' }); toast('Added', 'good'); VIEWS.account(); }
    catch (e) { toast(e.message, 'bad'); }
  };
};

/* --------------------------------------------------------- admin: charts */

// One series per chart, so no legend: the card title names it. Thin bars with
// rounded tops on a shared baseline, a recessive grid, and a tooltip per bar.
function barChart(series, valueOf, fmt, label) {
  const W = 560, H = 170, padL = 36, padB = 22, padT = 10;
  const vals = series.map(valueOf);
  const max = Math.max(1, ...vals);
  const nice = max <= 4 ? 4 : Math.ceil(max / 4) * 4;
  const bw = (W - padL) / series.length;
  const y = v => H - padB - (v / nice) * (H - padB - padT);
  const grid = [0, .5, 1].map(f => {
    const v = nice * f, yy = y(v);
    return `<line x1="${padL}" x2="${W}" y1="${yy}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>
      <text x="${padL - 6}" y="${yy + 4}" text-anchor="end" font-size="10" fill="var(--muted)">${esc(fmt(v, true))}</text>`;
  }).join('');
  const bars = series.map((d, i) => {
    const v = vals[i], x = padL + i * bw + 1, w = Math.max(2, bw - 2), top = y(v), h = H - padB - top;
    const tip = `${new Date(d.day).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}: ${fmt(v)} ${label}`;
    const r = Math.min(4, w / 2, h);
    const path = h > 0 ? `M${x},${H - padB} V${top + r} Q${x},${top} ${x + r},${top} H${x + w - r} Q${x + w},${top} ${x + w},${top + r} V${H - padB} Z` : '';
    return `<g class="bar-hit" data-tip="${esc(tip)}"><rect x="${padL + i * bw}" y="${padT}" width="${bw}" height="${H - padB - padT}" fill="transparent"/>
      ${path ? `<path d="${path}" fill="${CHART}"/>` : ''}</g>`;
  }).join('');
  const ticks = series.map((d, i) => (series.length - 1 - i) % 7 === 0
    ? `<text x="${padL + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle" font-size="10" fill="var(--muted)">${new Date(d.day).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}</text>` : '').join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${esc(label)} per day, last 30 days" class="chart">${grid}${bars}${ticks}</svg>`;
}

function wireTips(root) {
  let tip = $('#chartTip');
  if (!tip) { tip = document.createElement('div'); tip.id = 'chartTip'; tip.className = 'ctip'; document.body.appendChild(tip); }
  root.querySelectorAll('.bar-hit').forEach(g => {
    g.onmouseenter = () => { tip.textContent = g.dataset.tip; tip.style.opacity = 1; g.classList.add('on'); };
    g.onmousemove = e => { tip.style.left = e.clientX + 12 + 'px'; tip.style.top = e.clientY - 34 + 'px'; };
    g.onmouseleave = () => { tip.style.opacity = 0; g.classList.remove('on'); };
  });
}

/* ------------------------------------------------------- admin: overview */

VIEWS.admin = async function () {
  if (!isAdmin()) return go('workflow');
  const o = await api('/api/admin/overview');
  const curs = [...new Set([...Object.keys(o.revenue_total), ...Object.keys(o.mrr), 'INR'])];
  let revCur = curs.includes(prefCurrency()) ? prefCurrency() : curs[0];
  const tile = (k, v, s, cls = '') => `<div class="tile ${cls}"><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;

  const paintRevenue = () => {
    $('#revChart').innerHTML = barChart(o.series, d => (d.revenue || {})[revCur] || 0,
      (v, axis) => axis ? fmtMoney(v, revCur).replace(/\.00$/, '') : fmtMoney(v, revCur, 2), 'revenue');
    $('#revTotal').textContent = fmtMoney(o.revenue_30d[revCur] || 0, revCur);
    document.querySelectorAll('#revCur button').forEach(b => b.classList.toggle('on', b.dataset.v === revCur));
    wireTips($('#revChart'));
  };

  $('#view').innerHTML = `
    <div class="head"><div><h2>Platform overview</h2><p>Customers, subscriptions and revenue across ${esc(META.app)}.</p></div>
      <div class="row"><button class="btn" onclick="go('customers')">Customers</button><button class="btn primary" onclick="go('adminbots')">Bots & pricing</button></div></div>
    ${!o.razorpay_ready ? '<div class="banner bad">Razorpay is not connected, so customers cannot pay. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET on the server.</div>'
      : !o.webhook_ready ? '<div class="banner">Set RAZORPAY_WEBHOOK_SECRET so payments are confirmed even when a customer closes the tab before returning.</div>' : ''}
    <div class="tiles">
      ${tile('Customers', o.customers, `+${o.new_7d} this week · +${o.new_30d} this month`)}
      ${tile('Paying customers', o.paying_customers, `${o.active_subscriptions} active plans`, o.paying_customers ? 'hot' : '')}
      ${tile('Trials running', o.live_trials, `${o.trials_started} trials started in total`)}
      ${tile('Monthly recurring', moneyMap(o.mrr), 'yearly plans counted per month')}
      ${tile('Revenue, 30 days', moneyMap(o.revenue_30d), `all time ${moneyMap(o.revenue_total)}`)}
      ${tile('Suspended', o.suspended, 'accounts switched off')}
    </div>
    <div class="grid2">
      <div class="card"><div class="row" style="justify-content:space-between"><h3>Signups per day</h3>
        <span class="muted" style="font-size:12.5px">${o.series.reduce((a, d) => a + d.signups, 0)} in 30 days</span></div>
        <div id="sigChart" style="margin-top:10px">${barChart(o.series, d => d.signups, (v, axis) => axis ? String(Math.round(v)) : String(v), 'signups')}</div></div>
      <div class="card"><div class="row" style="justify-content:space-between"><h3>Revenue per day</h3>
        <div class="row" style="gap:8px"><b id="revTotal" class="mono"></b>
          <div class="seg" id="revCur">${curs.map(c => `<button data-v="${c}">${c}</button>`).join('')}</div></div></div>
        <div id="revChart" style="margin-top:10px"></div></div>
    </div>
    <div class="grid2">
      <div class="card"><h3>Bots</h3>
        ${o.bots.map(b => `<div class="row" style="padding:9px 0;border-top:1px solid var(--line);font-size:13px">
          <span class="mini-ico">${botIcon(b.key, 15)}</span><b style="flex:1">${esc(b.name)}</b>
          <span class="muted">${b.subscribers} subscribed</span><span class="tag grey">${b.trials} on trial</span></div>`).join('')}</div>
      <div class="card"><h3>Recent payments</h3>
        ${o.recent_payments.length ? o.recent_payments.map(p => `<div class="row" style="padding:9px 0;border-top:1px solid var(--line);font-size:13px">
          <div style="flex:1;min-width:0"><b>${esc(p.name || p.email)}</b><div class="muted" style="font-size:12px">${esc(p.bot)} · ${p.period === 'year' ? 'yearly' : 'monthly'} · ${fmtDay(p.created_at)}</div></div>
          <b class="mono">${fmtMinor(p.amount, p.currency)}</b>
          <span class="pill ${p.status === 'paid' ? 'sent' : p.status === 'failed' ? 'failed' : 'new'}">${p.status === 'created' ? 'open' : esc(p.status)}</span></div>`).join('')
          : '<p class="hint" style="margin:6px 0 0">No payments yet.</p>'}</div>
    </div>
    <div class="card"><h3>Newest customers</h3>
      ${o.recent_signups.length ? `<div class="tbl-wrap" style="margin-top:10px"><table><thead><tr><th>Name</th><th>Email</th><th>Company</th><th>Joined</th></tr></thead>
        <tbody>${o.recent_signups.map(u => `<tr onclick="openCustomer(${u.id})"><td><b>${esc(u.name || '—')}</b></td><td>${esc(u.email)}</td>
          <td>${esc(u.company || '—')}</td><td>${fmtDay(u.created_at)}</td></tr>`).join('')}</tbody></table></div>`
        : '<p class="hint" style="margin:6px 0 0">No customers yet. Share your landing page to get the first sign-ups.</p>'}</div>`;
  wireTips($('#sigChart'));
  $('#revCur').onclick = e => { const b = e.target.closest('button'); if (b) { revCur = b.dataset.v; paintRevenue(); } };
  paintRevenue();
};

/* ------------------------------------------------------ admin: customers */

VIEWS.customers = async function () {
  if (!isAdmin()) return go('workflow');
  let q = '', status = '';
  const paint = async () => {
    const list = await api(`/api/admin/customers?q=${encodeURIComponent(q)}&status=${status}`);
    $('#custList').innerHTML = list.length ? `<div class="tbl-wrap"><table>
      <thead><tr><th>Customer</th><th>Bots</th><th>Leads</th><th>Emails sent</th><th>Paid</th><th>Joined</th><th>Status</th></tr></thead>
      <tbody>${list.map(c => `<tr onclick="openCustomer(${c.id})">
        <td><div class="row" style="gap:10px;flex-wrap:nowrap"><span class="av-s">${esc(initials(c.name || c.email))}</span>
          <div class="biz">${esc(c.name || '—')}<small>${esc(c.email)}${c.company ? ' · ' + esc(c.company) : ''}</small></div></div></td>
        <td>${Object.keys(c.access).length ? Object.entries(c.access).map(([k, a]) =>
          `<span class="tag ${a.kind === 'trial' ? 'grey' : ''}" title="${esc(a.kind)} until ${esc(fmtDay(a.expires_at))}">${esc(botName(k).replace(/ Bot$/, ''))}${a.kind === 'trial' ? ' · trial' : ''}</span>`).join(' ')
          : '<span class="muted">none</span>'}</td>
        <td class="mono">${c.leads}</td><td class="mono">${c.emails_sent}</td>
        <td class="mono">${moneyMap(c.paid)}</td><td>${fmtDay(c.created_at)}</td>
        <td><span class="pill ${c.status === 'active' ? 'sent' : 'rejected'}">${esc(c.status)}</span></td></tr>`).join('')}
      </tbody></table></div>` : '<div class="empty"><h3>No customers found</h3><p>New sign-ups appear here.</p></div>';
  };
  $('#view').innerHTML = `
    <div class="head"><div><h2>Customers</h2><p>Every account, what it has access to, and how it is using the bots.</p></div></div>
    <div class="filters">
      <input type="search" id="custQ" placeholder="Search name, email or company">
      <select id="custS"><option value="">All statuses</option><option value="active">Active</option><option value="suspended">Suspended</option></select>
    </div>
    <div id="custList"><div class="empty">Loading…</div></div>`;
  let t;
  $('#custQ').oninput = e => { clearTimeout(t); t = setTimeout(() => { q = e.target.value.trim(); paint(); }, 250); };
  $('#custS').onchange = e => { status = e.target.value; paint(); };
  await paint();
};

async function openCustomer(id) {
  const d = $('#drawer');
  d.classList.add('on'); $('#drawerBg').classList.add('on');
  d.innerHTML = '<div class="empty">Loading…</div>';
  let c;
  try { c = await api('/api/admin/customers/' + id); } catch (e) { d.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const bots = (META.bots || []);
  d.innerHTML = `
    <div class="ce-head"><span class="ce-ico" style="font-weight:700">${esc(initials(c.name || c.email))}</span>
      <div style="min-width:0"><span class="ce-kicker">Customer #${c.id}</span><h2>${esc(c.name || c.email)}</h2></div>
      <span class="sp"></span><button class="btn ghost" onclick="closeDrawer()">Close</button></div>
    <div class="ce-sec"><div class="grid2" style="font-size:13px">
      <div><div class="muted">Email</div><b>${esc(c.email)}</b></div>
      <div><div class="muted">Company</div><b>${esc(c.company || '—')}</b></div>
      <div><div class="muted">Joined</div><b>${fmtDay(c.created_at)}</b></div>
      <div><div class="muted">Last sign-in</div><b>${c.last_login_at ? fmtTime(c.last_login_at) : '—'}</b></div>
      <div><div class="muted">Free trial</div><b>${c.trial_bot ? `${esc(botName(c.trial_bot))} · ${fmtDay(c.trial_started_at)}` : 'not used'}</b></div>
      <div><div class="muted">Status</div><span class="pill ${c.status === 'active' ? 'sent' : 'rejected'}">${esc(c.status)}</span></div>
    </div>
    <div class="camp-stats" style="margin-top:14px">
      <div><b>${c.usage.campaigns}</b><span>agents</span></div><div><b>${c.usage.runs}</b><span>runs</span></div>
      <div><b>${c.usage.leads}</b><span>leads</span></div><div><b>${c.usage.emails_sent}</b><span>emails sent</span></div></div></div>

    <div class="ce-sec"><h4>Bot access</h4>
      ${bots.map(b => { const a = c.access[b.key]; return `<div class="row" style="padding:8px 0;border-top:1px solid var(--line);font-size:13px">
        <span class="mini-ico">${botIcon(b.key, 15)}</span><b style="flex:1">${esc(b.name)}</b>
        ${a ? `<span class="pill ${a.kind === 'trial' ? 'pending_approval' : 'sent'}">${esc(a.kind)} · until ${esc(fmtDay(a.expires_at))}</span>
          <button class="btn sm ghost" onclick="revokeBot(${c.id},'${b.key}')">Revoke</button>` : '<span class="muted">no access</span>'}
        <button class="btn sm" onclick="grantBot(${c.id},'${b.key}')">+ Add days</button></div>`; }).join('')}</div>

    <div class="ce-sec"><h4>Payments</h4>
      ${c.payments.length ? c.payments.map(p => `<div class="row" style="padding:7px 0;border-top:1px solid var(--line);font-size:12.5px">
        <span>${fmtDay(p.paid_at || p.created_at)}</span><span style="flex:1">${esc(botName(p.bot))} · ${p.period === 'year' ? 'yearly' : 'monthly'}</span>
        <b class="mono">${fmtMinor(p.amount, p.currency)}</b><span class="pill ${p.status === 'paid' ? 'sent' : p.status === 'failed' ? 'failed' : 'new'}">${esc(p.status)}</span></div>`).join('')
        : '<p class="hint" style="margin:0">No payments.</p>'}</div>

    <div class="ce-sec"><h4>Account</h4>
      <div class="row">
        ${c.status === 'active' ? `<button class="btn danger" onclick="setCustomerStatus(${c.id},'suspended')">Suspend account</button>`
          : `<button class="btn good" onclick="setCustomerStatus(${c.id},'active')">Reactivate account</button>`}
        <button class="btn" onclick="resetCustomerPassword(${c.id})">Set a new password</button>
      </div>
      <p class="fine" style="margin-top:10px;font-size:12px">Suspending signs them out everywhere at once and stops their scheduled agents.</p></div>`;
}

async function grantBot(id, key) {
  const days = prompt(`Add how many days of ${botName(key)}?`, '30');
  if (!days) return;
  try { await post(`/api/admin/customers/${id}/grant`, { bot: key, days: +days, note: 'added by admin' }); toast('Access added', 'good'); openCustomer(id); }
  catch (e) { toast(e.message, 'bad'); }
}
async function revokeBot(id, key) {
  if (!confirm(`Remove ${botName(key)} access now?`)) return;
  try { await post(`/api/admin/customers/${id}/revoke`, { bot: key }); toast('Access removed', 'good'); openCustomer(id); }
  catch (e) { toast(e.message, 'bad'); }
}
async function setCustomerStatus(id, status) {
  if (status === 'suspended' && !confirm('Suspend this account? They are signed out immediately.')) return;
  try { await patch(`/api/admin/customers/${id}`, { status }); toast(status === 'active' ? 'Account reactivated' : 'Account suspended', 'good'); openCustomer(id); if (VIEW === 'customers') VIEWS.customers(); }
  catch (e) { toast(e.message, 'bad'); }
}
async function resetCustomerPassword(id) {
  const pw = prompt('New password for this customer (at least 8 characters). Share it with them securely.');
  if (!pw) return;
  try { await post(`/api/admin/customers/${id}/password`, { password: pw }); toast('Password changed — they are signed out everywhere', 'good'); }
  catch (e) { toast(e.message, 'bad'); }
}

/* ------------------------------------------------- admin: bots & pricing */

VIEWS.adminbots = async function () {
  if (!isAdmin()) return go('workflow');
  const bots = await api('/api/admin/bots');
  $('#view').innerHTML = `
    <div class="head"><div><h2>Bots & pricing</h2><p>What customers can buy and what it costs. Changes go live on the landing page and in checkout immediately; existing plans keep the price they were bought at.</p></div>
      <button class="btn primary" onclick="openServiceForm(null,'list')">${svgIcon(NODE_ICONS.plus)} New bot</button></div>
    <div class="store">${bots.map((b, i) => `
      <div class="sbot" style="--i:${i}" data-key="${esc(b.key)}">
        <div class="row" style="align-items:flex-start">
          <span class="sbot-ico">${botIcon(b.key, 22)}</span>
          <div style="flex:1;min-width:0"><div class="inp"><input class="bName" value="${esc(b.name)}" maxlength="80" style="font-weight:600;font-size:15px"></div></div>
          <label class="sw" title="On sale"><input type="checkbox" class="bActive" ${b.active ? 'checked' : ''}><i></i></label>
        </div>
        <div class="inp" style="margin-top:10px"><label>Tagline</label><input class="bTag" value="${esc(b.tagline)}" maxlength="200"></div>
        <div class="inp" style="margin-top:10px"><label>Features <span class="opt">one per line</span></label>
          <textarea class="bFeat" rows="4" style="min-height:96px;font-family:inherit;font-size:13px">${esc((b.features || []).join('\n'))}</textarea></div>
        <div class="pgrid">
          <div class="inp"><label>₹ / month</label><input type="number" min="0" class="p_inr_month" value="${b.prices.INR.month}"></div>
          <div class="inp"><label>₹ / year</label><input type="number" min="0" class="p_inr_year" value="${b.prices.INR.year}"></div>
          <div class="inp"><label>$ / month</label><input type="number" min="0" class="p_usd_month" value="${b.prices.USD.month}"></div>
          <div class="inp"><label>$ / year</label><input type="number" min="0" class="p_usd_year" value="${b.prices.USD.year}"></div>
        </div>
        <div class="sbot-foot"><span class="muted" style="font-size:12.5px">${b.subscribers} subscribed · ${moneyMap(b.revenue)} earned</span>
          <button class="btn primary sm" onclick="saveBot('${b.key}')">Save</button></div>
      </div>`).join('')}</div>`;
};

async function saveBot(key) {
  const card = document.querySelector(`.sbot[data-key="${key}"]`), v = s => card.querySelector(s).value;
  const body = {
    name: v('.bName').trim(), tagline: v('.bTag').trim(), description: '',
    features: v('.bFeat').split('\n').map(x => x.trim()).filter(Boolean),
    active: card.querySelector('.bActive').checked, sort_order: 100,
    price_inr_month: +v('.p_inr_month'), price_inr_year: +v('.p_inr_year'),
    price_usd_month: +v('.p_usd_month'), price_usd_year: +v('.p_usd_year'),
  };
  const orig = (META.bots || []).find(b => b.key === key);
  if (orig) { body.description = orig.description || ''; body.sort_order = orig.sort_order || 100; }
  try {
    await api(`/api/admin/bots/${key}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast(`${body.name} saved`, 'good'); await refreshMeta();
  } catch (e) { toast(e.message, 'bad'); }
}

/* ------------------------------------------------------- admin: payments */

VIEWS.payments = async function () {
  if (!isAdmin()) return go('workflow');
  let status = '';
  const paint = async () => {
    const rows = await api('/api/admin/payments?status=' + status);
    $('#payList').innerHTML = rows.length ? `<div class="tbl-wrap"><table>
      <thead><tr><th>Date</th><th>Customer</th><th>Bot</th><th>Plan</th><th>Amount</th><th>Status</th><th>Razorpay reference</th></tr></thead>
      <tbody>${rows.map(p => `<tr onclick="openCustomer(${p.user_id})">
        <td>${fmtTime(p.paid_at || p.created_at)}</td><td class="biz">${esc(p.name || '—')}<small>${esc(p.email)}</small></td>
        <td>${esc(p.bot)}</td><td>${p.period === 'year' ? 'Yearly' : 'Monthly'}</td><td class="mono"><b>${fmtMinor(p.amount, p.currency)}</b></td>
        <td><span class="pill ${p.status === 'paid' ? 'sent' : p.status === 'failed' ? 'failed' : 'new'}" title="${esc(p.error || '')}">${p.status === 'created' ? 'not completed' : esc(p.status)}</span></td>
        <td class="mono muted" style="font-size:12px">${esc(p.payment_id || p.order_id)}</td></tr>`).join('')}
      </tbody></table></div>` : '<div class="empty"><h3>No payments</h3><p>Completed and attempted payments appear here.</p></div>';
  };
  $('#view').innerHTML = `
    <div class="head"><div><h2>Payments</h2><p>Every checkout attempt. "Not completed" means the customer opened the payment window but did not finish.</p></div></div>
    <div class="filters"><select id="payS"><option value="">All</option><option value="paid">Paid</option><option value="created">Not completed</option><option value="failed">Failed</option></select></div>
    <div id="payList"><div class="empty">Loading…</div></div>`;
  $('#payS').onchange = e => { status = e.target.value; paint(); };
  await paint();
};

/* ------------------------------------------------------------- startup */

window.addEventListener('hashchange', () => {
  const v = (location.hash || '').replace('#', '');
  if (VIEWS[v] && v !== VIEW) go(v);
});

(async () => {
  try {
    META = await api('/api/meta');
    syncServices();
    const s = await api('/api/stats/summary', { quiet: true });
    window._pending = s.pending_approval;
  } catch (e) {
    if (/Not authenticated/.test(e.message)) { location.href = '/login'; return; }
    $('#view').innerHTML = `<div class="banner bad">Could not reach the server: ${esc(e.message)}</div>`;
    return;
  }
  renderFoot(); renderMe(); renderAccessBar();
  const hash = (location.hash || '').replace('#', '');
  const hasBots = isAdmin() || Object.keys(META.access || {}).length;
  go(VIEWS[hash] ? hash : hasBots ? 'workflow' : 'bots');
  setInterval(() => { if (!isAdmin()) renderAccessBar(); }, 60000);
})();
