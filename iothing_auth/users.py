"""Accounts: one per person for the whole suite, plus their role in each app and their per-app data.

Roles per app: none (no access there), viewer, editor, admin. An account without a row for an app holds the app's
default_role setting (viewer unless an admin changes it). suite_admin is admin in every app and manages accounts.
A passwordless account (hash '') signs in with one click on the LAN and is never more than a viewer anywhere.

Until the first account exists every app is open, as before (a fresh install is not locked out).
"""
import json
import re
import threading
import time

from iothing_auth import db
from iothing_auth.passwords import burn, check_password, hash_password, needs_rehash

ROLES = ("none", "viewer", "editor", "admin")
APP_ROLES = ("viewer", "editor", "admin")  # what an admin picks for an account in an app ("none" takes the app away)
RANK = {"none": 0, "viewer": 1, "editor": 2, "admin": 3}
COLS = "id, name, display_name, email, hash, suite_admin, listed, created, last_login, session_gen"

_cache = {"any": None, "any_at": 0.0}
CACHE_S = 3.0


def invalidate():
    _cache.update(any=None, any_at=0.0)
    from iothing_auth import sessions
    sessions.invalidate()


def init():
    """The tables, and the admin named by ADMIN_USER / ADMIN_PASSWORD on a first start with no accounts yet."""
    from iothing_auth import config
    db.init()
    if config.ADMIN_USER and config.ADMIN_PASSWORD and not any_users():
        create(config.ADMIN_USER, config.ADMIN_PASSWORD, suite_admin=True)


def any_users() -> bool:
    if _cache["any"] is not None and time.time() - _cache["any_at"] < CACHE_S:
        return _cache["any"]
    v = db.one("SELECT 1 AS x FROM users LIMIT 1") is not None
    _cache.update(any=v, any_at=time.time())
    return v


def row(r, app: str | None = None) -> dict:
    """A users row as the dict the apps serve (never the hash). With an app: `role`, the account's role there."""
    u = {"id": r["id"], "name": r["name"], "displayName": r.get("display_name") or r["name"], "email": r.get("email") or "",
         "passwordless": not r.get("hash"), "suiteAdmin": bool(r.get("suite_admin")), "listed": bool(r.get("listed", 1)),
         "created": r.get("created"), "last_login": r.get("last_login")}
    if app is not None:
        u["role"] = role_of(u, app)
    return u


def clean_email(email) -> str:
    email = (email or "").strip().lower()
    if email and (len(email) > 191 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        raise ValueError("That does not look like an email address")
    return email


def clean_name(name) -> str:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("A user name is needed (up to 64 characters)")
    return name


def get(user_id: int, app: str | None = None):
    r = db.one(f"SELECT {COLS} FROM users WHERE id = %s", (user_id,))
    return row(r, app) if r else None


def find(name_or_email: str, app: str | None = None):
    """By name, then by email (both case-insensitive)."""
    s = (name_or_email or "").strip()
    if not s:
        return None
    r = db.one(f"SELECT {COLS} FROM users WHERE name = %s", (s,)) or db.one(f"SELECT {COLS} FROM users WHERE email = %s", (s.lower(),))
    return row(r, app) if r else None


def list_users(app: str | None = None) -> list:
    return [row(r, app) for r in db.q(f"SELECT {COLS} FROM users ORDER BY id")]


def accounts(app: str | None = None) -> list:
    """The sign-in picker: listed accounts with a role in this app, and whether a password is asked for."""
    out = []
    for r in db.q(f"SELECT {COLS} FROM users WHERE listed = 1 ORDER BY lower(name)"):
        u = row(r, app)
        if app is None or u["role"] != "none":
            out.append({"id": u["id"], "name": u["name"], "displayName": u["displayName"], "passwordless": u["passwordless"]})
    return out


def create(name: str, password: str, role: str | None = None, app: str | None = None, email: str = "", display_name: str = "",
           suite_admin: bool = False) -> dict:
    """A new account; with app and role, that role there too. ValueError with the message to show."""
    name, email = clean_name(name), clean_email(email)
    if role is not None and role not in ROLES:
        raise ValueError("Unknown role")
    if password == "" and (suite_admin or (role or "viewer") not in ("none", "viewer")):
        raise ValueError("Only a viewer can have no password")
    if password != "" and len(password) < 8:
        raise ValueError("The password needs at least 8 characters")
    if find(name):
        raise ValueError("That user name is taken")
    if email and db.one("SELECT 1 AS x FROM users WHERE email = %s", (email,)):
        raise ValueError("Another account already has that email address")
    uid = db.ex("INSERT INTO users (name, display_name, email, hash, suite_admin, created) VALUES (%s, %s, %s, %s, %s, %s)",
                (name, (display_name or "").strip()[:128] or None, email or None, hash_password(password) if password else "", 1 if suite_admin else 0, int(time.time())))
    if app and role:
        set_role(uid, app, role)
    invalidate()
    return get(uid, app)


def update(user_id: int, name=None, password=None, email=None, display_name=None, listed=None, suite_admin=None, role=None, app=None):
    """Change the given fields (None: unchanged). A new password or a changed suite_admin ends the account's other sessions."""
    cur = db.one(f"SELECT {COLS} FROM users WHERE id = %s", (user_id,))
    if not cur:
        raise ValueError("No such user")
    sets = {}
    if email is not None:
        email = clean_email(email)
        if email and db.one("SELECT 1 AS x FROM users WHERE email = %s AND id != %s", (email, user_id)):
            raise ValueError("Another account already has that email address")
        sets["email"] = email or None
    if name is not None:
        name = clean_name(name)
        if db.one("SELECT 1 AS x FROM users WHERE name = %s AND id != %s", (name, user_id)):
            raise ValueError("That user name is taken")
        sets["name"] = name
    if display_name is not None:
        sets["display_name"] = display_name.strip()[:128] or None
    if listed is not None:
        sets["listed"] = 1 if listed else 0
    if suite_admin is not None:
        if suite_admin and not cur["hash"] and password in (None, ""):
            raise ValueError("Set a password first; an account without one can only be a viewer")
        if not suite_admin and cur["suite_admin"] and db.one("SELECT count(*) AS n FROM users WHERE suite_admin = 1 AND id != %s", (user_id,))["n"] == 0:
            raise ValueError("That is the only suite admin; make someone else one first")
        sets["suite_admin"] = 1 if suite_admin else 0
    if password is not None:
        if password == "":  # remove the password: one-click sign-in, viewers only
            still_admin = cur["suite_admin"] if suite_admin is None else suite_admin
            if still_admin or RANK[_max_role(user_id)] > 1:
                raise ValueError("Only a viewer can have no password")
            sets["hash"] = ""
        else:
            if len(password) < 8:
                raise ValueError("The password needs at least 8 characters")
            sets["hash"] = hash_password(password)
    if role is not None:
        if not app:
            raise ValueError("A role needs an app")
        if role not in ROLES:
            raise ValueError("Unknown role")
        if role not in ("none", "viewer") and not (cur["hash"] or sets.get("hash")):
            raise ValueError("Set a password first; an account without one can only be a viewer")
    if sets:
        db.ex("UPDATE users SET " + ", ".join(f"{k} = %s" for k in sets) + " WHERE id = %s", (*sets.values(), user_id))
    if role is not None:
        set_role(user_id, app, role)
    if "hash" in sets or "suite_admin" in sets:
        from iothing_auth import sessions
        sessions.end_all(user_id)
    invalidate()
    return get(user_id, app)


def delete(user_id: int):
    r = db.one("SELECT suite_admin FROM users WHERE id = %s", (user_id,))
    if r and r["suite_admin"] and db.one("SELECT count(*) AS n FROM users WHERE suite_admin = 1")["n"] == 1:
        raise ValueError("That is the only suite admin; make someone else one first")
    db.ex("DELETE FROM users WHERE id = %s", (user_id,))
    invalidate()


def touch_login(user_id: int):
    db.ex("UPDATE users SET last_login = %s WHERE id = %s", (int(time.time()), user_id))


def authenticate(name: str, password: str, app: str | None = None):
    """The account when the password is right (or the account has none), else None; constant time for an unknown name."""
    r = db.one(f"SELECT {COLS} FROM users WHERE name = %s OR email = %s", ((name or "").strip(), (name or "").strip().lower()))
    if not r:
        burn(password)
        return None
    if r["hash"]:
        if not check_password(password, r["hash"]):
            return None
        if needs_rehash(r["hash"]):
            db.ex("UPDATE users SET hash = %s WHERE id = %s", (hash_password(password), r["id"]))
    touch_login(r["id"])
    invalidate()
    return row(r, app)


# ---- roles
def default_role(app: str) -> str:
    v = db.get_setting("default_role", "viewer", app)
    return v if v in ROLES else "viewer"


def role_of(user, app: str) -> str:
    """The role this account holds in the app."""
    if not user:
        return "none"
    if user.get("suiteAdmin"):
        return "admin"
    r = db.one("SELECT role FROM user_roles WHERE user_id = %s AND app = %s", (user["id"], app))
    role = r["role"] if r else default_role(app)
    if user.get("passwordless") and RANK[role] > 1:
        role = "viewer"
    return role


def roles_of(user_id: int) -> dict:
    """{app: role} for every app the account has an explicit role in."""
    return {r["app"]: r["role"] for r in db.q("SELECT app, role FROM user_roles WHERE user_id = %s", (user_id,))}


def set_role(user_id: int, app: str, role: str):
    if role not in ROLES:
        raise ValueError("Unknown role")
    db.ex("INSERT INTO user_roles (user_id, app, role) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE role = VALUES(role)", (user_id, app, role))
    invalidate()


def _max_role(user_id: int, except_app: str = "") -> str:
    best = "none"
    for r in db.q("SELECT role FROM user_roles WHERE user_id = %s AND app != %s", (user_id, except_app)):
        if RANK[r["role"]] > RANK[best]:
            best = r["role"]
    return best


def admins_of(app: str) -> int:
    """How many accounts administer the app (suite admins included)."""
    n = db.one("SELECT count(*) AS n FROM users WHERE suite_admin = 1")["n"]
    n += db.one("SELECT count(*) AS n FROM user_roles r JOIN users u ON u.id = r.user_id WHERE r.app = %s AND r.role = 'admin' AND u.suite_admin = 0", (app,))["n"]
    return n


# ---- permissions (the "no accounts yet: open" rule included)
def is_signed_in(user) -> bool:
    return not any_users() or (user is not None and user.get("role", "viewer") != "none")


def can_write(user) -> bool:
    return not any_users() or (user is not None and user.get("role") in ("editor", "admin"))


def is_admin(user) -> bool:
    return not any_users() or (user is not None and user.get("role") == "admin")


def is_suite_admin(user) -> bool:
    return not any_users() or (user is not None and user.get("suiteAdmin"))


def require_login(app: str) -> bool:
    """Settings › Require sign-in for this app: nothing but the sign-in screen for visitors."""
    return db.get_setting("require_login", "0", app) == "1" and any_users()


# ---- per-user, per-app data (display settings; the ioPix person tag; the ioContacts "me" contact)
def get_data(user_id: int, app: str) -> dict:
    return {r["k"]: db.json_load(r["v"]) for r in db.q("SELECT k, v FROM user_data WHERE user_id = %s AND app = %s", (user_id, app))}


def get_value(user_id: int, app: str, key: str, default=None):
    r = db.one("SELECT v FROM user_data WHERE user_id = %s AND app = %s AND k = %s", (user_id, app, key))
    return db.json_load(r["v"], default) if r else default


def set_value(user_id: int, app: str, key: str, value):
    """Store one value (None removes it)."""
    if value is None:
        db.ex("DELETE FROM user_data WHERE user_id = %s AND app = %s AND k = %s", (user_id, app, key))
    else:
        db.ex("INSERT INTO user_data (user_id, app, k, v) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE v = VALUES(v)",
              (user_id, app, key, json.dumps(value)))


SETTINGS_KEY = "settings"


def get_settings(user_id: int, app: str) -> dict:
    """The user's display preferences in this app (a free-form JSON object the app reads and writes)."""
    v = get_value(user_id, app, SETTINGS_KEY, {})
    return v if isinstance(v, dict) else {}


def set_settings(user_id: int, app: str, changes: dict) -> dict:
    """Merge changes into the preferences (None removes a key; keys and values are limited in size) and return the result."""
    cur = get_settings(user_id, app)
    for k, v in (changes or {}).items():
        if not isinstance(k, str) or len(k) > 64:
            continue
        if v is None:
            cur.pop(k, None)
        elif isinstance(v, (str, int, float, bool)) and len(str(v)) <= 2000:
            cur[k] = v
    set_value(user_id, app, SETTINGS_KEY, cur)
    return cur


# ---- sign-in attempts: a wrong password costs a growing wait, per address and account (in memory: a restart clears them)
_attempts = {}
_attempts_lock = threading.Lock()
LOCK_AFTER = 5
LOCK_AFTER_NAME = 20
LOCK_BASE = 30
LOCK_MAX = 15 * 60


def login_wait(key: str) -> int:
    with _attempts_lock:
        rec = _attempts.get(key)
        if not rec:
            return 0
        return max(0, int(-(-(rec[1] - time.time()) // 1)))


def login_failed(key: str, free: int = LOCK_AFTER) -> int:
    with _attempts_lock:
        if len(_attempts) > 10000:
            now = time.time()
            for k in [k for k, (n, until) in _attempts.items() if until < now - LOCK_MAX]:
                del _attempts[k]
            if len(_attempts) > 10000:
                for k in list(_attempts)[:len(_attempts) // 2]:
                    del _attempts[k]
        rec = _attempts.setdefault(key, [0, 0.0])
        rec[0] += 1
        over = rec[0] - free
        wait = min(LOCK_MAX, LOCK_BASE * 2 ** (over - 1)) if over > 0 else 0
        rec[1] = time.time() + wait
        return wait


def login_succeeded(key: str):
    with _attempts_lock:
        _attempts.pop(key, None)
