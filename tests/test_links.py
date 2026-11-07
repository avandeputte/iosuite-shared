import io
import urllib.error

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iosuite_shared import admin, apppasswords, credentials, links, suite, users
from iosuite_shared.fastapi import Auth


def provider_app():
    auth = Auth("iocontacts", register=False)
    app = FastAPI()
    app.middleware("http")(auth.middleware())

    @app.get("/api/contacts")
    def contacts(request: Request):
        return {"who": auth.current_user(request)["name"]}

    @app.post("/api/contacts")
    def add(request: Request):
        return {"ok": True}

    @app.get("/face/1")
    def face(request: Request):
        return {"ok": True}

    @app.get("/dav/x")
    def dav(request: Request):
        return {"ok": True}
    return app


def test_service_account_and_read_only_app_password():
    users.create("alex", "secret123", suite_admin=True)
    assert links.url("iocontacts") is None and not links.available("iocontacts")
    suite.register("iocontacts", name="ioContacts", url="https://contacts.example.com/", version="1")
    assert links.url("iocontacts") == "https://contacts.example.com"
    h = links.headers("iocalendar", "iocontacts")
    svc = users.service_account("iocalendar")
    assert svc["name"] == "iocalendar-app" and svc["service"] == "iocalendar" and not svc["listed"]
    assert users.role_of(users.get(svc["id"]), "iocontacts") == "viewer"
    assert links.headers("iocalendar", "iocontacts") == h  # kept, not made again
    assert [u["name"] for u in users.list_users()] == ["alex"]  # people only
    assert "iocalendar-app" in [u["name"] for u in users.list_users(services=True)]
    assert users.authenticate("iocalendar-app", "anything") is None
    assert credentials.listing() == [] or all(not k["name"].startswith("link_") for k in credentials.listing())
    pws = admin.app_passwords()
    assert [(p["userName"], p["app"], p["scope"]) for p in pws] == [("iocalendar-app", "iocontacts", "read")]
    c = TestClient(provider_app(), base_url="http://contacts.example.com")
    auth_h = {"Authorization": h["Authorization"]}
    assert c.get("/api/contacts", headers=auth_h).json() == {"who": "iocalendar-app"}
    assert c.post("/api/contacts", headers=auth_h).status_code == 403
    assert c.get("/dav/x", headers=auth_h).status_code == 403 and c.get("/face/1", headers=auth_h).status_code == 200
    assert users.any_users()


def test_first_run_is_not_spoiled_by_a_service_account():
    users.service_account("iopix")
    assert not users.any_users()


def test_revoked_password_is_made_again(monkeypatch):
    suite.register("iopix", name="ioPix", url="https://photo.example.com", version="1")
    first = links.password("iocontacts", "iopix")[1]
    seen = []

    def fake(req, timeout=None):
        seen.append(req.headers["Authorization"])
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
        return io.BytesIO(b'{"ok": 1}')
    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert links.json("iocontacts", "iopix", "/api/meta") == {"ok": 1}
    second = links.password("iocontacts", "iopix")[1]
    assert second != first and len(seen) == 2 and seen[0] != seen[1]
    assert apppasswords.authenticate(None, first, "iopix") is None and apppasswords.authenticate(None, second, "iopix")
    with pytest.raises(RuntimeError):
        links.get("iocontacts", "nothere", "/api/x")


def test_cloudflare_service_token_goes_along():
    suite.register("iopix", name="ioPix", url="https://photo.example.com", version="1")
    assert "CF-Access-Client-Id" not in links.headers("iocontacts", "iopix")
    assert links.headers("iocontacts", "iopix", sign_in=False) == {}
    me = users.create("alex", "secret123", suite_admin=True)
    admin.set_key(me, "_suite", "cf_access_client_id", "id.access")
    admin.set_key(me, "_suite", "cf_access_client_secret", "sec")
    h = links.headers("iocontacts", "iopix")
    assert h["CF-Access-Client-Id"] == "id.access" and h["CF-Access-Client-Secret"] == "sec"
    assert set(links.headers("iocontacts", "iopix", sign_in=False)) == {"CF-Access-Client-Id", "CF-Access-Client-Secret"}
    assert {k["name"] for k in admin.keys("_suite")} == {"cf_access_client_id", "cf_access_client_secret", "cf_kv_account_id",
                                                         "cf_kv_namespace_id", "cf_kv_api_token"}
    links.forget("iocontacts")
    assert admin.app_passwords() == []


def test_internal_address_first_then_public(monkeypatch):
    suite.register("iopix", name="ioPix", url="https://photo.example.com", version="1", internal_url="http://192.168.1.5:8600")
    assert links.addresses("iopix") == ["http://192.168.1.5:8600", "https://photo.example.com"]
    assert links.url("iopix") == "https://photo.example.com"
    seen = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        if "192.168" in req.full_url:
            raise urllib.error.URLError("refused")
        return io.BytesIO(b'{"ok": 2}')
    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert links.json("iocontacts", "iopix", "/api/meta") == {"ok": 2}
    assert seen == ["http://192.168.1.5:8600/api/meta", "https://photo.example.com/api/meta"]


def test_same_machine_goes_over_localhost(monkeypatch):
    suite.register("iocontacts", name="ioContacts", url="https://contacts.example.com", version="1", internal_url="http://127.0.0.1:8640")
    assert links.addresses("iocontacts") == ["http://127.0.0.1:8640", "https://contacts.example.com"]
    monkeypatch.setattr(links, "_this_machine", lambda host: host == "192.168.1.9")
    suite.register("iocontacts", name="ioContacts", url="https://contacts.example.com", version="1", internal_url="http://192.168.1.9:8640")
    assert links.addresses("iocontacts") == ["http://127.0.0.1:8640", "http://192.168.1.9:8640", "https://contacts.example.com"]
    monkeypatch.setattr(links, "_this_machine", lambda host: False)
    assert links.addresses("iocontacts") == ["http://192.168.1.9:8640", "https://contacts.example.com"]
    monkeypatch.undo()
    assert not links._this_machine("203.0.113.7") and links._this_machine("127.0.0.1") and not links._this_machine("a.lan")
