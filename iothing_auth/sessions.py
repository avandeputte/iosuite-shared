"""Sessions shared by every app: a random token in an HttpOnly cookie, its hash in the sessions table.

The cookie is set for IOTHING_COOKIE_DOMAIN when the request's host is under it (photo.iothing.net and
notes.iothing.net then share it), and for the host alone otherwise (a LAN address or name). A session is renewed
once a day while in use, so a device used at least once a month stays signed in; "sign out everywhere" deletes
every session of the account.
"""
import hashlib
import secrets
import time

from iothing_auth import config, db, users

COOKIE = config.COOKIE
VIA_COOKIE = "iothing_via"  # how the session began (password, oneclick, cloudflare, proxy): shown in the account dialog only
SESSION_DAYS = config.SESSION_DAYS

_cache: dict = {}  # token -> (at, user): the cookie is checked on every request, a page load makes dozens
CACHE_S = 3.0


def invalidate():
    _cache.clear()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(user_id: int, app: str, via: str) -> str:
    """A new session; returns the token for the cookie."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.ex("INSERT INTO sessions (token_hash, user_id, app, via, created, expires, renewed) VALUES (%s, %s, %s, %s, %s, %s, %s)",
          (_hash(token), user_id, app, via[:16], now, now + SESSION_DAYS * 86400, now))
    if now % 50 == 0:  # now and then: expired sessions go
        db.ex("DELETE FROM sessions WHERE expires < %s", (now,))
    return token


def end(token: str):
    if token:
        db.ex("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))
    invalidate()


def end_all(user_id: int):
    db.ex("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    invalidate()


def user_from_token(token: str, app: str):
    """The account a session token stands for (with its role in the app), or None; cached briefly."""
    if not token:
        return None
    hit = _cache.get((token, app))
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    r = db.one("SELECT user_id, expires, renewed FROM sessions WHERE token_hash = %s", (_hash(token),))
    user = None
    now = int(time.time())
    if r and r["expires"] > now:
        user = users.get(r["user_id"], app)
        if user and now - r["renewed"] > 86400:
            db.ex("UPDATE sessions SET expires = %s, renewed = %s WHERE token_hash = %s", (now + SESSION_DAYS * 86400, now, _hash(token)))
    if len(_cache) > 500:
        _cache.clear()
    _cache[(token, app)] = (time.time(), user)
    return user


def list_sessions(user_id: int) -> list:
    return db.q("SELECT app, via, created, expires, renewed FROM sessions WHERE user_id = %s ORDER BY renewed DESC", (user_id,))


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
