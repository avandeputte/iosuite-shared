/* The Suite admin panel: everything in the Shared ioSuite database for the whole suite, the same screen in every io
   app (suite admins only). People and their role in every app, sessions, app passwords (with their scope), every app's
   keys and tokens, the apps with their default role and sign-in requirement, and sign-in at a reverse proxy.

   Served by iosuite-shared at <app>/api/suite-admin/ui.js (Auth.admin_router()); it talks to the API next to it, so an
   app served under a sub-path works too. One ES module, no build step, its own styles (scoped under .iosa), using the
   app's colours when it has them (--fg, --fg2, --bg, --line, --accent, --brand, --danger; ioHealth's --text, --muted,
   --card, --link).

     const ui = await import(new URL('api/suite-admin/ui.js', document.baseURI).href);
     ui.mount(el, { sub: 'people', toast, onSub: (s) => history.replaceState(null, '', `#/settings/suite/${s}`) });
*/
const API = new URL('./', import.meta.url);   // .../api/suite-admin/

const ICONS = {
  add: 'M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z',
  people: 'M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z',
  key: 'M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z',
  apps: 'M4 8h4V4H4v4zm6 12h4v-4h-4v4zm-6 0h4v-4H4v4zm0-6h4v-4H4v4zm6 0h4v-4h-4v4zm6-10v4h4V4h-4zm-6 4h4V4h-4v4zm6 6h4v-4h-4v4zm0 6h4v-4h-4v4z',
  lock: 'M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zM9 8V6c0-1.66 1.34-3 3-3s3 1.34 3 3v2H9z',
};
const icon = (n) => { const s = document.createElement('span'); s.className = 'sa-ic'; s.innerHTML = `<svg viewBox="0 0 24 24"><path d="${ICONS[n] || ''}"/></svg>`; return s; };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  const add = (k) => { if (k == null || k === false) return; if (Array.isArray(k)) k.forEach(add); else el.append(k instanceof Node ? k : String(k)); };
  kids.forEach(add);
  return el;
}

async function api(path, opts = {}) {
  const o = { credentials: 'same-origin', ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers['Content-Type'] = 'application/json'; delete o.json; }
  const r = await fetch(new URL(path, API), o);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === 'string' ? j.detail : (j.error || JSON.stringify(j.detail)); } catch { /* not JSON */ }
    throw new Error(msg || `Error ${r.status}`);
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}

// ------------------------------------------------------------------ styles (once per page)
const CSS = `
.iosa, .iosa-back { --sa-fg: var(--fg, var(--text, #202124)); --sa-fg2: var(--fg2, var(--muted, #5f6368)); --sa-line: var(--line, #dadce0);
  --sa-bg: var(--card, var(--bg, #fff)); --sa-bg2: var(--bg2, color-mix(in srgb, var(--sa-fg) 7%, var(--sa-bg)));
  --sa-accent: var(--accent, var(--link, #1a73e8)); --sa-accent-bg: var(--accent-bg, color-mix(in srgb, var(--sa-accent) 14%, transparent));
  --sa-brand: var(--brand, var(--sa-accent)); --sa-danger: var(--danger, #d93025); color: var(--sa-fg); font-size: 14px; line-height: 1.45; }
.iosa *, .iosa-back * { box-sizing: border-box; }
.iosa .sa-ic, .iosa-back .sa-ic { display: inline-flex; width: 20px; height: 20px; flex: none; }
.iosa .sa-ic svg, .iosa-back .sa-ic svg { width: 100%; height: 100%; fill: currentColor; }
.iosa .sa-muted, .iosa-back .sa-muted { color: var(--sa-fg2); }
.iosa .sa-small, .iosa-back .sa-small { font-size: 12px; }
.iosa .sa-err, .iosa-back .sa-err { color: var(--sa-danger); }
.iosa .sa-intro { margin: 0 0 8px; }
.iosa .sa-segs { display: inline-flex; gap: 2px; padding: 3px; border-radius: 999px; background: var(--sa-bg2); margin: 4px 0 14px; flex-wrap: wrap; }
.iosa .sa-segs button { font: inherit; border: 0; background: none; cursor: pointer; padding: 6px 14px; border-radius: 999px; color: var(--sa-fg2); }
.iosa .sa-segs button.on { background: var(--sa-bg); color: var(--sa-accent); font-weight: 600; box-shadow: 0 1px 2px rgba(0,0,0,.12); }
.iosa .sa-box { border: 1px solid var(--sa-line); border-radius: 16px; padding: 16px 20px; margin-bottom: 16px; background: var(--card, transparent); }
.iosa .sa-box h2 { margin: 0 0 6px; font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.iosa .sa-box h2 .sa-ic { color: var(--sa-brand); }
.iosa .sa-box h3 { margin: 16px 0 4px; font-size: 13px; font-weight: 600; }
.iosa .sa-box p { margin: 6px 0; }
.iosa .sa-scrollx { overflow-x: auto; max-width: 100%; }
.iosa table.sa-t { border-collapse: collapse; width: 100%; }
.iosa table.sa-t th, .iosa table.sa-t td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--sa-line); vertical-align: middle; }
.iosa table.sa-t th { font-size: 12px; color: var(--sa-fg2); font-weight: 600; }
.iosa .sa-face, .iosa-back .sa-face { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; flex: none; background: var(--sa-bg2); }
.iosa .sa-who, .iosa-back .sa-who { display: flex; align-items: center; gap: 8px; }
.iosa table.sa-roles th, .iosa table.sa-roles td { white-space: nowrap; }
.iosa .sa-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; vertical-align: middle; }
.iosa a { color: var(--sa-accent); }
.iosa input.sa-in, .iosa select.sa-in, .iosa textarea.sa-in, .iosa-back input.sa-in, .iosa-back select.sa-in, .iosa-back textarea.sa-in {
  font: inherit; color: inherit; width: 100%; height: 40px; padding: 0 12px; border-radius: 8px; border: 1px solid var(--sa-line); background: var(--sa-bg); outline: 0; min-width: 0; margin: 0; }
.iosa textarea.sa-in { height: auto; padding: 8px 12px; resize: vertical; }
.iosa .sa-in:focus, .iosa-back .sa-in:focus { border-color: var(--sa-accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--sa-accent) 25%, transparent); }
.iosa select.sa-in.sa-sm, .iosa-back select.sa-in.sa-sm { height: 34px; width: auto; padding: 0 8px; }
.iosa .sa-btn, .iosa-back .sa-btn { font: inherit; display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px; border-radius: 999px; color: var(--sa-fg2);
  border: 1px solid var(--sa-line); background: none; cursor: pointer; flex: none; white-space: nowrap; }
.iosa .sa-btn .sa-ic, .iosa-back .sa-btn .sa-ic { width: 18px; height: 18px; margin-left: -4px; }
.iosa .sa-btn:hover, .iosa-back .sa-btn:hover { background: var(--sa-accent-bg); color: var(--sa-accent); border-color: transparent; }
.iosa .sa-btn.primary, .iosa-back .sa-btn.primary { background: var(--sa-brand); color: #fff; border-color: transparent; }
.iosa .sa-btn.primary:hover, .iosa-back .sa-btn.primary:hover { filter: brightness(.92); }
.iosa .sa-btn.danger:hover, .iosa-back .sa-btn.danger:hover { background: color-mix(in srgb, var(--sa-danger) 14%, transparent); color: var(--sa-danger); }
.iosa .sa-link, .iosa-back .sa-link { font: inherit; border: 0; background: none; padding: 0; cursor: pointer; color: var(--sa-accent); font-size: 12px; font-weight: 500; flex: none; }
.iosa .sa-link:hover, .iosa-back .sa-link:hover { text-decoration: underline; }
.iosa .sa-multi, .iosa-back .sa-multi { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.iosa .sa-multi input.sa-in, .iosa-back .sa-multi input.sa-in { flex: 1; min-width: 160px; width: auto; }
.iosa .sa-grid, .iosa-back .sa-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin: 8px 0; }
.iosa .sa-field, .iosa-back .sa-field { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.iosa .sa-field > span, .iosa-back .sa-field > span { font-size: 12px; color: var(--sa-fg2); }
.iosa label.sa-check, .iosa-back label.sa-check { display: flex; gap: 8px; align-items: flex-start; margin: 6px 0; }
.iosa label.sa-check input, .iosa-back label.sa-check input { margin-top: 3px; accent-color: var(--sa-brand); }
.iosa .sa-btns { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.iosa-back { position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 1000; display: flex; align-items: center; justify-content: center; padding: 16px; }
.iosa-back .sa-dialog { background: var(--sa-bg); border-radius: 16px; padding: 20px 22px; width: 100%; max-width: 460px; box-shadow: 0 2px 12px rgba(0,0,0,.4); max-height: 90vh; overflow: auto; }
.iosa-back .sa-dialog.wide { max-width: 620px; }
.iosa-back .sa-dialog h2 { margin: 0 0 10px; font-size: 18px; font-weight: 500; }
.iosa-back .sa-dialog h3 { margin: 18px 0 6px; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 10px; }
.iosa-back .sa-dialog p { margin: 6px 0; }
.iosa-back .sa-dialog .sa-btns { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.iosa-back .sa-rolegrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 6px 16px; }
.iosa-back .sa-rolerow { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.iosa-back .sa-rolerow select.sa-in.sa-sm { width: 150px; flex: none; }
.iosa-back .sa-minirow { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--sa-line); font-size: 13px; }
.iosa-back .sa-minirow > span { min-width: 0; overflow-wrap: anywhere; }
.iosa-toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: #323232; color: #fff; padding: 12px 18px; border-radius: 8px; z-index: 1001; font-size: 14px; max-width: calc(100vw - 32px); }
`;
function styles() {
  if (document.getElementById('iosa-css')) return;
  document.head.append(h('style', { id: 'iosa-css' }, CSS));
}

// ------------------------------------------------------------------ small parts
let toastTimer;
let toast = (msg) => {
  let t = document.querySelector('.iosa-toast');
  if (!t) { t = h('div', { class: 'iosa-toast', role: 'status' }); document.body.append(t); }
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
};
const fail = (e) => toast(e.message || String(e));

function dialog(title, body, buttons, wide = false) {
  return new Promise((resolve) => {
    const back = h('div', { class: 'iosa-back' });
    const close = (v) => { back.remove(); removeEventListener('keydown', esc); resolve(v); };
    const esc = (e) => { if (e.key === 'Escape') close(null); };
    addEventListener('keydown', esc);
    back.addEventListener('click', (e) => { if (e.target === back) close(null); });
    const btns = (buttons || [{ label: 'OK', value: true, primary: true }]).map((b) =>
      h('button', { class: `sa-btn ${b.primary ? 'primary' : ''} ${b.danger ? 'danger' : ''}`, onclick: () => close(typeof b.value === 'function' ? b.value() : b.value) }, b.label));
    back.append(h('div', { class: `sa-dialog${wide ? ' wide' : ''}`, role: 'dialog' }, h('h2', null, title), body, h('div', { class: 'sa-btns' }, btns)));
    document.body.append(back);
    const f = back.querySelector('input:not([type=checkbox]),select,textarea'); if (f) f.focus();
  });
}
const confirmDialog = (title, text, ok = 'OK', danger = false) => dialog(title, h('p', null, text), [{ label: 'Cancel', value: false }, { label: ok, value: true, primary: !danger, danger }]);
const fld = (l, e) => h('label', { class: 'sa-field' }, h('span', null, l), e);
const box = (ic, title, ...kids) => h('div', { class: 'sa-box' }, h('h2', null, icon(ic), title), ...kids);

const SCOPE_LABEL = { '': 'Everything', dav: 'Sync only (CalDAV, CardDAV, LDAP)', read: 'Read only (an app reading another)' };
const ROLE_HELP = { none: 'no access', viewer: 'sees', editor: 'changes', admin: 'manages' };
function sinceText(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 45) return `${Math.round(s / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const appLabel = (o, key) => (o.apps.find((a) => a.app === key) || {}).name || key;

// ------------------------------------------------------------------ the panel
export const SUBS = [['people', 'People'], ['roles', 'Roles'], ['passwords', 'App passwords'], ['keys', 'Keys and tokens'], ['apps', 'Apps'], ['signin', 'Sign-in at the proxy']];

/** Draw the panel into `el`. Options: sub (the first part shown, one of SUBS), onSub(sub) (called when another part
    is picked, e.g. to put it in the address), toast(msg) (the app's own toast; else the panel's). */
export async function mount(el, opts = {}) {
  styles();
  if (opts.toast) toast = opts.toast;
  const root = h('div', { class: 'iosa' });
  el.replaceChildren(root);
  let sub = SUBS.some(([k]) => k === opts.sub) ? opts.sub : 'people';
  let photo = null;  // ioPix's People (the person in photos), read once, when People is first shown
  const photoPeople = () => (photo = photo || api('photo-people').catch((e) => ({ available: false, people: [], error: e.message })));
  const draw = async () => {
    let o;
    try { o = await api('../suite-admin'); } catch (e) { root.replaceChildren(h('p', { class: 'sa-err' }, e.message)); return; }
    o.people = o.users.filter((u) => !u.service);
    const body = h('div');
    root.replaceChildren(
      h('p', { class: 'sa-muted sa-intro' }, 'The whole suite at once: the accounts, passwords and roles every io app shares (the Shared ioSuite database). Changes apply in every app within a few seconds. Suite admins are admin everywhere.'),
      h('div', { class: 'sa-segs' }, SUBS.map(([k, l]) => h('button', { class: sub === k ? 'on' : '', onclick: () => go(k) }, l))),
      body);
    const ctx = { o, redraw: draw, go, photoPeople };
    await ({ people: people, roles: roles, passwords: passwords, keys: keys, apps: apps, signin: signin })[sub](body, ctx);
  };
  const go = (k) => { sub = k; if (opts.onSub) opts.onSub(k); draw(); };
  await draw();
  return { go, refresh: draw };
}

/** A role picker for one account in one app: "default (…)" removes the row, so the app's default applies. */
function roleSelect(u, a) {
  const cur = u.roles[a.app] || '';
  const allowed = u.passwordless ? ['none', 'viewer'] : ['none', 'viewer', 'editor', 'admin'];
  return h('select', { class: 'sa-in sa-sm', title: `${u.displayName} in ${a.name}` },
    h('option', { value: '' }, `default (${u.passwordless && ['editor', 'admin'].includes(a.defaultRole) ? 'viewer' : a.defaultRole})`),
    [...new Set([...allowed, ...(cur ? [cur] : [])])].map((x) => h('option', { value: x, selected: x === cur ? true : null }, x)));
}

const faceUrl = (id) => new URL(`photo-people/${id}/face.jpg`, API).href;
const face = (id) => h('img', { class: 'sa-face', src: faceUrl(id), alt: '', loading: 'lazy', onerror: (e) => { e.target.style.visibility = 'hidden'; } });

function people(el, ctx) {
  const { o } = ctx;
  // the person in photos (ioPix's People): a column, filled in once ioPix answered, left out without ioPix
  const photoTh = h('th', { style: { display: 'none' } }, 'In photos');
  const photoTd = {};
  ctx.photoPeople().then((p) => {
    if (!p.available) return;
    const names = new Map(p.people.map((x) => [x.id, x.name]));
    photoTh.style.display = '';
    for (const u of o.people) {
      const td = photoTd[u.id];
      td.style.display = '';
      td.replaceChildren(u.photoPerson ? h('span', { class: 'sa-who' }, face(u.photoPerson), names.get(u.photoPerson) || `#${u.photoPerson}`) : h('span', { class: 'sa-muted' }, '–'));
    }
  });
  el.append(box('people', 'People',
    h('div', { class: 'sa-scrollx' }, h('table', { class: 'sa-t' }, h('tr', null, h('th', null, 'Account'), photoTh, h('th', null, 'Email'), h('th', null, 'Sign-in'), h('th', null, 'Last seen'), h('th')),
      o.people.map((u) => h('tr', null,
        h('td', null, h('b', null, u.displayName), h('div', { class: 'sa-muted sa-small' }, u.name,
          u.suiteAdmin ? ' · suite admin' : '', u.listed ? '' : ' · not on the sign-in list', u.id === o.me ? ' · you' : '')),
        (photoTd[u.id] = h('td', { class: 'sa-small', style: { display: 'none' } })),
        h('td', { class: 'sa-small' }, u.email || h('span', { class: 'sa-muted' }, '–')),
        h('td', { class: 'sa-small' }, u.passwordless ? 'One click (LAN, viewer)' : 'Password',
          u.appPasswords ? h('div', { class: 'sa-muted' }, `${u.appPasswords} app password${u.appPasswords > 1 ? 's' : ''}`) : null),
        h('td', { class: 'sa-small' }, sinceText(u.lastSeen || u.last_login), h('div', { class: 'sa-muted' }, `${u.sessions} session${u.sessions === 1 ? '' : 's'}`)),
        h('td', { style: { textAlign: 'right' } }, h('button', { class: 'sa-btn', onclick: () => editAccount(ctx, u) }, 'Manage')))))),
    h('p', null, h('button', { class: 'sa-btn primary', onclick: () => editAccount(ctx, null) }, icon('add'), 'Add someone'))));
  const svc = o.users.filter((u) => u.service);
  if (svc.length) el.append(box('apps', 'Apps reading from each other',
    h('p', { class: 'sa-muted' }, 'Each app that reads from another (ioCalendar and ioPix from ioContacts, ioContacts from ioPix, every app's Settings › Suite from ioPix for the person in photos) signs in as its own account here, with a read-only app password it made by itself. Nobody signs in with these. Revoking one of their app passwords is safe: the app makes a new one the next time it needs it.'),
    h('div', { class: 'sa-scrollx' }, h('table', { class: 'sa-t' }, h('tr', null, h('th', null, 'Account'), h('th', null, 'For'), h('th', null, 'App passwords'), h('th', null, 'Last used'), h('th')),
      svc.map((u) => h('tr', null, h('td', null, h('b', null, u.name)), h('td', null, appLabel(o, u.service)),
        h('td', { class: 'sa-small' }, `${u.appPasswords}`), h('td', { class: 'sa-small' }, sinceText(u.lastSeen || u.last_login)),
        h('td', { style: { textAlign: 'right' } }, h('button', { class: 'sa-btn', onclick: () => ctx.go('passwords') }, 'App passwords'))))))));
}

/** The account dialog: who they are, how they sign in, their role in every app, and (existing accounts) their
    sessions and app passwords. */
async function editAccount(ctx, u) {
  const { o } = ctx;
  const isNew = !u, me = u && u.id === o.me;
  u = u || { name: '', displayName: '', email: '', listed: true, suiteAdmin: false, passwordless: false, roles: {} };
  const name = h('input', { class: 'sa-in', value: u.name, autocomplete: 'off' });
  const dn = h('input', { class: 'sa-in', value: isNew ? '' : u.displayName, autocomplete: 'off' });
  const em = h('input', { class: 'sa-in', type: 'email', value: u.email, autocomplete: 'off' });
  const listed = h('input', { type: 'checkbox', checked: u.listed });
  const sa = h('input', { type: 'checkbox', checked: u.suiteAdmin, disabled: me ? true : null });
  const pw = h('input', { class: 'sa-in', type: 'password', autocomplete: 'new-password', placeholder: isNew ? '8 or more; empty: one click on the LAN, viewer only' : (u.passwordless ? 'none yet (one click on the LAN)' : 'unchanged') });
  const nopw = h('input', { type: 'checkbox', checked: !isNew && u.passwordless });
  const sels = {};
  const roleRows = o.apps.map((a) => { sels[a.app] = roleSelect(u, a); return h('div', { class: 'sa-rolerow' }, h('span', null, a.name, a.registered ? '' : h('span', { class: 'sa-muted sa-small' }, ' (not running)')), sels[a.app]); });
  const extra = h('div');
  // the person in photos: someone in ioPix's People; their face is the account's picture in ioPix (sign-in screen, menu)
  const ppSel = h('select', { class: 'sa-in' }, h('option', { value: '' }, 'not linked'));
  const ppFace = h('span');
  const ppNote = h('div', { class: 'sa-muted sa-small' });
  const ppRow = h('div', { style: { display: 'none' } }, h('h3', null, 'Person in photos'),
    h('div', { class: 'sa-who' }, ppFace, ppSel), ppNote);
  const showFace = () => ppFace.replaceChildren(...(ppSel.value ? [face(ppSel.value)] : []));
  ppSel.addEventListener('change', showFace);
  ctx.photoPeople().then((p) => {
    if (!p.available) return;
    ppRow.style.display = '';
    const list = p.people.slice().sort((a, b) => a.name.localeCompare(b.name));
    if (u.photoPerson && !list.some((x) => x.id === u.photoPerson)) list.unshift({ id: u.photoPerson, name: `#${u.photoPerson} (not in People any more)` });
    ppSel.append(...list.map((x) => h('option', { value: x.id, selected: x.id === u.photoPerson ? true : null }, x.photos != null ? `${x.name} (${x.photos})` : x.name)));
    ppSel.value = u.photoPerson ? String(u.photoPerson) : '';
    showFace();
    ppNote.textContent = p.error || 'Someone in ioPix’s People: their face is this account’s picture in ioPix (the sign-in screen and the menu) and “Photos of me” shows their photos.';
  });
  const body = h('div', null,
    h('div', { class: 'sa-grid' }, fld('User name', name), fld('Display name', dn), fld('Email', em), fld(isNew ? 'Password' : 'New password', pw)),
    !isNew && !me ? h('label', { class: 'sa-check' }, nopw, h('span', null, 'No password: one-click sign-in on the LAN only, viewer everywhere, no app passwords')) : null,
    h('label', { class: 'sa-check' }, listed, h('span', null, 'On the sign-in list')),
    h('label', { class: 'sa-check' }, sa, h('span', null, 'Suite admin: admin in every app, manages this screen', me ? h('span', { class: 'sa-muted sa-small' }, ' (another suite admin can change yours)') : '')),
    ppRow, h('h3', null, 'Role in each app'), h('div', { class: 'sa-rolegrid' }, roleRows), extra);
  if (!isNew) accountExtra(extra, o, u);
  const save = async () => {
    const roles = {};
    for (const [k, s] of Object.entries(sels)) if ((s.value || '') !== (u.roles[k] || '')) roles[k] = s.value || null;
    const b = { name: name.value.trim(), displayName: dn.value.trim(), email: em.value.trim(), listed: listed.checked, suiteAdmin: sa.checked, roles };
    if (isNew) b.password = pw.value;
    else if (nopw.checked && !u.passwordless) b.password = '';
    else if (pw.value) b.password = pw.value;
    if (me) delete b.suiteAdmin;
    const pp = ppSel.value ? Number(ppSel.value) : null;
    if (ppRow.style.display !== 'none' && pp !== (u.photoPerson || null)) b.photoPerson = pp;
    try {
      await api(isNew ? 'users' : `users/${u.id}`, { method: isNew ? 'POST' : 'PATCH', json: b });
      toast(isNew ? 'Account made' : 'Saved'); return true;
    } catch (e) { fail(e); return false; }
  };
  const buttons = [{ label: 'Cancel', value: null }];
  if (!isNew && !me) buttons.unshift({ label: 'Delete', value: 'delete', danger: true });
  buttons.push({ label: isNew ? 'Add' : 'Save', value: 'save', primary: true });
  // the dialog closes on any button; keep it open on a failed save by asking again
  for (;;) {
    const v = await dialog(isNew ? 'Add someone' : u.displayName, body, buttons, true);
    if (v === 'save') { if (await save()) break; continue; }
    if (v === 'delete') {
      if (await confirmDialog('Delete account', `Delete ${u.name}? The account, its roles, sessions and app passwords go from every io app.`, 'Delete', true)) {
        try { await api(`users/${u.id}`, { method: 'DELETE' }); toast('Deleted'); break; } catch (e) { fail(e); }
      }
      continue;
    }
    break;
  }
  ctx.redraw();
}
async function accountExtra(el, o, u) {
  let ses, pws;
  try { [ses, pws] = await Promise.all([api(`users/${u.id}/sessions`), api(`app-passwords?user=${u.id}`)]); } catch (e) { el.replaceChildren(h('p', { class: 'sa-err' }, e.message)); return; }
  const draw = () => el.replaceChildren(...[
    h('h3', null, 'Sessions', ses.sessions.length ? h('button', { class: 'sa-link', onclick: async () => {
      try { await api(`users/${u.id}/sessions`, { method: 'DELETE' }); ses.sessions = ses.sessions.filter((s) => s.current); toast('Signed out everywhere'); draw(); } catch (e) { fail(e); }
    } }, 'End all') : null),
    ses.sessions.length ? ses.sessions.map((s) => h('div', { class: 'sa-minirow' }, h('span', null, `${appLabel(o, s.app)} · ${s.client}${s.cloudflare ? ' · via Cloudflare' : ''}`, h('span', { class: 'sa-muted sa-small' }, ` ${s.lastIp || s.ip || ''} · ${sinceText(s.lastSeen)}${s.current ? ' · this browser' : ''}`)),
      s.current ? null : h('button', { class: 'sa-link', onclick: async () => { try { await api(`users/${u.id}/sessions?session=${encodeURIComponent(s.id)}`, { method: 'DELETE' }); ses.sessions = ses.sessions.filter((x) => x !== s); draw(); } catch (e) { fail(e); } } }, 'End'))) : h('p', { class: 'sa-muted sa-small' }, 'Not signed in anywhere.'),
    h('h3', null, 'App passwords'),
    pws.items.length ? pws.items.map((p) => h('div', { class: 'sa-minirow' }, h('span', null, `${p.appName} · ${p.label}`, h('span', { class: 'sa-muted sa-small' }, ` ${SCOPE_LABEL[p.scope] || p.scope} · used ${sinceText(p.lastUsed)}`)),
      h('button', { class: 'sa-link', onclick: async () => { if (await revokeAppPassword(p)) { pws.items = pws.items.filter((x) => x !== p); draw(); } } }, 'Revoke'))) : h('p', { class: 'sa-muted sa-small' }, 'None. Make one under App passwords.')].flat());
  draw();
}
async function revokeAppPassword(p) {
  if (!await confirmDialog('Revoke app password', `Revoke "${p.label}" (${p.displayName}, ${p.appName})? The device using it stops working.`, 'Revoke', true)) return false;
  try { await api(`app-passwords/${p.id}`, { method: 'DELETE' }); toast('Revoked'); return true; } catch (e) { fail(e); return false; }
}

function roles(el, ctx) {
  const { o } = ctx;
  el.append(box('lock', 'Roles',
    h('p', { class: 'sa-muted' }, 'Each person’s role in each app: none (no access), viewer, editor or admin (the app’s settings and its people). “default” follows the app’s default role (set under Apps). One-click accounts are viewers at most; suite admins are admin everywhere.'),
    h('div', { class: 'sa-scrollx' }, h('table', { class: 'sa-t sa-roles' },
      h('tr', null, h('th', null, 'Person'), o.apps.map((a) => h('th', null, a.name, h('div', { class: 'sa-muted sa-small' }, `default: ${a.defaultRole}`)))),
      o.people.map((u) => h('tr', null, h('td', null, h('b', null, u.displayName), h('div', { class: 'sa-muted sa-small' }, u.name)),
        o.apps.map((a) => {
          if (u.suiteAdmin) return h('td', { class: 'sa-muted sa-small' }, 'admin (suite)');
          const s = roleSelect(u, a);
          s.onchange = async () => {
            try { await api(`users/${u.id}`, { method: 'PATCH', json: { roles: { [a.app]: s.value || null } } }); u.roles[a.app] = s.value || undefined; toast(`${u.displayName}: ${s.value || 'default'} in ${a.name}`); } catch (e) { fail(e); ctx.redraw(); }
          };
          return h('td', null, s);
        })))))));
}

async function passwords(el, ctx) {
  const { o } = ctx;
  const fu = h('select', { class: 'sa-in sa-sm' }, h('option', { value: '' }, 'Everyone'), o.users.map((u) => h('option', { value: u.id }, u.displayName)));
  const fa = h('select', { class: 'sa-in sa-sm' }, h('option', { value: '' }, 'Every app'), o.apps.map((a) => h('option', { value: a.app }, a.name)));
  const list = h('div', { class: 'sa-scrollx' });
  const draw = async () => {
    let r; try { r = await api(`app-passwords?${new URLSearchParams({ ...(fu.value ? { user: fu.value } : {}), ...(fa.value ? { app: fa.value } : {}) })}`); } catch (e) { list.replaceChildren(h('p', { class: 'sa-err' }, e.message)); return; }
    list.replaceChildren(r.items.length ? h('table', { class: 'sa-t' }, h('tr', null, h('th', null, 'Person'), h('th', null, 'App'), h('th', null, 'Device'), h('th', null, 'Reaches'), h('th', null, 'Made'), h('th', null, 'Last used'), h('th')),
      r.items.map((p) => {
        const sc = h('select', { class: 'sa-in sa-sm' }, [...new Set([...(o.scopes || ['', 'dav']), p.scope])].map((s) => h('option', { value: s, selected: s === p.scope ? true : null }, SCOPE_LABEL[s] || s)));
        sc.onchange = async () => { try { await api(`app-passwords/${p.id}`, { method: 'PATCH', json: { scope: sc.value } }); p.scope = sc.value; toast('Saved'); } catch (e) { fail(e); sc.value = p.scope; } };
        return h('tr', null, h('td', null, p.displayName), h('td', null, p.appName), h('td', null, p.label), h('td', null, sc),
          h('td', { class: 'sa-small' }, new Date(p.created * 1000).toLocaleDateString()), h('td', { class: 'sa-small' }, sinceText(p.lastUsed)),
          h('td', { style: { textAlign: 'right' } }, h('button', { class: 'sa-btn danger', onclick: async () => { if (await revokeAppPassword(p)) draw(); } }, 'Revoke')));
      })) : h('p', { class: 'sa-muted' }, 'No app passwords here.'));
  };
  fu.onchange = fa.onchange = draw;
  const who = h('select', { class: 'sa-in sa-sm' }, o.people.filter((u) => !u.passwordless).map((u) => h('option', { value: u.id }, u.displayName)));
  const app = h('select', { class: 'sa-in sa-sm' }, o.apps.map((a) => h('option', { value: a.app }, a.name)));
  const label = h('input', { class: 'sa-in', placeholder: 'Device, e.g. Alex’s iPhone' });
  const scope = h('select', { class: 'sa-in sa-sm' }, (o.scopes || ['', 'dav']).map((s) => h('option', { value: s }, SCOPE_LABEL[s] || s)));
  el.append(box('key', 'App passwords',
    h('p', { class: 'sa-muted' }, 'One per device and app, for clients that cannot sign in on the web page (CalDAV and CardDAV, LDAP, MCP assistants…). “Sync only” works for the app’s sync protocols and nothing else, so a password lifted from a phone cannot reach the web app or its API.'),
    h('div', { class: 'sa-multi' }, fu, fa), list),
  box('add', 'Make one for someone',
    h('div', { class: 'sa-multi' }, who, app, scope, label, h('button', { class: 'sa-btn primary', onclick: async () => {
      try {
        const r = await api('app-passwords', { method: 'POST', json: { userId: Number(who.value), app: app.value, label: label.value.trim(), scope: scope.value } });
        label.value = '';
        await dialog('App password', h('div', null, h('p', null, `For ${r.name} in ${appLabel(o, r.app)}. Copy it now: it is not shown again.`),
          h('div', { class: 'sa-multi' }, h('input', { class: 'sa-in', value: r.password, readonly: true, onfocus: (e) => e.target.select() }),
            h('button', { class: 'sa-btn', onclick: async () => { try { await navigator.clipboard.writeText(r.password); toast('Copied'); } catch { toast('Select it and copy'); } } }, 'Copy')),
          h('p', { class: 'sa-muted sa-small' }, `User name: ${r.name}`)), [{ label: 'Done', value: true, primary: true }]);
        draw();
      } catch (e) { fail(e); }
    } }, 'Make'))));
  draw();
}

/** Every app's keys and tokens for things outside the suite (Home Assistant, AccuWeather, Google Maps...), kept in the
    Shared ioSuite database. Write-only: a value is never shown back, only whether one is set. */
async function keys(el, ctx) {
  const { o } = ctx;
  let r;
  try { r = await api('keys'); } catch (e) { el.replaceChildren(h('p', { class: 'sa-err' }, e.message)); return; }
  const groups = {};
  for (const k of r.items) (groups[k.app] = groups[k.app] || []).push(k);
  const order = Object.keys(groups).sort((a, b) => (a === '' ? 1 : b === '' ? -1 : appLabel(o, a).localeCompare(appLabel(o, b))));
  const row = (k) => {
    const inp = h('input', { class: 'sa-in', type: 'password', autocomplete: 'off', placeholder: k.set ? 'set; paste a new one to replace it' : 'not set' });
    const where = k.app === '' ? '_suite' : k.app;
    const save = async (value) => {
      try { const n = await api(`keys/${encodeURIComponent(where)}/${encodeURIComponent(k.name)}`, { method: 'PUT', json: { value } }); Object.assign(k, n); toast(n.set ? 'Saved' : 'Cleared'); el.replaceChildren(); keys(el, ctx); } catch (e) { fail(e); }
    };
    return h('div', { style: { padding: '10px 0', borderTop: '1px solid var(--sa-line)' } },
      h('div', null, h('b', null, k.label), ' ', h('span', { class: 'sa-small' }, k.set ? '· set' : h('span', { class: 'sa-muted' }, '· not set'),
        k.updated ? h('span', { class: 'sa-muted' }, ` ${sinceText(k.updated)}${k.updatedBy ? ` by ${k.updatedBy}` : ''}`) : null)),
      k.hint ? h('div', { class: 'sa-muted sa-small' }, k.hint) : null,
      k.envSet ? h('div', { class: 'sa-muted sa-small' }, `${k.env} in the app's environment wins over this one.`) : null,
      h('div', { style: { marginTop: '6px' } }, h('div', { class: 'sa-multi' }, inp,
        h('button', { class: 'sa-btn', onclick: () => { if (inp.value.trim()) save(inp.value.trim()); else toast('Paste the new value first'); } }, 'Replace'),
        k.set ? h('button', { class: 'sa-btn danger', onclick: async () => { if (await confirmDialog('Clear key', `Clear ${k.label}? What uses it stops working until a new one is set.`, 'Clear', true)) save(''); } }, 'Clear') : null)));
  };
  el.append(box('key', 'Keys and tokens',
    h('p', { class: 'sa-muted' }, 'The keys each app keeps for services outside the suite, all in the Shared ioSuite database. Values are never shown again once saved. How the apps sign in to each other is automatic: see the app passwords of the apps’ own accounts under People.'),
    order.length ? order.map((a) => [h('h3', null, a === '' ? 'Whole suite' : appLabel(o, a)), h('div', null, groups[a].map(row))]) : h('p', { class: 'sa-muted' }, 'No app keeps any yet.')));
}

function apps(el, ctx) {
  const { o } = ctx;
  el.append(box('apps', 'Apps',
    h('p', { class: 'sa-muted' }, 'Every app registers itself when it starts. The default role is what someone without a role in the app gets: “none” makes the app invitation-only. “Sign-in required” shows visitors nothing but the sign-in screen.'),
    h('div', { class: 'sa-scrollx' }, h('table', { class: 'sa-t' }, h('tr', null, h('th', null, 'App'), h('th', null, 'Version'), h('th', null, 'Default role'), h('th', null, 'Sign-in required'), h('th', null, 'In use'), h('th')),
      o.apps.map((a) => {
        const dr = h('select', { class: 'sa-in sa-sm' }, (o.defaultRoles || ['none', 'viewer', 'editor']).map((x) => h('option', { value: x, selected: x === a.defaultRole ? true : null }, `${x} (${ROLE_HELP[x]})`)));
        dr.onchange = async () => { try { await api(`apps/${a.app}`, { method: 'PATCH', json: { defaultRole: dr.value } }); a.defaultRole = dr.value; toast(`${a.name}: ${dr.value} by default`); } catch (e) { fail(e); dr.value = a.defaultRole; } };
        const rl = h('input', { type: 'checkbox', checked: a.requireLogin, onchange: async (e) => { try { await api(`apps/${a.app}`, { method: 'PATCH', json: { requireLogin: e.target.checked } }); toast('Saved'); } catch (err) { e.target.checked = !e.target.checked; fail(err); } } });
        return h('tr', null,
          h('td', null, h('span', { style: { whiteSpace: 'nowrap' } }, h('span', { class: 'sa-dot', style: { background: a.color || 'var(--sa-fg2)' } }), ' ', a.url ? h('a', { href: a.url, target: '_blank', rel: 'noopener' }, a.name) : h('b', null, a.name)),
            h('div', { class: 'sa-muted sa-small' }, a.registered ? `${a.app} · seen ${sinceText(a.lastSeen)}` : `${a.app} · not registered (only in the data)`)),
          h('td', { class: 'sa-small' }, a.version || '–'), h('td', null, dr), h('td', null, rl),
          h('td', { class: 'sa-small' }, `${a.roles} role${a.roles === 1 ? '' : 's'}, ${a.appPasswords} app password${a.appPasswords === 1 ? '' : 's'}`),
          h('td', { style: { textAlign: 'right' } }, h('button', { class: 'sa-btn danger', onclick: () => forgetApp(ctx, a) }, 'Forget')));
      })))));
}
async function forgetApp(ctx, a) {
  const data = h('input', { type: 'checkbox' });
  const v = await dialog(`Forget ${a.name}`, h('div', null, h('p', null, `Take ${a.name} off the list of apps? If it is still installed, it comes back the next time it starts.`),
    h('label', { class: 'sa-check' }, data, h('span', null, `Also remove its roles, settings and app passwords (${a.roles} roles, ${a.appPasswords} app passwords): it was uninstalled for good`))),
  [{ label: 'Cancel', value: null }, { label: 'Forget', value: () => (data.checked ? 'data' : 'list'), danger: true }]);
  if (!v) return;
  try { await api(`apps/${a.app}${v === 'data' ? '?data=1' : ''}`, { method: 'DELETE' }); toast(`${a.name} forgotten`); ctx.redraw(); } catch (e) { fail(e); }
}

/* Sign-in at a reverse proxy (Cloudflare Access on a tunnel, or Authelia & co. behind nginx...), once for every app:
   iosuite-shared keeps it suite-wide and reads it from nowhere else (config.env and the environment are ignored). */
const PROXIES = [['off', 'Off'], ['cloudflare', 'Cloudflare Access'], ['haproxy', 'HAProxy'], ['nginx', 'nginx'], ['caddy', 'Caddy'], ['traefik', 'Traefik'], ['header', 'Another proxy']];
const NO_ACCOUNT = [['', 'Gets the sign-in screen'], ['deny', 'Is denied access'], ['viewer', 'Gets a viewer account'], ['editor', 'Gets an editor account']];
async function signin(el) {
  let d;
  try { d = await api('proxy'); } catch (e) { el.replaceChildren(h('p', { class: 'sa-err' }, e.message)); return; }
  const draw = () => {
    const v = d.values;
    const opt = (list, cur) => list.map(([k, l]) => h('option', { value: k, selected: k === (cur || '') ? true : null }, l));
    const mode = h('select', { class: 'sa-in' }, opt(PROXIES, (v.AUTH_PROXY || 'off').toLowerCase()));
    const team = h('input', { class: 'sa-in', value: v.CF_ACCESS_TEAM, placeholder: 'myteam.cloudflareaccess.com', autocomplete: 'off' });
    const aud = h('textarea', { class: 'sa-in', rows: 2, placeholder: 'Application Audience (AUD) tag; several: one per line', style: { fontFamily: 'monospace', fontSize: '12px' } }, (d.auds || []).join('\n'));
    const header = h('input', { class: 'sa-in', value: v.AUTH_PROXY_HEADER, placeholder: 'default: Cf-Access-Authenticated-User-Email / Remote-User, X-Forwarded-User…', autocomplete: 'off' });
    const trusted = h('input', { class: 'sa-in', value: v.AUTH_PROXY_TRUSTED, placeholder: 'default: 127.0.0.1, ::1', autocomplete: 'off' });
    const create = h('select', { class: 'sa-in' }, opt(NO_ACCOUNT, (v.AUTH_PROXY_CREATE || '').toLowerCase()));
    const cf = h('div', { class: 'sa-grid' }, fld('Cloudflare team domain', team), fld('AUD tag(s)', aud));
    const other = h('div', { class: 'sa-grid' }, fld('User header', header), fld('Proxy addresses', trusted));
    const rest = h('div', { class: 'sa-grid' }, fld('Someone with no account', create));
    const show = () => { cf.style.display = mode.value === 'cloudflare' ? '' : 'none'; other.style.display = rest.style.display = mode.value === 'off' ? 'none' : ''; };
    mode.onchange = show;
    const seen = d.seen && d.seen.iss ? h('p', { class: 'sa-muted sa-small' }, `Last request through Cloudflare (unverified): team ${String(d.seen.iss).replace(/^https:\/\//, '')}, AUD ${[].concat(d.seen.aud || []).join(', ')}${d.seen.email ? ', ' + d.seen.email : ''}. `,
      h('button', { class: 'sa-link', onclick: () => {
        mode.value = 'cloudflare'; show(); team.value = String(d.seen.iss).replace(/^https:\/\//, '');
        const tags = aud.value.split(/[\s,]+/).filter(Boolean); [].concat(d.seen.aud || []).forEach((t) => { if (!tags.includes(t)) tags.push(t); }); aud.value = tags.join('\n');
      } }, 'Use these')) : null;
    const here = d.here || {};
    const state = here.mode === 'off' || !here.mode ? 'Off: everyone uses the suite’s own sign-in.' : here.mode === 'cloudflare'
      ? (here.cloudflareVerified ? `On: Cloudflare Access tokens are verified (${(here.auds || []).length} AUD tag${(here.auds || []).length === 1 ? '' : 's'}).` : 'On, but without the team domain and AUD tag: nobody is signed in through Cloudflare.')
      : `On (${here.mode}): the user header is believed from ${(here.trusted || []).join(', ')}.`;
    const save = async () => {
      const body = { AUTH_PROXY: mode.value === 'off' ? '' : mode.value, CF_ACCESS_TEAM: team.value.trim(), CF_ACCESS_AUD: aud.value.split(/[\s,]+/).filter(Boolean).join(', '),
        AUTH_PROXY_HEADER: header.value.trim(), AUTH_PROXY_TRUSTED: trusted.value.trim(), AUTH_PROXY_CREATE: create.value };
      try { d = await api('proxy', { method: 'PUT', json: body }); toast('Saved for every app'); draw(); } catch (e) { fail(e); }
    };
    el.replaceChildren(box('key', 'Sign-in at the proxy',
      h('p', { class: 'sa-muted' }, 'Someone signed in at your reverse proxy (Cloudflare Access on a tunnel, or Authelia, Authentik or oauth2-proxy behind HAProxy, nginx, Caddy or Traefik) is signed in to every io app, matched to an account by email, then name. One setting for the whole suite, kept in the Shared ioSuite database and read only from there: proxy settings in an app’s config.env or environment are ignored.'),
      h('p', null, h('b', null, state)),
      h('div', { class: 'sa-grid' }, fld('Proxy', mode)), cf, seen,
      h('p', { class: 'sa-muted sa-small' }, 'Team domain: Zero Trust › Settings › Team name and domain. AUD tag: Zero Trust › Access › Applications › the application › Overview. One application covering every app’s host has one tag; with one application per app, list each one’s tag.'),
      other, rest,
      h('div', { class: 'sa-btns' }, h('button', { class: 'sa-btn primary', onclick: save }, 'Save for every app'))));
    show();
  };
  draw();
}
