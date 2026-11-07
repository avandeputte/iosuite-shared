"""Suite administration: everything in the Shared ioSuite database, seen and changed from one place by a suite admin.

Accounts (with their role in every app), app passwords of every account in every app, sessions, and the suite's apps
with the per-app access settings (default_role, require_login). `Auth.admin_router()` serves it at /api/suite-admin/*
(suite admins only); the start page's Settings › Suite is its screen.

The apps are the registered ones (ios_apps) plus any app key that only shows up in roles, settings or app passwords
(an app that is not installed any more, or has not started since the registry came in).
"""
import re
import time

from iosuite_shared import apppasswords, db, sessions, suite, users

APP_KEY = re.compile(r"^[a-z0-9_-]{1,32}$")
DEFAULT_ROLES = ("none", "viewer", "editor")  # what an app gives an account without a role there ("admin" by default is never wanted)


def clean_app(app) -> str:
    app = str(app or "").strip().lower()
    if not APP_KEY.match(app) or app.startswith("_"):
        raise ValueError("Unknown app")
    return app


def _forget_caches():
    users.invalidate()
    sessions.invalidate()
    apppasswords.forget()
    from iosuite_shared import proxyauth
    proxyauth.forget_identities()


# ---- apps
def apps() -> list:
    """Every app the database knows: the registry's fields (registered: False for one known only from its data), its
    defaultRole and requireLogin, and how many explicit roles and app passwords it has."""
    reg = {a["app"]: a for a in suite.apps()}
    keys = set(reg)
    for sql in ("SELECT DISTINCT app FROM ios_user_roles", "SELECT DISTINCT app FROM ios_app_passwords",
                "SELECT DISTINCT app FROM ios_settings WHERE app != '' AND LEFT(app, 1) != '_'"):
        keys |= {r["app"] for r in db.q(sql)}
    roles = {r["app"]: r["n"] for r in db.q("SELECT app, count(*) AS n FROM ios_user_roles GROUP BY app")}
    pws = {r["app"]: r["n"] for r in db.q("SELECT app, count(*) AS n FROM ios_app_passwords GROUP BY app")}
    out = []
    for k in keys:
        a = dict(reg.get(k) or {"app": k, "name": k, "description": None, "version": None, "color": None, "icon": None, "url": None,
                                "lastSeen": None})
        a["registered"] = k in reg
        a["defaultRole"] = users.default_role(k)
        a["requireLogin"] = db.get_setting("require_login", "0", k) == "1"
        a["roles"], a["appPasswords"] = roles.get(k, 0), pws.get(k, 0)
        out.append(a)
    return sorted(out, key=lambda a: (not a["registered"], (a["name"] or a["app"]).lower(), a["app"]))


def set_app(app: str, default_role=None, require_login=None) -> dict:
    """Change an app's access settings (None: unchanged)."""
    app = clean_app(app)
    if default_role is not None:
        if default_role not in DEFAULT_ROLES:
            raise ValueError("The default role is none, viewer or editor")
        db.set_setting("default_role", "" if default_role == "viewer" else default_role, app)  # viewer is the default: no row
    if require_login is not None:
        db.set_setting("require_login", "1" if require_login else "", app)
    _forget_caches()
    return next((a for a in apps() if a["app"] == app), {"app": app})


def forget_app(app: str, data: bool = False) -> bool:
    """Take an app off the registry; with data, also its roles, its access settings and its app passwords (an app that
    was uninstalled for good). It registers again if it starts."""
    app = clean_app(app)
    gone = suite.forget(app)
    if data:
        for pid, uid in [(r["id"], r["user_id"]) for r in db.q("SELECT id, user_id FROM ios_app_passwords WHERE app = %s", (app,))]:
            apppasswords.delete(uid, pid)
        gone = (db.ex("DELETE FROM ios_user_roles WHERE app = %s", (app,)) > 0) or gone
        gone = (db.ex("DELETE FROM ios_settings WHERE app = %s", (app,)) > 0) or gone
        _forget_caches()
    return gone


# ---- accounts
def accounts() -> list:
    """Every account: the shared fields, `roles` (the explicit role per app), `effective` (the role it holds in every
    known app: the row, else the app's default; suite admin: admin; passwordless: viewer at most), its number of app
    passwords and sessions, and when it was last seen in any session."""
    keys = [a["app"] for a in apps()]
    pw = {r["user_id"]: r["n"] for r in db.q("SELECT user_id, count(*) AS n FROM ios_app_passwords GROUP BY user_id")}
    now = int(time.time())
    ses = {r["user_id"]: r for r in db.q("SELECT user_id, count(*) AS n, max(coalesce(last_seen_at, renewed_at)) AS seen "
                                         "FROM ios_sessions WHERE expires_at > %s GROUP BY user_id", (now,))}
    roles = {}
    for r in db.q("SELECT user_id, app, role FROM ios_user_roles"):
        roles.setdefault(r["user_id"], {})[r["app"]] = r["role"]
    defaults = {k: users.default_role(k) for k in keys}
    out = []
    for u in users.list_users():
        mine = roles.get(u["id"], {})
        eff = {}
        for k in keys:
            role = "admin" if u["suiteAdmin"] else mine.get(k, defaults[k])
            if u["passwordless"] and users.RANK[role] > 1:
                role = "viewer"
            eff[k] = role
        s = ses.get(u["id"]) or {}
        out.append({**u, "roles": mine, "effective": eff, "appPasswords": pw.get(u["id"], 0), "sessions": int(s.get("n") or 0),
                    "lastSeen": s.get("seen")})
    return sorted(out, key=lambda u: u["name"].lower())


def account(user_id: int) -> dict | None:
    return next((u for u in accounts() if u["id"] == user_id), None)


def set_roles(me, user_id: int, roles: dict):
    """{app: role | None}: None (or "default") removes the row, so the app's default role applies."""
    if not isinstance(roles, dict):
        raise ValueError("roles is {app: role}")
    for app, role in roles.items():
        app = clean_app(app)
        if role in (None, "", "default"):
            db.ex("DELETE FROM ios_user_roles WHERE user_id = %s AND app = %s", (user_id, app))
        else:
            users.update(user_id, role=str(role), app=app)  # the checks: a known role, a password for more than viewer


def create(me, body: dict) -> dict:
    roles = body.get("roles") or {}
    u = users.create(str(body.get("name", ""))[:64], str(body.get("password") or "")[:1024], email=str(body.get("email") or ""),
                     display_name=str(body.get("displayName") or ""), suite_admin=bool(body.get("suiteAdmin")))
    try:
        if body.get("listed") is not None:
            users.update(u["id"], listed=bool(body["listed"]))
        set_roles(me, u["id"], roles)
    except ValueError:
        users.delete(u["id"])
        raise
    _forget_caches()
    return account(u["id"])


def update(me, user_id: int, body: dict) -> dict:
    """Change an account: name, displayName, email, listed, suiteAdmin, password ('' removes it: one-click viewer),
    roles {app: role | None}."""
    target = users.get(user_id)
    if not target:
        raise LookupError("No such user")
    own = bool(me) and me.get("id") == user_id
    if own and body.get("suiteAdmin") is False:
        raise ValueError("You cannot take away your own suite admin while signed in; another suite admin can")
    if own and body.get("password") == "":
        raise ValueError("You cannot remove your own password")
    pw = body.get("password")
    users.update(user_id, name=body.get("name"), email=body.get("email"), display_name=body.get("displayName"),
                 listed=None if body.get("listed") is None else bool(body["listed"]),
                 suite_admin=None if body.get("suiteAdmin") is None else bool(body["suiteAdmin"]),
                 password=None if pw is None else str(pw)[:1024])
    if body.get("roles") is not None:
        set_roles(me, user_id, body["roles"])
    _forget_caches()
    return account(user_id)


def delete(me, user_id: int):
    if not users.get(user_id):
        raise LookupError("No such user")
    if me and me.get("id") == user_id:
        raise ValueError("You cannot delete your own account while signed in")
    users.delete(user_id)
    _forget_caches()


# ---- app passwords
def app_passwords(user_id: int = None, app: str = None) -> list:
    """Every app password (never the secret): id, userId, userName, app, appName, label, scope ('' everything, 'dav' sync only), created, lastUsed."""
    where, args = [], []
    if user_id is not None:
        where.append("p.user_id = %s"); args.append(user_id)
    if app:
        where.append("p.app = %s"); args.append(clean_app(app))
    rows = db.q("SELECT p.id, p.user_id, p.app, p.label, p.scope, p.created_at, p.last_used_at, u.name, u.display_name, a.name AS app_name "
                "FROM ios_app_passwords p JOIN ios_users u ON u.id = p.user_id LEFT JOIN ios_apps a ON a.app = p.app"
                + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY lower(u.name), p.app, p.created_at", tuple(args))
    return [{"id": r["id"], "userId": r["user_id"], "userName": r["name"], "displayName": r["display_name"] or r["name"], "app": r["app"],
             "appName": r["app_name"] or r["app"], "label": r["label"], "scope": r["scope"] or "", "created": r["created_at"], "lastUsed": r["last_used_at"]} for r in rows]


def new_app_password(user_id: int, app: str, label: str, scope: str = "") -> dict:
    """A new app password for an account in an app (shown once)."""
    u = users.get(user_id)
    if not u:
        raise LookupError("No such user")
    if u["passwordless"]:
        raise ValueError("A one-click account cannot have app passwords; give it a password first")
    app = clean_app(app)
    pw = apppasswords.create(user_id, app, (label or "Device").strip()[:128] or "Device", scope or "")
    return {"password": pw, "name": u["name"], "app": app, "scope": scope or ""}


def revoke_app_password(pid: int):
    r = db.one("SELECT user_id FROM ios_app_passwords WHERE id = %s", (pid,))
    if not r:
        raise LookupError("No such app password")
    apppasswords.delete(r["user_id"], pid)


# ---- sessions
def end_sessions(user_id: int, session_id: str = None):
    if not users.get(user_id):
        raise LookupError("No such user")
    if session_id:
        sessions.end_one(user_id, session_id)
    else:
        sessions.end_all(user_id)


def overview() -> dict:
    return {"apps": apps(), "users": accounts(), "roles": list(users.ROLES), "defaultRoles": list(DEFAULT_ROLES),
            "scopes": list(apppasswords.SCOPES)}
