// ioSuite edge check: a Cloudflare Worker on the DAV paths of ioCalendar and ioContacts (calendar.example.com/dav*,
// contacts.example.com/dav*). A request goes on to the server only with a Basic app password the app has published
// to the Workers KV namespace bound as APP_PASSWORDS (iosuite_shared.edge): key <app>:<prefix>, value {"h": sha-256 hex}.
// Everything else gets the same 401 the server would give, here, at the edge. The server still checks every password.
//
// Variables (wrangler.toml [vars], or Settings › Variables in the dashboard):
//   APPS  JSON, the host each app is reached at: {"calendar.example.com": "iocalendar", "contacts.example.com": "iocontacts"}
//   MODE  "monitor" (default): check and log, but let everything through; "enforce": turn away what fails.

const REALMS = { iocalendar: "ioCalendar", iocontacts: "ioContacts" };
const APP_PASSWORD = /^([a-z0-9]+)-([0-9a-f]{8})-/; // as the server reads it: the word is a hint, the 8 hex find the row

function unauthorized(app) {
  const realm = REALMS[app] || "ioSuite";
  return new Response(`Sign in with your ${realm} name and an app password.`, {
    status: 401,
    headers: { "WWW-Authenticate": `Basic realm="${realm}", charset="UTF-8"`, "Content-Type": "text/plain; charset=utf-8" },
  });
}

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameString(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

function basicPassword(header) {
  const m = /^Basic\s+([A-Za-z0-9+/=]+)\s*$/i.exec(header || "");
  if (!m) return null;
  let decoded;
  try { decoded = atob(m[1]); } catch { return null; }
  const i = decoded.indexOf(":");
  return i < 0 ? null : decoded.slice(i + 1);
}

// "pass" or the reason it fails
export async function check(request, env, app) {
  if (!app) return "host not in APPS";
  const password = basicPassword(request.headers.get("Authorization"));
  if (password === null) return "no Basic credentials";
  const m = APP_PASSWORD.exec(password);
  if (!m) return "not an app password";
  const entry = await env.APP_PASSWORDS.get(`${app}:${m[2]}`, { type: "json", cacheTtl: 60 });
  if (!entry || !entry.h) return "unknown app password";
  return sameString(await sha256hex(password), entry.h) ? "pass" : "wrong app password";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!/^\/dav(\/|$)/.test(url.pathname)) return fetch(request); // a route wider than /dav*: not ours to check
    let apps = {};
    try { apps = JSON.parse(env.APPS || "{}"); } catch { /* checked below as an unknown host */ }
    const app = apps[url.hostname.toLowerCase()];
    const result = await check(request, env, app);
    if (result === "pass") return fetch(request);
    const enforce = (env.MODE || "monitor").toLowerCase() === "enforce";
    if (result !== "no Basic credentials") console.log(JSON.stringify({ edge: enforce ? "blocked" : "would block", reason: result, host: url.hostname,
      path: url.pathname, method: request.method, ip: request.headers.get("CF-Connecting-IP") }));
    // a request with no credentials is the client asking how to sign in: answer it here either way
    if (enforce || result === "no Basic credentials") return unauthorized(app);
    return fetch(request);
  },
};
