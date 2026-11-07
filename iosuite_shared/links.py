"""One app of the suite reading from another, with nothing to set up: ioCalendar's and ioPix's birthdays from
ioContacts, ioContacts' faces from ioPix.

    from iosuite_shared import links
    if links.available("iocontacts"):                       # it is in the suite's list of apps, with an address
        r = links.get("iocalendar", "iocontacts", "/api/contacts?q=ann")   # a urllib response; links.json(...) for JSON
    links.url("iocontacts")                                  # its address, for links people click

The other app is found in the suite's list (ios_apps): its public address (PUBLIC_URL, else the one it was last
reached at), never the internal one. The reader signs in as its own service account (users.service_account:
<app>-app, on no sign-in screen) with an app password of the other app that may only read (scope 'read'),
and a viewer role there. Both are made the first time they are needed; the password is kept in the keys and tokens
store (iosuite_shared.credentials) under the reader, and made again when the other app stops taking it (revoked from
Settings › Suite, say). When the other app sits behind Cloudflare Access, the suite-wide Cloudflare Access service
token (keys `cf_access_client_id` and `cf_access_client_secret` of the whole suite, Settings › Suite › Keys and tokens)
goes with every request.
"""
import base64
import json as _json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from iosuite_shared import apppasswords, credentials, db, suite, users

log = logging.getLogger("iosuite_shared.links")
_lock = threading.Lock()
SUITE_KEYS = [
    {"name": "cf_access_client_id", "label": "Cloudflare Access service token: client id",
     "hint": "for the apps reading from each other when they sit behind Cloudflare Access"},
    {"name": "cf_access_client_secret", "label": "Cloudflare Access service token: client secret",
     "hint": "goes with the client id"},
]
_declared = False


def _key(provider: str) -> str:
    return f"link_{provider}"


def url(provider: str) -> str | None:
    """The other app's public address, or None while it is not in the suite's list."""
    try:
        u = suite.url_of(provider)
    except Exception:  # noqa: BLE001  the shared database is away
        return None
    return u.rstrip("/") if u else None


def available(provider: str) -> bool:
    return bool(url(provider))


def _declare_suite_keys():
    global _declared
    if not _declared:
        credentials.declare("", SUITE_KEYS)
        _declared = True


def account(reader: str) -> dict:
    return users.service_account(reader)


def password(reader: str, provider: str, fresh: bool = False) -> tuple[str, str]:
    """(user name, app password) the reader signs in to the provider with; made on first use (or with fresh, again:
    the old one is revoked)."""
    with _lock:
        svc = account(reader)
        pw = "" if fresh else credentials.get(reader, _key(provider))
        if pw:
            return svc["name"], pw
        for r in db.q("SELECT id FROM ios_app_passwords WHERE user_id = %s AND app = %s", (svc["id"], provider)):
            apppasswords.delete(svc["id"], r["id"])
        if not db.one("SELECT 1 AS x FROM ios_user_roles WHERE user_id = %s AND app = %s", (svc["id"], provider)):
            users.set_role(svc["id"], provider, "viewer")  # a role a suite admin gave it later (none, say) is kept
        pw = apppasswords.create(svc["id"], provider, f"{reader} reads {provider} (automatic)", apppasswords.READ)
        credentials.put(reader, _key(provider), pw, by="automatic")
        log.info("%s: made an app password to read %s as %s", reader, provider, svc["name"])
        return svc["name"], pw


def headers(reader: str, provider: str, fresh: bool = False, sign_in: bool = True) -> dict:
    """The headers that sign the reader in at the provider (HTTP Basic, and Cloudflare Access when the suite has a
    service token). sign_in False: Cloudflare Access only (an address that carries its own token, such as a feed)."""
    h = {}
    if sign_in:
        name, pw = password(reader, provider, fresh)
        h["Authorization"] = "Basic " + base64.b64encode(f"{name}:{pw}".encode()).decode()
    _declare_suite_keys()
    cid, sec = credentials.get("", "cf_access_client_id"), credentials.get("", "cf_access_client_secret")
    if cid and sec:
        h.update({"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": sec})
    return h


def get(reader: str, provider: str, path: str, timeout: float = 15, accept: str = "application/json", user_agent: str = None,
        sign_in: bool = True):
    """GET path at the provider as the reader (a urllib response). RuntimeError when the provider is not in the suite;
    urllib's HTTPError and URLError otherwise. A 401 makes the app password again once (it was revoked). For an
    address that carries its own token (a feed), sign_in=False sends no app password."""
    base = url(provider)
    if not base:
        raise RuntimeError(f"{provider} is not in the suite's list of apps")
    target = base + path
    if urllib.parse.urlsplit(target).scheme not in ("http", "https"):
        raise RuntimeError(f"{provider}'s address is not http(s)")
    for attempt in (0, 1):
        h = {"Accept": accept, "User-Agent": user_agent or reader, **headers(reader, provider, fresh=attempt == 1, sign_in=sign_in)}
        try:
            return urllib.request.urlopen(urllib.request.Request(target, headers=h), timeout=timeout)  # noqa: S310  http(s), checked above
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt or not sign_in:
                raise
            log.info("%s: %s did not take its app password; making a new one", reader, provider)


def json(reader: str, provider: str, path: str, timeout: float = 15):
    with get(reader, provider, path, timeout) as r:
        return _json.loads(r.read())


def forget(reader: str, provider: str = None) -> None:
    """Revoke the reader's app passwords (at one provider, or everywhere) and drop the stored copies."""
    svc = db.one("SELECT id FROM ios_users WHERE service = %s", (reader,))
    if not svc:
        return
    for r in db.q("SELECT id, app FROM ios_app_passwords WHERE user_id = %s" + (" AND app = %s" if provider else ""),
                  (svc["id"], provider) if provider else (svc["id"],)):
        apppasswords.delete(svc["id"], r["id"])
        credentials.put(reader, _key(r["app"]), "")
