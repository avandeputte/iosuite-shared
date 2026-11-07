"""App passwords: one per device, for one app, for clients that cannot use the cookie (CardDAV, LDAP, MCP
clients, HTTP Basic). Shown once, revocable. The form is <app>-<8 hex>-<secret>, so the prefix finds the row.

Scope: what a password may reach. '' (the default) is everything the account may; DAV ('dav') is the app's sync
protocols only (CalDAV / CardDAV, and LDAP in ioContacts), the kind an Apple profile puts on a phone, so a password
lifted from a device cannot reach the REST API, MCP or the web app. Auth enforces it by path (Auth.scope_paths); a
server of its own (LDAP) asks authenticate_scoped and checks the scope itself. READ ('read') may only read (GET and
HEAD, anywhere but MCP and the sync protocols: READ_NEVER): what the apps' own service accounts get to read from each other
(iosuite_shared.links).
"""
import hashlib
import re
import secrets
import time

from iosuite_shared import db, edge, users
from iosuite_shared.passwords import check_password, hash_password

_cache: dict = {}  # a successful check, for a minute: scrypt is slow on purpose and DAV clients send Basic on every request


DAV = "dav"
READ = "read"
READ_NEVER = ("/mcp", "/dav", "/.well-known/")
SCOPES = ("", DAV, READ)


def create(user_id: int, app: str, label: str, scope: str = "") -> str:
    if scope not in SCOPES:
        raise ValueError(f"Unknown app password scope: {scope!r}")
    prefix = secrets.token_hex(4)
    pw = f"{app}-{prefix}-{secrets.token_urlsafe(18)}"
    db.ex("INSERT INTO ios_app_passwords (user_id, app, label, prefix, hash, created_at, scope) VALUES (%s, %s, %s, %s, %s, %s, %s)",
          (user_id, app, (label or "Device")[:128], prefix, hash_password(pw), int(time.time()), scope))
    edge.publish(app, prefix, pw)
    return pw


def list_for(user_id: int, app: str) -> list:
    return [{"id": r["id"], "label": r["label"], "created": r["created_at"], "lastUsed": r["last_used_at"], "scope": r["scope"]}
            for r in db.q("SELECT * FROM ios_app_passwords WHERE user_id = %s AND app = %s ORDER BY created_at", (user_id, app))]


def delete(user_id: int, pid: int):
    gone = [(r["app"], r["prefix"]) for r in db.q("SELECT app, prefix FROM ios_app_passwords WHERE id = %s AND user_id = %s", (pid, user_id))]
    db.ex("DELETE FROM ios_app_passwords WHERE id = %s AND user_id = %s", (pid, user_id))
    _cache.clear()
    edge.unpublish(gone)


def forget_user(user_id: int):
    """Before an account is deleted (its app passwords go with it): their keys off the edge."""
    edge.unpublish([(r["app"], r["prefix"]) for r in db.q("SELECT app, prefix FROM ios_app_passwords WHERE user_id = %s", (user_id,))])
    _cache.clear()


def forget():
    _cache.clear()


def set_scope(app: str, prefix: str, scope: str) -> int:
    """Give an existing password (found by its 8-hex prefix) a scope: for an app moving its own record of scopes here."""
    if scope not in SCOPES:
        raise ValueError(f"Unknown app password scope: {scope!r}")
    n = db.ex("UPDATE ios_app_passwords SET scope = %s WHERE app = %s AND prefix = %s", (scope, app, prefix))
    _cache.clear()
    return n


def authenticate(name: str | None, password: str, app: str):
    """The account for an app password of this app (name optional: a bearer token carries none), else, with a name,
    for the account password. None when neither fits. Ignores the scope: see authenticate_scoped."""
    return authenticate_scoped(name, password, app)[0]


def authenticate_scoped(name: str | None, password: str, app: str):
    """(account, scope) as authenticate finds them: scope is the app password's ('' everything, 'dav'...), '' for the
    account password, None with no account."""
    key = hashlib.sha256(f"{app}\0{name or ''}\0{password}".encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        u = users.get(hit[1], app)
        return (u, hit[2]) if u else (None, None)
    if not password:
        return None, None
    user, scope = None, ""
    m = re.match(r"^([a-z0-9]+)-([0-9a-f]{8})-", password)  # the word is a hint only (ioc- from before the suite): the row says which app
    if m:
        for r in db.q("SELECT * FROM ios_app_passwords WHERE prefix = %s AND app = %s", (m.group(2), app)):
            if check_password(password, r["hash"]):
                u = users.get(r["user_id"], app)
                if u and (not name or name.strip().lower() in (u["name"].lower(), u["email"].lower())):
                    db.ex("UPDATE ios_app_passwords SET last_used_at = %s WHERE id = %s", (int(time.time()), r["id"]))
                    edge.publish(app, r["prefix"], password)  # made before the edge check was on, or since a restart
                    user, scope = u, r.get("scope") or ""
                    break
    if not user and name:
        user = users.authenticate(name, password, app)
        if user and user["passwordless"]:
            user = None  # a one-click account is for the LAN sign-in screen, not for clients
    if user:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time() + 60, user["id"], scope)
    return user, (scope if user else None)
