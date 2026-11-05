from fastapi import FastAPI
from fastapi.testclient import TestClient

from iosuite_shared import suite, users
from iosuite_shared.fastapi import Auth


def make_app(name, **kw):
    auth = Auth(name, app_label=kw.pop("label", name), **kw)
    app = FastAPI()
    app.middleware("http")(auth.middleware(open_paths=("/",)))
    app.include_router(auth.router())

    @app.get("/api/x", dependencies=[auth.access.public])
    def x():
        return {"ok": True}
    return app, auth


def test_register_keeps_one_row_and_clears_what_the_app_no_longer_says():
    suite.register("iopix", "ioPix", url="https://photo.iothing.net/", version="1.0", color="#3b4a8c", icon="/static/icon.svg")
    r = suite.get("iopix")
    assert r["url"] == "https://photo.iothing.net" and r["configuredUrl"] == "https://photo.iothing.net"
    assert r["icon"] == "https://photo.iothing.net/static/icon.svg" and r["version"] == "1.0"
    suite.register("iopix", "ioPix", version="1.1")
    r = suite.get("iopix")
    assert r["url"] is None and r["version"] == "1.1" and r["color"] is None
    assert [a["app"] for a in suite.apps()] == ["iopix"]
    assert suite.forget("iopix") and suite.apps() == [] and not suite.forget("iopix")


def test_bad_url_is_refused():
    import pytest
    with pytest.raises(ValueError):
        suite.register("iopix", "ioPix", url="photo.iothing.net")


def test_rank_prefers_the_shared_domain_over_https(monkeypatch):
    monkeypatch.setattr("iosuite_shared.config.COOKIE_DOMAIN", "iothing.net")
    assert suite._rank("http://localhost:8600") == -1
    assert suite._rank("http://127.0.0.1:8600") == -1
    assert suite._rank("http://testserver") == -1
    assert suite._rank("http://192.168.1.89:8600") == 0
    assert suite._rank("http://[fd00::1]:8600") == 0
    assert suite._rank("http://nas:8600") == 1
    assert suite._rank("https://photo.iothing.net") == 3
    assert suite._rank("https://photo.iothing.net") > suite._rank("http://nas.lan:8600")


def test_middleware_registers_and_notes_the_best_address(monkeypatch):
    monkeypatch.setattr("iosuite_shared.config.COOKIE_DOMAIN", "iothing.net")
    suite.invalidate()
    app, _ = make_app("iocalendar", label="ioCalendar", version="0.4.0", color="#963522", icon="static/icons/icon.svg")
    TestClient(app, base_url="http://testserver").get("/api/x")
    r = suite.get("iocalendar")
    assert r["name"] == "ioCalendar" and r["version"] == "0.4.0" and r["url"] is None  # a test host is never kept
    TestClient(app, base_url="http://192.168.1.89:8620").get("/api/x")
    assert suite.get("iocalendar")["url"] == "http://192.168.1.89:8620"
    TestClient(app, base_url="http://calendar.lan:8620").get("/api/x")
    assert suite.get("iocalendar")["url"] == "http://calendar.lan:8620"
    c = TestClient(app, base_url="http://10.0.0.5:8620")
    c.get("/api/x", headers={"x-forwarded-proto": "https", "x-forwarded-host": "calendar.iothing.net"})
    assert suite.get("iocalendar")["url"] == "https://calendar.iothing.net"
    assert suite.get("iocalendar")["icon"] == "https://calendar.iothing.net/static/icons/icon.svg"
    TestClient(app, base_url="http://192.168.1.89:8620").get("/api/x")  # a worse address does not replace it
    assert suite.get("iocalendar")["url"] == "https://calendar.iothing.net"


def test_configured_url_wins_and_register_false_stays_out():
    suite.invalidate()
    app, _ = make_app("iopix", label="ioPix", public_url="https://photo.example.com")
    TestClient(app, base_url="http://192.168.1.89:8600").get("/api/x")
    r = suite.get("iopix")
    assert r["url"] == "https://photo.example.com" and r["seenUrl"] == "http://192.168.1.89:8600"
    other, _ = make_app("tool", register=False)
    TestClient(other, base_url="http://192.168.1.89:9000").get("/api/x")
    assert suite.get("tool") is None


def test_bad_public_url_registers_without_it():
    suite.invalidate()
    app, _ = make_app("ionotes", public_url="notes.example.com")
    TestClient(app, base_url="http://192.168.1.89:8610").get("/api/x")
    assert suite.get("ionotes")["url"] == "http://192.168.1.89:8610"


def test_api_lists_for_signed_in_and_suite_admins_forget():
    suite.invalidate()
    app, _ = make_app("iosuite", label="ioSuite")
    users.create("alex", "secret123", "admin", "iosuite", suite_admin=True)
    users.create("bob", "secret123", "viewer", "iosuite")
    suite.register("iopix", "ioPix", url="https://photo.iothing.net")
    c = TestClient(app, base_url="http://home.lan")
    assert c.get("/api/auth/suite").status_code == 401
    c.post("/api/auth/login", json={"name": "bob", "password": "secret123"})
    body = c.get("/api/auth/suite").json()
    assert body["app"] == "iosuite" and {a["app"] for a in body["items"]} == {"iopix", "iosuite"}
    assert c.delete("/api/auth/suite/iopix").status_code == 403
    c.post("/api/auth/login", json={"name": "alex", "password": "secret123"})
    assert c.delete("/api/auth/suite/iopix").status_code == 200
    assert c.delete("/api/auth/suite/iopix").status_code == 404
    assert [a["app"] for a in c.get("/api/auth/suite").json()["items"]] == ["iosuite"]


def test_an_app_with_its_own_middleware_registers_through_resolve():
    suite.invalidate()
    auth = Auth("iohealth", app_label="ioHealth", version="3.0")
    app = FastAPI()

    @app.middleware("http")
    async def mine(request, call_next):
        await auth.resolve(request)
        return await call_next(request)

    @app.get("/x")
    def x():
        return {"ok": True}
    TestClient(app, base_url="http://192.168.1.89:47800").get("/x")
    r = suite.get("iohealth")
    assert r["version"] == "3.0" and r["url"] == "http://192.168.1.89:47800"
