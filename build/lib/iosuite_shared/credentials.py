"""Keys and tokens: every credential an app of the suite keeps (an integration's API key or token, the user name and
password one app uses to reach another), kept in the Shared ioSuite database so a suite admin sees and changes them
all from one place (Settings › Suite › Keys and tokens on the start page).

    from iosuite_shared import credentials
    credentials.declare("iosuite", [
        {"name": "ha_token", "label": "Home Assistant token", "hint": "a long-lived access token", "env": "HA_TOKEN"},
    ], adopt=(db.get_setting, db.set_setting))    # once at start: what the app keeps; moves values from its own settings
    token = os.environ.get("HA_TOKEN") or credentials.get("iosuite", "ha_token")
    credentials.put("iosuite", "ha_token", new_value, by=user_name)     # '' or None clears it

Values are write-only from the outside: listing() never returns one, only whether it is set and when it changed.
The app reads its own with get(). A value from the environment (`env`) still wins inside the app; the listing says
when one is set there, so the panel can tell that the stored one is not used.

`declare(..., adopt=(get, set))` is the move from the app's own settings: a value found there while the shared one is
empty is copied over and removed from the app's own settings. It runs at every start and does nothing once moved.
"""
import re
import time

from iosuite_shared import db

NAME = re.compile(r"^[a-z0-9_]{1,64}$")
_cache: dict = {}  # (app, name) -> (value, at)
CACHE_S = 5.0


def forget():
    _cache.clear()


def _name(name) -> str:
    name = str(name or "").strip()
    if not NAME.match(name):
        raise ValueError("Unknown key")
    return name


def declare(app: str, items, adopt=None) -> list:
    """Say which credentials the app keeps: items of {name, label, hint?, env?}. Each gets its row (an empty value
    when it has none yet); label, hint and whether `env` is set in the environment are refreshed every start. With
    `adopt=(get, set)`, values still in the app's own settings move here. Returns the names moved."""
    import os
    moved = []
    for it in items:
        name = _name(it["name"])
        env = str(it.get("env") or "")
        env_set = 1 if env and os.environ.get(env, "").strip() else 0
        db.ex("INSERT INTO ios_credentials (app, name, value, label, hint, env, env_set, updated_at) "
              "VALUES (%s, %s, '', %s, %s, %s, %s, NULL) "
              "ON DUPLICATE KEY UPDATE label = VALUES(label), hint = VALUES(hint), env = VALUES(env), env_set = VALUES(env_set)",
              (app, name, str(it.get("label") or name)[:128], str(it.get("hint") or "")[:255], env[:64], env_set))
        if adopt:
            old_get, old_set = adopt
            key = it.get("old") or name
            try:
                old = old_get(key, "") or ""
            except Exception:  # noqa: BLE001  the app's own settings are away: try again next start
                continue
            if old and not get(app, name):
                put(app, name, old, by="moved from the app's settings")
            if old and get(app, name):
                old_set(key, None)
                moved.append(name)
    forget()
    return moved


def get(app: str, name: str, default: str = "") -> str:
    """The stored value ('' when there is none). Cached a few seconds: some are asked on every request."""
    now = time.time()
    hit = _cache.get((app, name))
    if hit and now - hit[1] < CACHE_S:
        return hit[0] or default
    r = db.one("SELECT value FROM ios_credentials WHERE app = %s AND name = %s", (app, name))
    v = (r or {}).get("value") or ""
    _cache[(app, name)] = (v, now)
    return v or default


def put(app: str, name: str, value, by: str = None) -> None:
    """Store a value (None or '' clears it; the row stays, so the panel still lists the key)."""
    name = _name(name)
    v = "" if value is None else str(value).strip()
    db.ex("INSERT INTO ios_credentials (app, name, value, label, updated_at, updated_by) VALUES (%s, %s, %s, %s, %s, %s) "
          "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at), updated_by = VALUES(updated_by)",
          (app, name, v, name, int(time.time()), (by or "")[:64] or None))
    forget()


def is_set(app: str, name: str) -> bool:
    return bool(get(app, name))


def remove(app: str, name: str = None) -> int:
    """Delete one key's row, or every row of the app (an app that is gone for good)."""
    forget()
    if name is None:
        return db.ex("DELETE FROM ios_credentials WHERE app = %s", (app,))
    return db.ex("DELETE FROM ios_credentials WHERE app = %s AND name = %s", (app, name))


def listing(app: str = None) -> list:
    """Every key (never the value; app '' : the whole suite's; not the app passwords links keeps, which Settings ›
    Suite shows with the service accounts' app passwords): app, appName, name, label, hint, set, env (the variable that overrides it),
    envSet (it is set there, so the stored value is not used), updated, updatedBy."""
    rows = db.q("SELECT c.app, c.name, c.label, c.hint, c.env, c.env_set, c.updated_at, c.updated_by, c.value != '' AS has, "
                "a.name AS app_name FROM ios_credentials c LEFT JOIN ios_apps a ON a.app = c.app"
                " WHERE c.name NOT LIKE 'link\\_%%'" + (" AND c.app = %s" if app is not None else "") + " ORDER BY c.app, c.label",
                (app,) if app is not None else None)
    return [{"app": r["app"], "appName": r["app_name"] or r["app"] or "Whole suite", "name": r["name"], "label": r["label"] or r["name"],
             "hint": r["hint"] or "", "set": bool(r["has"]), "env": r["env"] or "", "envSet": bool(r["env_set"]),
             "updated": r["updated_at"], "updatedBy": r["updated_by"]} for r in rows]
