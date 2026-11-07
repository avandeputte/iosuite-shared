"""Edge check of app passwords: a Cloudflare Worker in front of the DAV paths turns a wrong app password away before
it reaches the server (cloudflare/edge-auth/). The Worker reads a Workers KV namespace this module keeps up to date:

    key   <app>:<prefix>          the app and the 8 hex of the password (iocalendar:3fa91c02)
    value {"h": "<sha-256 hex>"}  of the whole password

A SHA-256 is enough here and fast for the Worker: an app password is 144 random bits, nothing anyone can guess from
its hash. Account passwords are never sent. The server still checks every password itself (scrypt), so a stale key
only lets a request reach it, never in.

Off unless all three are set (config.env):

    CF_KV_ACCOUNT_ID=…      the Cloudflare account id
    CF_KV_NAMESPACE_ID=…    the KV namespace the Worker is bound to
    CF_KV_API_TOKEN=…       an API token with Account › Workers KV Storage › Edit

A password is published when it is made, and again the first time it signs in after each start of the app (the only
moment the server holds it in clear again): passwords made before the edge check was on get there with their next
use, while the Worker is in its `monitor` mode. Deleting one, or the account, removes its key. Calls run in the
background and never hold up a request; a failure is logged.
"""
import hashlib
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from iosuite_shared import config

log = logging.getLogger("iosuite_shared.edge")
API = "https://api.cloudflare.com/client/v4"
_published: set = set()  # keys put since the app started: put again after a restart, on first use


def settings():
    return (config.env("CF_KV_ACCOUNT_ID"), config.env("CF_KV_NAMESPACE_ID"), config.env("CF_KV_API_TOKEN"))


def enabled() -> bool:
    return all(settings())


def key(app: str, prefix: str) -> str:
    return f"{app}:{prefix}"


def digest(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _request(method: str, path: str, body: bytes | None = None, ctype: str = "application/json"):
    """One Cloudflare API call; (status, parsed JSON or None). The tests replace it."""
    account, ns, token = settings()
    url = f"{API}/accounts/{account}/storage/kv/namespaces/{ns}{path}"
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def _put(k: str, password: str):
    body = json.dumps({"h": digest(password)}).encode()
    status, _ = _request("PUT", "/values/" + urllib.parse.quote(k, safe=""), body)
    if status != 200:
        _published.discard(k)
        raise RuntimeError(f"Cloudflare KV put {k}: HTTP {status}")


def _delete(k: str):
    status, _ = _request("DELETE", "/values/" + urllib.parse.quote(k, safe=""))
    if status not in (200, 404):
        raise RuntimeError(f"Cloudflare KV delete {k}: HTTP {status}")


def _background(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception as e:  # never up into the request
            log.warning("edge check: %s", e)
    t = threading.Thread(target=run, daemon=True, name="edge-kv")
    t.start()
    return t


def publish(app: str, prefix: str, password: str, wait: bool = False):
    """Put the password's hash at the edge (a no-op when the edge check is off, or done since the start)."""
    if not enabled():
        return None
    k = key(app, prefix)
    if k in _published:
        return None
    _published.add(k)
    t = _background(_put, k, password)
    if wait:
        t.join()
    return t


def unpublish(pairs, wait: bool = False):
    """Remove the keys of these (app, prefix) pairs."""
    if not enabled():
        return None
    ks = [key(a, p) for a, p in pairs]
    if not ks:
        return None
    for k in ks:
        _published.discard(k)
    t = _background(lambda: [_delete(k) for k in ks])
    if wait:
        t.join()
    return t


def list_keys() -> list:
    """Every key in the namespace (pages of 1000)."""
    out, cursor = [], ""
    while True:
        status, j = _request("GET", "/keys?limit=1000" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else ""))
        if status != 200 or not j or not j.get("success", True):
            raise RuntimeError(f"Cloudflare KV list: HTTP {status}")
        out += [r["name"] for r in j.get("result", [])]
        cursor = (j.get("result_info") or {}).get("cursor") or ""
        if not cursor:
            return out


def sync(apply: bool = False) -> dict:
    """Compare the namespace with the database: keys without a password are removed (with apply), passwords without
    a key are counted (they get there with their next use: their clear text is not kept anywhere)."""
    from iosuite_shared import db
    rows = {key(r["app"], r["prefix"]) for r in db.q("SELECT app, prefix FROM ios_app_passwords")}
    have = set(list_keys())
    stale = sorted(have - rows)
    if apply:
        for k in stale:
            _delete(k)
    return {"keys": len(have), "passwords": len(rows), "stale": stale, "removed": len(stale) if apply else 0,
            "missing": sorted(rows - have)}
