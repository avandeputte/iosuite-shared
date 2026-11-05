"""The suite's apps: one row per installed app, which each app writes itself, so the others can find it.

Every app installs on its own (its own box, port, database), so nothing but this table knows where the others are.
An app registers when it starts (`Auth(..., version=..., public_url=...)` does it, or `suite.register()` by hand) and
the middleware notes the address it is reached at. The start page (and any app switcher) reads `suite.apps()` or
`GET /api/auth/suite` and links to each one without anybody typing an address.

Where an app is (`url` in what `apps()` answers), in this order:
  1. the address an app's configuration names (its PUBLIC_URL): `configured_url`;
  2. failing that, the address it was reached at, as browsers see it (`seen_url`): a host under the suite's cookie
     domain over HTTPS beats a LAN name, which beats an IP address; loopback and test hosts are never kept.

A suite admin may forget an app that was uninstalled (`DELETE /api/auth/suite/{app}`); it comes back the next time it
starts.
"""
import ipaddress
import threading
import time

from iosuite_shared import config, db

FIELDS = ("name", "description", "version", "color", "icon")
SEEN_EVERY_S = 3600  # last_seen is refreshed at most this often per app (the address itself is written when it changes)

_lock = threading.Lock()
_seen: dict = {}  # app -> (seen_url as stored, rank, at)


def _clip(v, n: int):
    v = (str(v).strip() if v is not None else "")[:n]
    return v or None


def clean_url(url) -> str | None:
    """http(s)://host[:port][/path] without a trailing slash, or None. Raises ValueError for anything else."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return None
    if not (u.startswith("http://") or u.startswith("https://")) or len(u) > 512 or any(c in u for c in " \"'<>"):
        raise ValueError(f"not an http(s) address: {u[:80]}")
    return u


def register(app: str, name: str = None, url: str = None, version: str = None, description: str = None,
             color: str = None, icon: str = None) -> dict:
    """Write (or refresh) this app's row: its display name, the address its configuration names (None: none, the
    address it is reached at is used), its version, a line on what it is, its colour (#rrggbb) and an icon path on the
    app (/static/icon.svg) or a full URL. Called at every start; whatever the app no longer says is cleared."""
    if not app or len(app) > 32:
        raise ValueError("an app key of 1 to 32 characters")
    with _lock:
        _seen.pop(app, None)
    now = int(time.time())
    row = {"name": _clip(name or app, 64), "description": _clip(description, 255), "version": _clip(version, 32),
           "color": _clip(color, 16), "icon": _clip(icon, 512), "configured_url": clean_url(url)}
    db.ex("""INSERT INTO suite_apps (app, name, description, version, color, icon, configured_url, registered, updated, last_seen)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON DUPLICATE KEY UPDATE name = VALUES(name), description = VALUES(description), version = VALUES(version),
               color = VALUES(color), icon = VALUES(icon), configured_url = VALUES(configured_url),
               updated = VALUES(updated), last_seen = VALUES(last_seen)""",
          (app, row["name"], row["description"], row["version"], row["color"], row["icon"], row["configured_url"], now, now, now))
    return get(app)


def _rank(origin: str) -> int:
    """How good an address is for the others to link to: -1 never (loopback, test hosts), 0 an IP address, 1 a LAN
    name, 2 a host under the cookie domain; +1 over HTTPS."""
    scheme, _, rest = origin.partition("://")
    host = rest.rsplit("@", 1)[-1]
    host = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
    host = host.lower()
    if not host or host in ("localhost", "testserver", "testclient") or host.endswith(".localhost"):
        return -1
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_unspecified:
            return -1
        r = 0
    except ValueError:
        d = config.COOKIE_DOMAIN
        r = 2 if d and (host == d or host.endswith("." + d)) else 1
    return r + (1 if scheme == "https" else 0)


def origin_of(request) -> str:
    """scheme://host[:port] as the browser used it (through a proxy: X-Forwarded-Proto / -Host)."""
    h = request.headers
    host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip()
    proto = (h.get("x-forwarded-proto") or "").split(",")[0].strip().lower() or request.url.scheme
    return f"{proto}://{host}" if host else ""


def note_seen(app: str, request) -> None:
    """The middleware calls this on every request: keep the best address the app is reached at, and when it was last
    up. Cheap: a database write only when the address gets better or once an hour."""
    origin = origin_of(request)
    if not origin:
        return
    rank, now = _rank(origin), time.time()
    with _lock:
        known = _seen.get(app)
        if known is None:
            r = db.one("SELECT seen_url FROM suite_apps WHERE app = %s", (app,))
            stored = r["seen_url"] if r else None
            known = (stored, _rank(stored) if stored else -1, 0.0)
        better = rank >= 0 and origin != known[0] and rank >= known[1]
        if not better and now - known[2] < SEEN_EVERY_S:
            return
        _seen[app] = (origin if better else known[0], rank if better else known[1], now)
    if better:
        n = db.ex("UPDATE suite_apps SET seen_url = %s, last_seen = %s WHERE app = %s", (origin, int(now), app))
    else:
        n = db.ex("UPDATE suite_apps SET last_seen = %s WHERE app = %s", (int(now), app))
    if not n:  # not registered (yet): try again on a later request
        with _lock:
            _seen.pop(app, None)


def _out(r: dict) -> dict:
    url = r["configured_url"] or r["seen_url"]
    icon = r["icon"]
    if icon and url and not icon.startswith(("http://", "https://")):
        icon = url + "/" + icon.lstrip("/")
    return {"app": r["app"], "name": r["name"], "description": r["description"], "version": r["version"],
            "color": r["color"], "icon": icon, "url": url, "configuredUrl": r["configured_url"], "seenUrl": r["seen_url"],
            "registered": r["registered"], "updated": r["updated"], "lastSeen": r["last_seen"]}


def apps() -> list:
    """Every registered app: {app, name, description, version, color, icon (absolute), url, configuredUrl, seenUrl,
    registered, updated, lastSeen}, by name."""
    return [_out(r) for r in db.q("SELECT * FROM suite_apps ORDER BY name, app")]


def get(app: str) -> dict | None:
    r = db.one("SELECT * FROM suite_apps WHERE app = %s", (app,))
    return _out(r) if r else None


def url_of(app: str) -> str | None:
    r = get(app)
    return r["url"] if r else None


def forget(app: str) -> bool:
    """Remove an app (uninstalled); it registers again when it starts."""
    with _lock:
        _seen.pop(app, None)
    return db.ex("DELETE FROM suite_apps WHERE app = %s", (app,)) > 0


def invalidate():
    """Tests: forget what was noted in memory."""
    with _lock:
        _seen.clear()
