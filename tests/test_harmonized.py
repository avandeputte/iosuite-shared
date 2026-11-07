"""What every app gets the same way since 0.11.0: the public status, install() (400 for bad input, the headers, the body
cap), the MCP gate and helpers, the log set-up, the standard settings and the test helpers."""
import json
import logging

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from iosuite_shared import config, testing, users
from iosuite_shared import mcp as shared_mcp
from iosuite_shared.fastapi import Auth
from iosuite_shared.updates import Updater, setup_log


class FakeUpdater:
    version = "1.2.3"

    def __init__(self):
        self.found = None
        self.auto = True

    def local_commit(self):
        return "abcdef0123"

    def auto_check_enabled(self):
        return self.auto

    def available(self):
        return dict(self.found) if self.found else None

    def can_run(self):
        return False

    def in_docker(self):
        return False


def make_app(mcp_on=lambda: True, seen=None, **install):
    auth = Auth("iotest", app_label="ioTest", version="1.2.3", register=False)
    app = FastAPI()
    updater = FakeUpdater()
    gate = auth.mcp_gate(mcp_on, on_user=seen.append if seen is not None else None)
    app.middleware("http")(auth.middleware(open_paths=("/",), open_prefixes=("/mcp",), before=gate))
    app.include_router(auth.router())
    app.include_router(auth.status_router(updater, extra=lambda request: {"mine": users.is_admin(request.state.user)}))
    auth.install(app, **install)

    @app.get("/")
    def index():
        return {"page": True}

    @app.post("/api/echo", dependencies=[auth.access.public])
    async def echo(request: Request):
        return {"n": len(await request.body())}

    @app.get("/api/boom", dependencies=[auth.access.public])
    def boom():
        raise ValueError("a word where a number belongs")

    @app.get("/api/gone", dependencies=[auth.access.public])
    def gone():
        raise HTTPException(404, "No such thing")

    @app.post("/mcp", dependencies=[auth.access.public])
    def mcp():
        return {"mcp": True}
    return app, auth, updater


def test_status_is_public_and_the_same_shape_everywhere():
    app, auth, updater = make_app()
    c = TestClient(app, base_url="http://test.example.com")
    s = c.get("/api/status").json()
    # no accounts yet: the install is open, so a visitor counts as signed in and admin (the suite's rule), and sees the commit
    assert s == {"ok": True, "app": "iotest", "name": "ioTest", "version": "1.2.3", "usersExist": False, "requireLogin": False, "signedIn": True,
                 "isAdmin": True, "viaProxy": False, "user": None, "mine": True, "commit": "abcdef0", "update": None}
    c.post("/api/auth/setup", json={"name": "alex", "password": "secret123", "email": "a@b.c"})
    updater.found = {"version": "1.3.0", "remote": "fedcba9"}
    s = c.get("/api/status").json()
    assert s["user"] == {"id": 1, "name": "alex", "displayName": "alex", "email": "a@b.c", "role": "admin", "suiteAdmin": True, "passwordless": False}
    assert s["signedIn"] and s["isAdmin"] and s["mine"] and s["commit"] == "abcdef0" and s["usersExist"]
    assert s["update"] == {"version": "1.3.0", "remote": "fedcba9", "canRun": False, "docker": False}
    updater.auto = False
    assert c.get("/api/status").json()["update"] is None
    # sign-in required: the status still answers a visitor, with nothing about the install
    from iosuite_shared import db
    db.set_setting("require_login", "1", "iotest")
    v = TestClient(app, base_url="http://test.example.com")
    s = v.get("/api/status").json()
    assert s["requireLogin"] and s["usersExist"] and s["user"] is None and "commit" not in s and "update" not in s
    assert v.get("/api/echo").status_code == 401


def test_install_answers_bad_input_with_400_and_sets_the_headers():
    app, _, _ = make_app()
    c = TestClient(app)
    r = c.get("/api/boom")
    assert r.status_code == 400 and r.json() == {"detail": "Bad request: a word where a number belongs"}
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "same-origin"
    assert c.get("/").headers["cache-control"] == "no-cache"
    r = c.post("/api/echo", content=b"{", headers={"content-type": "application/json"})
    assert r.status_code == 200  # the route read the raw body itself; malformed JSON is a 400 only where a route parses it


def test_install_error_key_for_an_app_whose_phone_reads_error():
    app, _, _ = make_app(error_key="error")
    c = TestClient(app)
    assert c.get("/api/gone").json() == {"detail": "No such thing", "error": "No such thing"}
    assert c.get("/api/boom").json()["error"].startswith("Bad request")


def test_install_caps_the_body_by_length_and_as_it_arrives():
    app, _, _ = make_app(limit_for=lambda path, ctype: 64 if ctype == "application/json" else 1024)
    c = TestClient(app)
    assert c.post("/api/echo", content=b"x" * 64, headers={"content-type": "application/json"}).json() == {"n": 64}
    r = c.post("/api/echo", content=b"x" * 65, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"] == "That request is too large"
    assert c.post("/api/echo", content=b"x" * 1000, headers={"content-type": "application/octet-stream"}).json() == {"n": 1000}
    assert c.post("/api/echo", content=b"x", headers={"content-length": "abc"}).status_code == 400

    def chunks():
        for _ in range(3):
            yield b"y" * 40
    r = c.post("/api/echo", content=chunks(), headers={"content-type": "application/json"})  # chunked: no Content-Length to check first
    assert r.status_code == 413


def test_mcp_gate_switch_sign_in_and_the_context():
    on = {"v": False}
    seen = []
    app, _, _ = make_app(mcp_on=lambda: on["v"], seen=seen)
    c = TestClient(app, base_url="http://test.example.com")
    r = c.post("/mcp", json={})
    assert r.status_code == 404 and "switched off (Settings › Server)" in r.json()["detail"]
    on["v"] = True
    assert c.post("/mcp", json={}).status_code == 401  # an assistant always signs in, even before the first account exists
    c.post("/api/auth/setup", json={"name": "alex", "password": "secret123"})
    assert c.post("/mcp", json={}).status_code == 200  # the session cookie is a sign-in too
    v = TestClient(app, base_url="http://test.example.com")
    r = v.post("/mcp", json={})
    assert r.status_code == 401 and "app password" in r.json()["detail"] and "Bearer" in r.headers["www-authenticate"]
    pw = c.post("/api/auth/app-passwords", json={"label": "Claude"}).json()["password"]
    assert v.post("/mcp", json={}, headers={"authorization": f"Bearer {pw}"}).status_code == 200
    assert seen[-1]["name"] == "alex"
    assert v.post("/api/echo", json={}).status_code == 200  # the gate touches /mcp only


def test_mcp_helpers():
    from mcp.server.mcpserver.exceptions import ToolError
    assert shared_mcp.clamp("7", 1, 5) == 5 and shared_mcp.clamp(None, 1, 5) == 1 and shared_mcp.clamp(3, 1, 5) == 3
    store = {}
    assert shared_mcp.enabled(lambda k, d: store.get(k, d)) is False
    shared_mcp.set_enabled(lambda k, v: store.__setitem__(k, v), True)
    assert store == {"mcp_enabled": "1"} and shared_mcp.enabled(lambda k, d: store.get(k, d))
    assert shared_mcp.enabled(lambda k, d: 1 / 0, forced=True)
    assert shared_mcp.enabled(lambda k, d: 1 / 0) is False
    with pytest.raises(ValueError):
        shared_mcp.set_enabled(lambda k, v: None, False, forced=True)
    with pytest.raises(ToolError):
        shared_mcp.who()
    users.create("alex", "secret123", "admin", "iotest", suite_admin=True)  # once accounts exist, roles count
    shared_mcp.current_user.set({"id": 2, "name": "bob", "role": "viewer"})
    assert shared_mcp.who()["name"] == "bob"
    with pytest.raises(ToolError):
        shared_mcp.need_admin()
    with pytest.raises(ToolError):
        shared_mcp.need_writer()
    shared_mcp.current_user.set({"id": 1, "name": "alex", "role": "admin"})
    assert shared_mcp.need_admin()["name"] == "alex" and shared_mcp.need_writer()
    shared_mcp.current_user.set(None)


def test_mcp_server_mounts_ahead_of_the_routes():
    server = shared_mcp.make_server("iotest", "ioTest", "1.2.3", instructions="Testing.")

    @server.tool(annotations=shared_mcp.READ_ONLY)
    def hello() -> str:
        return "hi"
    app = FastAPI()

    @app.get("/mcp")
    def shadow():
        return {"shadowed": True}
    shared_mcp.mount(app, server)
    assert app.router.routes[0].path == "/mcp" and len([r for r in app.router.routes if r.path == "/mcp"]) == 2


def test_setup_log_is_idempotent_and_catches_uvicorn(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        f = tmp_path / "app.log"
        assert setup_log(f) and setup_log(f)
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1 and added[0] in logging.getLogger("uvicorn.access").handlers
        logging.getLogger("x").warning("hello there")
        logging.getLogger("uvicorn.access").info("GET / 200")
        added[0].flush()
        text = f.read_text()
        assert " WARNING x: hello there" in text and "uvicorn.access: GET / 200" in text
        assert setup_log(None) is False
        assert setup_log(tmp_path / "nope" / "deeper" / "x.log") is False
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            logging.getLogger("uvicorn.access").removeHandler(h)
            logging.getLogger("uvicorn.error").removeHandler(h)
            h.close()


def test_standard_settings(tmp_path, monkeypatch):
    for k in ("DATA_DIR", "LOG_FILE", "HOST", "PORT", "PUBLIC_URL", "FORWARDED_ALLOW_IPS", "MCP"):
        monkeypatch.delenv(k, raising=False)
    s = config.standard("iocalendar", tmp_path)
    assert s["APP_DIR"] == tmp_path.resolve() and s["DATA_DIR"] == tmp_path.resolve() / "data" and s["DATA_DIR"].is_dir()
    assert s["STATIC_DIR"] == tmp_path.resolve() / "static" and s["LOG_FILE"] == s["DATA_DIR"] / "iocalendar.log"
    assert (s["HOST"], s["PORT"], s["PUBLIC_URL"], s["BASE_URL"], s["FORWARDED_ALLOW_IPS"], s["MCP_FORCED"]) == ("0.0.0.0", 8620, "", "http://localhost:8620", "*", False)
    monkeypatch.setenv("LOG_FILE", "")
    monkeypatch.setenv("PUBLIC_URL", "https://cal.example.com/")
    monkeypatch.setenv("MCP", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "elsewhere"))
    s = config.standard("iohealth", tmp_path)
    assert s["LOG_FILE"] is None and s["BASE_URL"] == s["PUBLIC_URL"] == "https://cal.example.com" and s["MCP_FORCED"] and s["PORT"] == 47800
    assert s["DATA_DIR"] == (tmp_path / "elsewhere").resolve()
    for k in ("DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_USER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_NAME", "cal_test")
    assert config.own_db("calendar") == {"host": "127.0.0.1", "port": 3306, "database": "cal_test", "user": "calendar", "password": ""}


def test_check_access_declared_names_the_route_without_a_marker():
    app, auth, _ = make_app()

    @app.get("/api/open")
    def open_route():
        return {}
    with pytest.raises(AssertionError) as e:
        testing.check_access_declared(app, auth.access.declared)
    assert "/api/open" in str(e.value) and "/api/echo" not in str(e.value)


def test_check_conventions(tmp_path):
    class Cfg:
        PORT = 8620
    (tmp_path / "pyproject.toml").write_text('version = "1.0"\n')
    (tmp_path / ".gitignore").write_text("config.env\n.venv\n")
    for f in ("Dockerfile", "docker-compose.yml", "config.env.example", "run.sh"):
        (tmp_path / f).write_text("8620\n")
    (tmp_path / "README.md").write_text("Settings live in config.env; the example is config.env.example.\n")
    testing.check_conventions(tmp_path, "iocalendar", "1.0", Cfg)
    (tmp_path / "setup.sh").write_text("cp .env iocalendar.env\n")
    with pytest.raises(AssertionError):
        testing.check_conventions(tmp_path, "iocalendar", "1.0", Cfg)


def test_clean_shared_tables():
    users.create("alex", "secret123", "admin", "iotest", suite_admin=True)
    users.create("bob", "secret123", "viewer", "iotest")
    auth = Auth("iotest", register=False)
    auth._registered = True
    testing.clean_shared_tables(auth)
    assert not users.any_users() and not auth._registered
    assert users.create("carol", "secret123", "admin", "iotest")["id"] == 1


def test_updater_is_what_status_router_expects(tmp_path):
    u = Updater("iotest", "1.0", tmp_path, settings=(lambda k, d="": d, lambda k, v: None))
    for name in ("local_commit", "auto_check_enabled", "available", "can_run", "in_docker"):
        assert callable(getattr(u, name))
    assert json.dumps(u.status())


def test_check_access_declared_sees_through_included_routers():
    """FastAPI 0.162 keeps an included router as one entry of app.routes: the check must still reach its routes, and
    must never pass because it found nothing to check."""
    from fastapi import APIRouter, FastAPI
    from iosuite_shared.fastapi import Auth
    auth = Auth("t", register=False)
    r = APIRouter()

    @r.get("/open")
    def open_route():
        return {}

    @r.get("/closed", dependencies=[auth.access.admin])
    def closed_route():
        return {}
    app = FastAPI()
    app.include_router(r, prefix="/api")
    assert {p for _m, p, _r in testing.api_routes(app)} >= {"/api/open", "/api/closed"}
    with pytest.raises(AssertionError, match="/api/open"):
        testing.check_access_declared(app, auth.access.declared)
    with pytest.raises(AssertionError, match="nothing was checked"):
        testing.check_access_declared(app, auth.access.declared, prefix="/nothing/")
    testing.check_access_declared(app, auth.access.declared, ignore=("/api/open",))
