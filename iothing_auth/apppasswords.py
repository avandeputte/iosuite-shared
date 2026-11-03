"""App passwords: one per device, for one app, for clients that cannot use the cookie (CardDAV, LDAP, MCP
clients, HTTP Basic). Shown once, revocable. The form is <app>-<8 hex>-<secret>, so the prefix finds the row.
"""
import hashlib
import re
import secrets
import time

from iothing_auth import db, users
from iothing_auth.passwords import check_password, hash_password

_cache: dict = {}  # a successful check, for a minute: scrypt is slow on purpose and DAV clients send Basic on every request


def create(user_id: int, app: str, label: str) -> str:
    prefix = secrets.token_hex(4)
    pw = f"{app}-{prefix}-{secrets.token_urlsafe(18)}"
    db.ex("INSERT INTO app_passwords (user_id, app, label, prefix, hash, created) VALUES (%s, %s, %s, %s, %s, %s)",
          (user_id, app, (label or "Device")[:128], prefix, hash_password(pw), int(time.time())))
    return pw


def list_for(user_id: int, app: str) -> list:
    return [{"id": r["id"], "label": r["label"], "created": r["created"], "lastUsed": r["last_used"]}
            for r in db.q("SELECT * FROM app_passwords WHERE user_id = %s AND app = %s ORDER BY created", (user_id, app))]


def delete(user_id: int, pid: int):
    db.ex("DELETE FROM app_passwords WHERE id = %s AND user_id = %s", (pid, user_id))
    _cache.clear()


def forget():
    _cache.clear()


def authenticate(name: str | None, password: str, app: str):
    """The account for an app password of this app (name optional: a bearer token carries none), else, with a name,
    for the account password. None when neither fits."""
    key = hashlib.sha256(f"{app}\0{name or ''}\0{password}".encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return users.get(hit[1], app)
    if not password:
        return None
    user = None
    m = re.match(r"^([a-z0-9]+)-([0-9a-f]{8})-", password)
    if m and m.group(1) == app:
        for r in db.q("SELECT * FROM app_passwords WHERE prefix = %s AND app = %s", (m.group(2), app)):
            if check_password(password, r["hash"]):
                u = users.get(r["user_id"], app)
                if u and (not name or name.strip().lower() in (u["name"].lower(), u["email"].lower())):
                    db.ex("UPDATE app_passwords SET last_used = %s WHERE id = %s", (int(time.time()), r["id"]))
                    user = u
                    break
    if not user and name:
        user = users.authenticate(name, password, app)
        if user and user["passwordless"]:
            user = None  # a one-click account is for the LAN sign-in screen, not for clients
    if user:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time() + 60, user["id"])
    return user
