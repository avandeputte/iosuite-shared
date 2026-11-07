"""Sign-in done by a reverse proxy in front of the apps: Cloudflare Access (a Cloudflare Tunnel with an Access
policy), or HAProxy, nginx, Caddy or Traefik with an authentication step (Authelia, Authentik, oauth2-proxy, basic
auth...). The proxy passes who signed in in a request header; the app takes that person as signed in when the
account exists (matched on the account's email address, then its name).

The header is believed only from the proxy itself, never from anyone who can reach the app directly:
- Cloudflare Access: with CF_ACCESS_TEAM and CF_ACCESS_AUD set, the signed token Cloudflare adds to every request
  (Cf-Access-Jwt-Assertion) is checked against the team's public keys, and the email comes from it.
- otherwise: the request must come straight from one of AUTH_PROXY_TRUSTED (addresses or networks; by default this
  machine only, 127.0.0.1 and ::1), the address the proxy connects from.

Settings: kept once for the whole suite in the Shared ioSuite database (ios_settings, app ''), the only place they are
read from: the start page's Settings › Suite and every app's own Settings change them. config.env and the environment
are never read for them.
CF_ACCESS_AUD holds one AUD tag or several (comma-separated): one Cloudflare Access application covering *.example.com
has one, an application per host one each, and a token for any of them is accepted.
"""
import ipaddress
import logging
import threading
import time

from iosuite_shared import db, users

log = logging.getLogger("iosuite_shared.proxyauth")
authlog = logging.getLogger("iosuite_shared.auth")  # sign-ins, failed attempts and sign-outs, for the apps' server logs

PRESETS = ("cloudflare", "haproxy", "nginx", "caddy", "traefik", "header")
CF_HEADER = "Cf-Access-Authenticated-User-Email"
CF_JWT_HEADER = "Cf-Access-Jwt-Assertion"
GENERIC_HEADERS = ("Remote-User", "X-Forwarded-User", "X-Auth-Request-User", "X-Auth-Request-Email", "X-Forwarded-Email")
KEYS = ("AUTH_PROXY", "AUTH_PROXY_HEADER", "AUTH_PROXY_TRUSTED", "AUTH_PROXY_CREATE", "CF_ACCESS_TEAM", "CF_ACCESS_AUD")
EXTERNAL_HEADERS = ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion")

_jwks = {"client": None, "team": None}
_lock = threading.Lock()
seen = {}  # the last Cloudflare Access token seen (unverified claims), to set this up from: {iss, aud, email, peer, at}
_cache: dict = {}  # identity -> (user, until): a page load makes dozens of requests
_logged: dict = {}


class Proxy:
    """Proxy sign-in for one app (its settings fall back to the suite-wide ones)."""

    def __init__(self, app: str):
        self.app = app

    def opt(self, name: str):
        """The suite-wide value."""
        return db.get_setting(name.lower(), "", "") or None

    def save(self, values: dict):
        """Store proxy sign-in settings (keys of KEYS; empty removes one) suite-wide, for every app."""
        for k, v in values.items():
            if k in KEYS:
                v = str(v or "").strip()
                v = clean_team(v) if k == "CF_ACCESS_TEAM" else ", ".join(aud_tags(v)) if k == "CF_ACCESS_AUD" else v
                db.set_setting(k.lower(), v, "")
        _cache.clear()

    def mode(self) -> str:
        m = (self.opt("AUTH_PROXY") or "off").strip().lower()
        return m if m in PRESETS else "off"

    def headers(self) -> list:
        raw = (self.opt("AUTH_PROXY_HEADER") or "").strip()
        if raw:
            return [h.strip() for h in raw.split(",") if h.strip()]
        return [CF_HEADER] if self.mode() == "cloudflare" else list(GENERIC_HEADERS)

    def trusted(self) -> list:
        raw = (self.opt("AUTH_PROXY_TRUSTED") or "").strip() or "127.0.0.1,::1"
        out = []
        for part in raw.replace(" ", ",").split(","):
            if part:
                try:
                    out.append(ipaddress.ip_network(part, strict=False))
                except ValueError:
                    log.warning("AUTH_PROXY_TRUSTED: %r is not an address or network", part)
        return out

    def cf_settings(self):
        """(team domain, [AUD tags]), or (None, None) unless both are set."""
        team = clean_team(self.opt("CF_ACCESS_TEAM"))
        aud = aud_tags(self.opt("CF_ACCESS_AUD"))
        return (team, aud) if team and aud else (None, None)

    def from_trusted(self, peer: str) -> bool:
        try:
            ip = ipaddress.ip_address((peer or "").split("%")[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.trusted())

    def note(self, headers_in, peer: str):
        """Remember the claims of a Cloudflare Access token (not verified: only to show which team and application
        the requests come through, so they can be set up)."""
        tok = headers_in.get(CF_JWT_HEADER)
        if not tok or not self.from_trusted(peer):
            return
        try:
            import jwt
            c = jwt.decode(tok, options={"verify_signature": False})
            seen.update(iss=c.get("iss"), aud=c.get("aud"), email=c.get("email"), peer=peer, at=int(time.time()))
        except Exception:
            pass

    def external(self, headers_in, peer: str = "") -> bool:
        """Did the request come in from outside? Through Cloudflare (its headers), from a public address, or forwarded
        by a trusted proxy on behalf of a public address. Outside, one-click sign-in to a passwordless account is not
        offered. A forged header can only take that option away from the one who forges it, never grant anything."""
        if any(headers_in.get(h) for h in EXTERNAL_HEADERS):
            return True
        if peer and _public(peer):
            return True
        if peer and self.from_trusted(peer):
            fwd = (headers_in.get("x-forwarded-for") or "").split(",")[0].strip()
            return bool(fwd) and _public(fwd)
        return False

    def visitor_of(self, request) -> str:
        """The address the sign-in throttle counts by: the visitor's own, as a trusted proxy reports it, else the connection's."""
        peer = peer_of(request)
        if peer and self.from_trusted(peer):
            h = request.headers
            fwd = h.get("cf-connecting-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip()
            if fwd:
                return f"{fwd} via {peer}"
        return peer

    def identity(self, headers_in, peer: str):
        """Who the proxy says is asking, or None (not configured, not from the proxy, or no user in the request)."""
        m = self.mode()
        if m == "off":
            return None
        if m == "cloudflare":
            team, aud = self.cf_settings()
            if not team:
                if once("cf-unset"):
                    log.warning("AUTH_PROXY=cloudflare without CF_ACCESS_TEAM and CF_ACCESS_AUD: nobody is signed in through the proxy")
                return None
            token = headers_in.get(CF_JWT_HEADER) or ""
            return _cf_email(token, team, aud) if token else None
        if not self.from_trusted(peer):
            return None
        found = {name: (headers_in.get(name) or "").strip() for name in self.headers()}
        values = {v for v in found.values() if v}
        if len(values) > 1:
            log.warning("proxy sign-in refused: %s carry different identities (set AUTH_PROXY_HEADER to the one header your proxy sets)",
                        ", ".join(n for n, v in found.items() if v))
            return None
        return next(iter(values), None)

    def deny_unknown(self) -> bool:
        return (self.opt("AUTH_PROXY_CREATE") or "").strip().lower() == "deny"

    def user_for(self, ident: str):
        """The account for this identity (email first, then name), created with AUTH_PROXY_CREATE's role in this app
        when there is none and that is allowed; None otherwise."""
        u = users.find(ident, self.app)
        if u:
            return u
        role = (self.opt("AUTH_PROXY_CREATE") or "").strip().lower()
        if role not in ("viewer", "editor"):
            return None
        name = ident.strip()[:64]
        try:
            import secrets
            u = users.create(name, secrets.token_urlsafe(24), role, self.app, email=ident if "@" in ident else "")
            authlog.info("account %s created for a sign-in through the proxy (%s in %s)", name, role, self.app)
            return u
        except ValueError as e:
            log.warning("could not create an account for %s: %s", name, e)
            return None

    def user(self, request):
        """The signed-in user the proxy vouches for, or None (request.state.proxy_ident: who it named)."""
        ident = self.identity(request.headers, peer_of(request))
        request.state.proxy_ident = ident
        if not ident:
            return None
        hit = _cache.get((self.app, ident))
        now = time.time()
        if hit and hit[1] > now:
            return hit[0]
        u = self.user_for(ident)
        _cache[(self.app, ident)] = (u, now + 30)
        if once((self.app, ident)):
            how = "Cloudflare Access" if self.mode() == "cloudflare" else f"the proxy ({self.mode()})"
            if u:
                authlog.info("signed in by %s: %s as %s (%s) from %s", how, ident, u["name"], u["role"], self.source(request))
            elif self.deny_unknown():
                authlog.warning("%s let in %s, who has no account here: access denied (from %s)", how, ident, self.source(request))
            else:
                authlog.warning("%s let in %s, who has no account here: shown the sign-in screen (from %s)", how, ident, self.source(request))
        return u

    def source(self, request) -> str:
        h = request.headers
        if any(h.get(x) for x in EXTERNAL_HEADERS):
            return f"{h.get('cf-connecting-ip') or '?'} through Cloudflare"
        return peer_of(request) or "?"

    def status(self) -> dict:
        """For Settings and diagnostics: how proxy sign-in is set up (no secrets)."""
        team, aud = self.cf_settings()
        return {"values": {k: (self.opt(k) or "") for k in KEYS}, "auds": aud or [],
                "mode": self.mode(), "headers": self.headers(), "trusted": [str(n) for n in self.trusted()], "seen": dict(seen),
                "team": team, "aud": ", ".join(aud or []), "cloudflareVerified": bool(team), "create": (self.opt("AUTH_PROXY_CREATE") or "").strip().lower() or None}


# ---- the values, cleaned and compared
def clean_team(v) -> str:
    """A team domain as stored: no scheme, no trailing slash, lower case (myteam.cloudflareaccess.com)."""
    return (v or "").strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")


def aud_tags(v) -> list:
    """The AUD tags in a CF_ACCESS_AUD value (comma- or space-separated), in order, without repeats."""
    out = []
    for t in str(v or "").replace(",", " ").split():
        if t not in out:
            out.append(t)
    return out


def suite_status() -> dict:
    """The suite-wide proxy sign-in settings, for the start page's Settings › Suite (none of these is a secret)."""
    values = {k: db.settings_of("").get(k.lower(), "") for k in KEYS}
    return {"values": values, "auds": aud_tags(values["CF_ACCESS_AUD"]), "seen": dict(seen), "presets": list(PRESETS)}


def save_suite(values: dict) -> dict:
    """Store suite-wide values (keys of KEYS; empty removes one), checked. Raises ValueError."""
    changes = {k: str(v or "").strip() for k, v in values.items() if k in KEYS}
    m = changes.get("AUTH_PROXY")
    if m is not None and m.lower() not in ("", "off", *PRESETS):
        raise ValueError("Unknown proxy")
    if changes.get("AUTH_PROXY_CREATE", "").lower() not in ("", "viewer", "editor", "deny"):
        raise ValueError("Someone with no account: the sign-in screen, deny, or a viewer or editor account")
    if "CF_ACCESS_TEAM" in changes:
        changes["CF_ACCESS_TEAM"] = clean_team(changes["CF_ACCESS_TEAM"])
    if "CF_ACCESS_AUD" in changes:
        changes["CF_ACCESS_AUD"] = ", ".join(aud_tags(changes["CF_ACCESS_AUD"]))
    cur = {k: db.settings_of("").get(k.lower(), "") for k in KEYS}
    cur.update(changes)
    if (cur["AUTH_PROXY"] or "").lower() == "cloudflare" and bool(cur["CF_ACCESS_TEAM"]) != bool(cur["CF_ACCESS_AUD"]):
        raise ValueError("Cloudflare needs both the team domain and the AUD tag (or neither)")
    for k, v in changes.items():
        db.set_setting(k.lower(), v, "")
    _cache.clear()
    return suite_status()


def forget_identities():
    _cache.clear()


def once(key, seconds: float = 3600) -> bool:
    """True when this key has not been logged in the last `seconds` (and records it now)."""
    now = time.time()
    if now - _logged.get(key, 0) > seconds:
        _logged[key] = now
        return True
    return False


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified)


def peer_of(request) -> str:
    """The address the connection really came from (before uvicorn's forwarded-header rewrite)."""
    return request.scope.get("iothing.peer") or (request.client.host if request.client else "")


def _cf_email(token: str, team: str, aud):
    """The email in a Cloudflare Access token, once its signature, audience (any of the AUD tags), issuer and expiry check out."""
    import jwt
    with _lock:
        if _jwks["team"] != team:
            _jwks.update(team=team, client=jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600))
        client = _jwks["client"]
    try:
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{team}", leeway=30, options={"require": ["exp"]})
    except Exception as e:
        if once("refused", 60):
            authlog.warning("Cloudflare Access token refused (%s): treated as not signed in", str(e)[:200])
        return None
    return (claims.get("email") or claims.get("common_name") or "").strip() or None


def remember_peer():
    """uvicorn rewrites the client address from X-Forwarded-For for a trusted proxy (its --forwarded-allow-ips) before
    the app sees the request; keep the address the connection really came from, which is what proxy sign-in trusts.
    Call it before the app is created."""
    try:
        from uvicorn.middleware import proxy_headers
    except ImportError:
        return
    cls = proxy_headers.ProxyHeadersMiddleware
    if getattr(cls, "_iothing_peer", False):
        return
    original = cls.__call__

    async def call(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket") and scope.get("client"):
            scope["iothing.peer"] = scope["client"][0]
        return await original(self, scope, receive, send)
    cls.__call__ = call
    cls._iothing_peer = True
