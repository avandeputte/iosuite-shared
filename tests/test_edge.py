"""The edge check's KV sync (iosuite_shared.edge), with the Cloudflare API replaced by a dict."""
import hashlib

import pytest

from iosuite_shared import apppasswords, edge, users


@pytest.fixture
def kv(monkeypatch):
    store = {}

    def request(method, path, body=None, ctype=None):
        if path.startswith("/values/"):
            k = path[len("/values/"):].replace("%3A", ":")
            if method == "PUT":
                store[k] = body
                return 200, {"success": True}
            if method == "DELETE":
                return (200, {"success": True}) if store.pop(k, None) is not None else (404, None)
        if path.startswith("/keys"):
            return 200, {"success": True, "result": [{"name": k} for k in sorted(store)], "result_info": {"cursor": ""}}
        raise AssertionError(path)

    monkeypatch.setattr(edge, "_request", request)
    for k, v in (("CF_KV_ACCOUNT_ID", "acc"), ("CF_KV_NAMESPACE_ID", "ns"), ("CF_KV_API_TOKEN", "tok")):
        monkeypatch.setenv(k, v)
    edge._published.clear()
    threads = []
    real = edge._background

    def background(fn, *args):  # run the call before the test looks
        t = real(fn, *args)
        t.join()
        threads.append(t)
        return t
    monkeypatch.setattr(edge, "_background", background)
    return store


def _alex():
    return users.create("alex", "secret-pass-1", "admin", "iocalendar", email="alex@example.com", suite_admin=True)


def test_off_without_settings(monkeypatch):
    for k in ("CF_KV_ACCOUNT_ID", "CF_KV_NAMESPACE_ID", "CF_KV_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(edge, "_request", lambda *a, **k: pytest.fail("no call when off"))
    u = _alex()
    pw = apppasswords.create(u["id"], "iocalendar", "iPhone")
    assert apppasswords.authenticate("alex", pw, "iocalendar")


def test_create_publishes_the_hash_and_delete_removes_it(kv):
    u = _alex()
    pw = apppasswords.create(u["id"], "iocalendar", "iPhone")
    prefix = pw.split("-")[1]
    assert list(kv) == [f"iocalendar:{prefix}"]
    assert kv[f"iocalendar:{prefix}"] == ('{"h": "%s"}' % hashlib.sha256(pw.encode()).hexdigest()).encode()
    pid = apppasswords.list_for(u["id"], "iocalendar")[0]["id"]
    apppasswords.delete(u["id"], pid)
    assert kv == {}


def test_first_use_after_a_start_publishes_again(kv):
    u = _alex()
    pw = apppasswords.create(u["id"], "iocontacts", "Mac")
    kv.clear()                      # made before the edge check was on
    edge._published.clear()         # the app restarted
    assert apppasswords.authenticate("alex", pw, "iocontacts")
    assert list(kv) == [f"iocontacts:{pw.split('-')[1]}"]
    assert apppasswords.authenticate("alex", "secret-pass-1", "iocontacts")  # an account password is never sent
    assert len(kv) == 1


def test_deleting_the_account_removes_its_keys(kv):
    _alex()
    bob = users.create("bob", "secret-pass-2", "editor", "iocalendar")
    apppasswords.create(bob["id"], "iocalendar", "iPad")
    apppasswords.create(bob["id"], "iocontacts", "iPad")
    assert len(kv) == 2
    users.delete(bob["id"])
    assert kv == {}


def test_sync_finds_stale_and_missing(kv):
    u = _alex()
    pw = apppasswords.create(u["id"], "iocalendar", "iPhone")
    kv["iocalendar:deadbeef"] = b"{}"
    kv.pop(f"iocalendar:{pw.split('-')[1]}")
    r = edge.sync()
    assert r["stale"] == ["iocalendar:deadbeef"] and r["missing"] == [f"iocalendar:{pw.split('-')[1]}"] and "iocalendar:deadbeef" in kv
    r = edge.sync(apply=True)
    assert r["removed"] == 1 and kv == {}


def test_a_failed_call_never_reaches_the_request(kv, monkeypatch):
    monkeypatch.setattr(edge, "_request", lambda *a, **k: (500, None))
    u = _alex()
    pw = apppasswords.create(u["id"], "iocalendar", "iPhone")  # no exception
    assert apppasswords.authenticate("alex", pw, "iocalendar")
    assert f"iocalendar:{pw.split('-')[1]}" not in edge._published  # tried again on a later use


def test_settings_from_the_shared_database(kv, monkeypatch):
    for k in ("CF_KV_ACCOUNT_ID", "CF_KV_NAMESPACE_ID", "CF_KV_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert not edge.enabled()
    edge.store("acc", "ns", "tok", by="test")
    assert edge.settings() == ("acc", "ns", "tok") and edge.enabled()
    from iosuite_shared import credentials
    keys = {r["name"]: r for r in credentials.listing("")}
    assert keys["cf_kv_api_token"]["set"] and keys["cf_kv_api_token"]["env"] == "CF_KV_API_TOKEN"
    monkeypatch.setenv("CF_KV_NAMESPACE_ID", "from-env")  # config.env wins
    assert edge.settings()[1] == "from-env"
    u = _alex()
    pw = apppasswords.create(u["id"], "iocalendar", "iPhone")
    assert f"iocalendar:{pw.split('-')[1]}" in kv
