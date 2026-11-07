"""Sessions shared by every app: a random token in an HttpOnly cookie, its hash in the sessions table.

The cookie is set for COOKIE_DOMAIN when the request's host is under it (photo.example.com and
notes.example.com then share it), and for the host alone otherwise (a LAN address or name). A session is renewed
once a day while in use, so a device used at least once a month stays signed in; "sign out everywhere" deletes
every session of the account.
"""
import hashlib
import secrets
import time

from iosuite_shared import config, db, users

COOKIE = config.COOKIE
VIA_COOKIE = "iothing_via"  # how the session began (password, oneclick, cloudflare, proxy): shown in the account dialog only
SESSION_DAYS = config.SESSION_DAYS

_cache: dict = {}  # token -> (at, user): the cookie is checked on every request, a page load makes dozens
CACHE_S = 3.0


def invalidate():
    _cache.clear()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def describe(request, proxy=None) -> dict:
    """Where a request comes from, for the sessions table: {ip, cloudflare, client, user_agent}. The address is the
    visitor's real one when a trusted proxy or Cloudflare reports it; the client is "web" for a browser, the product
    token of an app's User-Agent otherwise (the iOS and Mac apps send "iopix/<build> CFNetwork/..."), or "other"."""
    h = request.headers
    ua = (h.get("user-agent") or "")[:255]
    if proxy is not None:
        ip, cf = proxy.visitor_of(request), proxy.external(h, "") and any(h.get(x) for x in ("cf-ray", "cf-connecting-ip", "cf-access-jwt-assertion"))
    else:
        ip, cf = (request.client.host if request.client else ""), bool(h.get("cf-ray"))
    return {"ip": (ip or "")[:64], "cloudflare": bool(cf), "client": classify(ua), "user_agent": ua or None}


def classify(ua: str) -> str:
    """web (a browser), the app's product token (iopix/123 → "iopix"), or other."""
    ua = ua or ""
    if not ua:
        return "other"
    if ua.startswith("Mozilla/") or "Safari/" in ua or "Chrome/" in ua or "Firefox/" in ua:
        return "web"
    if "CFNetwork" in ua or "Darwin" in ua:  # URLSession: "<bundle name>/<build> CFNetwork/<v> Darwin/<v>"
        return (ua.split("/")[0].strip().lower() or "app")[:32]
    return ua.split("/")[0].split(" ")[0].strip().lower()[:32] or "other"


def create(user_id: int, app: str, via: str, info: dict = None) -> str:
    """A new session; returns the token for the cookie. info: describe()'s dict."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    i = info or {}
    db.ex("INSERT INTO ios_sessions (token_hash, user_id, app, via, created_at, expires_at, renewed_at, ip, cloudflare, client, user_agent, last_seen_at, last_ip) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
          (_hash(token), user_id, app, via[:16], now, now + SESSION_DAYS * 86400, now, i.get("ip"), 1 if i.get("cloudflare") else 0,
           (i.get("client") or "")[:32], i.get("user_agent"), now, i.get("ip")))
    if now % 50 == 0:  # now and then: expired sessions go
        db.ex("DELETE FROM ios_sessions WHERE expires_at < %s", (now,))
    return token


def end(token: str):
    if token:
        db.ex("DELETE FROM ios_sessions WHERE token_hash = %s", (_hash(token),))
    invalidate()


def end_all(user_id: int):
    db.ex("DELETE FROM ios_sessions WHERE user_id = %s", (user_id,))
    invalidate()


def user_from_token(token: str, app: str):
    """The account a session token stands for (with its role in the app), or None; cached briefly."""
    return lookup(token, app)[0]


SEEN_EVERY = 300  # last_seen / last_ip are written at most this often per session


def lookup(token: str, app: str, info: dict = None) -> tuple:
    """(user, renewed): the account, and whether the session was just given a fresh 30 days (then the cookie is
    set again too, so the browser's copy lasts as long). info (describe()): where it is seen from now."""
    if not token:
        return None, False
    hit = _cache.get((token, app))
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1], False
    r = db.one("SELECT user_id, expires_at, renewed_at, last_seen_at FROM ios_sessions WHERE token_hash = %s", (_hash(token),))
    user, renewed = None, False
    now = int(time.time())
    if r and r["expires_at"] > now:
        user = users.get(r["user_id"], app)
        if user and now - r["renewed_at"] > 86400:
            db.ex("UPDATE ios_sessions SET expires_at = %s, renewed_at = %s WHERE token_hash = %s", (now + SESSION_DAYS * 86400, now, _hash(token)))
            renewed = True
        if user and now - (r["last_seen_at"] or 0) > SEEN_EVERY:
            db.ex("UPDATE ios_sessions SET last_seen_at = %s, last_ip = %s WHERE token_hash = %s", (now, (info or {}).get("ip"), _hash(token)))
    if len(_cache) > 500:
        _cache.clear()
    _cache[(token, app)] = (time.time(), user)
    return user, renewed


def list_sessions(user_id: int, current_token: str = None) -> list:
    """The account's sessions, newest activity first: app, via, client, ip / lastIp, cloudflare, userAgent,
    created / lastSeen / expires (epoch seconds), id (to end one) and current (the one asking)."""
    cur = _hash(current_token) if current_token else None
    return [{"id": r["token_hash"][:16], "app": r["app"], "via": r["via"], "client": r["client"] or "other", "ip": r["ip"], "lastIp": r["last_ip"],
             "cloudflare": bool(r["cloudflare"]), "userAgent": r["user_agent"], "created": r["created_at"], "lastSeen": r["last_seen_at"] or r["renewed_at"],
             "expires": r["expires_at"], "current": r["token_hash"] == cur}
            for r in db.q("SELECT * FROM ios_sessions WHERE user_id = %s ORDER BY coalesce(last_seen_at, renewed_at) DESC", (user_id,))]


def end_one(user_id: int, session_id: str):
    """End one of the account's sessions by the id list_sessions gives."""
    db.ex("DELETE FROM ios_sessions WHERE user_id = %s AND token_hash LIKE %s", (user_id, session_id[:16] + "%"))
    invalidate()


# ---- cookies
def cookie_domain(host: str):
    """The domain attribute for this request's host: the shared domain when the host is under it, else none (host only)."""
    host = (host or "").split(":")[0].strip().lower()
    d = config.COOKIE_DOMAIN
    if d and (host == d or host.endswith("." + d)):
        return d
    return None


def set_cookie(response, token: str, host: str, via: str, secure: bool):
    """secure: the request came over HTTPS (the proxy said so): the cookie is never sent over plain HTTP then."""
    kw = dict(max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/", secure=secure)
    dom = cookie_domain(host)
    if dom:
        kw["domain"] = dom
    response.set_cookie(COOKIE, token, **kw)
    response.set_cookie(VIA_COOKIE, via, **kw)


def clear_cookie(response, host: str, cloudflare_cookie: bool = False):
    """cloudflare_cookie: also drop Cloudflare Access's own session cookie (on the home network, where Cloudflare's
    sign-out page cannot be reached), so the next visit from outside asks Cloudflare's sign-in again."""
    for dom in {cookie_domain(host), None}:
        kw = {"path": "/"}
        if dom:
            kw["domain"] = dom
        response.delete_cookie(COOKIE, **kw)
        response.delete_cookie(VIA_COOKIE, **kw)
    if cloudflare_cookie:
        response.delete_cookie("CF_Authorization", path="/")


def over_https(request) -> bool:
    """Whether the client reached the app over HTTPS: directly, or through a proxy that says so."""
    return request.url.scheme == "https" or (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"


def host_of(request) -> str:
    return request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
