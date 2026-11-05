from fastapi import FastAPI
from fastapi.testclient import TestClient

from iosuite_shared import users
from iosuite_shared.fastapi import Auth


def make_app(name="iopix", read_only=False):
    auth = Auth(name, read_only=lambda: read_only)
    app = FastAPI()
    app.middleware("http")(auth.middleware(open_paths=("/", "/api/status"), open_prefixes=("/api/auth/",)))
    app.include_router(auth.router())
    app.include_router(auth.users_router())

    @app.get("/api/status", dependencies=[auth.access.public])
    def status():
        return {"ok": True}

    @app.get("/api/private", dependencies=[auth.access.signed_in])
    def private():
        return {"ok": True}

    @app.post("/api/edit", dependencies=[auth.access.editor_write])
    def edit():
        return {"ok": True}

    @app.get("/api/admin", dependencies=[auth.access.admin])
    def admin():
        return {"ok": True}
    return app, auth


def test_open_until_first_user_then_setup_and_login():
    app, _ = make_app()
    c = TestClient(app, base_url="http://photo.iothing.net")
    assert c.get("/api/admin").status_code == 200  # no accounts yet: open
    r = c.post("/api/auth/setup", json={"name": "alex", "password": "secret123"})
    assert r.status_code == 200 and r.json()["user"]["suiteAdmin"]
    assert "iothing_session" in r.cookies
    assert c.get("/api/auth/me").json()["isAdmin"]
    assert c.post("/api/auth/setup", json={"name": "x", "password": "secret123"}).status_code == 409
    c2 = TestClient(app, base_url="http://photo.iothing.net")
    assert c2.get("/api/admin").status_code == 401
    assert c2.post("/api/auth/login", json={"name": "alex", "password": "nope"}).status_code == 401
    assert c2.post("/api/auth/login", json={"name": "alex", "password": "secret123"}).status_code == 200
    assert c2.get("/api/admin").status_code == 200
    r = c2.post("/api/auth/logout")
    assert r.status_code == 200
    assert c2.get("/api/admin").status_code == 401


def test_session_shared_between_apps_with_their_own_roles():
    pix, _ = make_app("iopix")
    notes, _ = make_app("ionotes")
    users.create("alex", "secret123", "admin", "iopix", suite_admin=True)
    users.create("bob", "secret123", "editor", "iopix")
    users.set_role(users.find("bob")["id"], "ionotes", "viewer")
    c = TestClient(pix, base_url="http://photo.iothing.net")
    r = c.post("/api/auth/login", json={"name": "bob", "password": "secret123"})
    cookie = r.cookies["iothing_session"]
    assert c.post("/api/edit").status_code == 200
    n = TestClient(notes, base_url="http://notes.iothing.net", cookies={"iothing_session": cookie})
    me = n.get("/api/auth/me").json()
    assert me["user"]["name"] == "bob" and me["user"]["role"] == "viewer" and me["signedIn"]
    assert n.post("/api/edit").status_code == 403
    users.set_role(users.find("bob")["id"], "ionotes", "none")
    from iosuite_shared import sessions
    sessions.invalidate()
    assert n.get("/api/private").status_code == 401  # no access there
    assert n.post("/api/auth/login", json={"name": "bob", "password": "secret123"}).status_code == 403


def test_require_login_and_cross_site():
    app, auth = make_app()
    users.create("alex", "secret123", "admin", "iopix", suite_admin=True)
    c = TestClient(app, base_url="http://photo.iothing.net")
    assert c.get("/api/status").status_code == 200
    users.db.set_setting("require_login", "1", "iopix")
    assert c.get("/api/status").status_code == 200  # open path
    assert c.get("/api/private").status_code == 401
    c.post("/api/auth/login", json={"name": "alex", "password": "secret123"})
    assert c.get("/api/private").status_code == 200
    assert c.post("/api/edit", headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/edit", headers={"origin": "http://photo.iothing.net"}).status_code == 200


def test_users_admin_api_and_read_only():
    app, _ = make_app(read_only=True)
    users.create("alex", "secret123", "admin", "iopix", suite_admin=True)
    c = TestClient(app, base_url="http://photo.iothing.net")
    c.post("/api/auth/login", json={"name": "alex", "password": "secret123"})
    assert c.post("/api/edit").status_code == 403  # read-only
    r = c.post("/api/users", json={"name": "bob", "password": "secret123", "role": "editor"})
    assert r.status_code == 200 and r.json()["role"] == "editor"
    uid = r.json()["id"]
    assert c.post(f"/api/users/{uid}", json={"role": "viewer", "roles": {"ionotes": "admin"}}).status_code == 200
    lst = c.get("/api/users").json()
    bob = next(u for u in lst["users"] if u["name"] == "bob")
    assert bob["role"] == "viewer" and bob["roles"] == {"iopix": "viewer", "ionotes": "admin"}
    assert c.post("/api/auth/settings", json={"theme": "dark"}).json() == {"theme": "dark"}
    assert c.post("/api/auth/password", json={"current": "wrong", "password": "newpass123"}).status_code == 403
    assert c.post("/api/auth/password", json={"current": "secret123", "password": "newpass123"}).status_code == 200
    assert c.get("/api/private").status_code == 200  # this session went on
    assert c.post(f"/api/users/{uid}", json={"delete": True}).status_code == 200
    assert users.get(uid) is None


def test_app_password_basic_and_bearer():
    app, _ = make_app("iocontacts")
    u = users.create("alex", "secret123", "editor", "iocontacts")
    from iosuite_shared import apppasswords
    pw = apppasswords.create(u["id"], "iocontacts", "Mac")
    c = TestClient(app, base_url="http://contacts.iothing.net")
    assert c.get("/api/private").status_code == 401
    assert c.get("/api/private", headers={"authorization": "Bearer " + pw}).status_code == 200
    import base64
    basic = base64.b64encode(f"alex:{pw}".encode()).decode()
    assert c.get("/api/private", headers={"authorization": "Basic " + basic}).status_code == 200
    assert c.get("/api/private", headers={"authorization": "Bearer nope"}).status_code == 401


def test_unreachable_auth_database_answers_503_not_open(monkeypatch):
    from iosuite_shared import db
    app, _ = make_app()
    c = TestClient(app, base_url="http://photo.iothing.net")
    db._ready = False
    db._retry_at = 0.0
    monkeypatch.setattr(db, "params", lambda: {"host": "203.0.113.1", "port": 1, "database": "x", "user": "x", "password": "x"})
    assert c.get("/api/status").status_code == 200  # the open path still answers
    r = c.get("/api/admin")
    assert r.status_code == 503 and "Shared ioSuite database" in r.json()["detail"]
    assert c.post("/api/auth/login", json={"name": "a", "password": "b"}).status_code == 503


def test_sessions_record_where_they_began_and_were_seen():
    from iosuite_shared import sessions
    app, _ = make_app()
    users.create("alex", "secret123", "admin", "iopix", suite_admin=True)
    web = TestClient(app, base_url="http://photo.iothing.net", headers={"user-agent": "Mozilla/5.0 (Macintosh) Safari/605"})
    web.post("/api/auth/login", json={"name": "alex", "password": "secret123"})
    phone = TestClient(app, base_url="http://photo.iothing.net", headers={"user-agent": "iopix/312 CFNetwork/1498 Darwin/25.0", "cf-ray": "abc", "cf-connecting-ip": "203.0.113.9"})
    phone.post("/api/auth/login", json={"name": "alex", "password": "secret123"})
    lst = web.get("/api/auth/sessions").json()["sessions"]
    assert len(lst) == 2
    by = {s["client"]: s for s in lst}
    assert by["web"]["current"] and not by["web"]["cloudflare"] and by["web"]["ip"] == "testclient"
    assert not by["iopix"]["current"] and by["iopix"]["cloudflare"] and by["iopix"]["userAgent"].startswith("iopix/312") and by["iopix"]["lastSeen"]
    assert web.delete(f"/api/auth/sessions/{by['iopix']['id']}").status_code == 200
    assert phone.get("/api/auth/me").json()["user"] is None  # ended
    assert len(web.get("/api/users/1/sessions").json()["sessions"]) == 1  # admins see an account's sessions
    assert web.delete("/api/users/1/sessions").status_code == 200
    sessions.invalidate()
    assert web.get("/api/auth/me").json()["user"] is None
    assert sessions.classify("curl/8.0") == "curl" and sessions.classify("") == "other"
