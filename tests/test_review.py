"""The behaviours the 2026-11 code review asked for: the throttles on every password path, the pool, the forwarded
headers, the trusted-proxy rule, the Cloudflare token check, session renewal and the custom access markers."""
import base64
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iosuite_shared import apppasswords, db, proxyauth, sessions, users
from iosuite_shared.fastapi import Auth


def make_app(name="iocontacts", **install):
    auth = Auth(name, register=False)
    app = FastAPI()
    app.middleware("http")(auth.middleware(open_paths=("/",), open_prefixes=("/api/auth/",)))
    app.include_router(auth.router())

    @app.get("/api/private", dependencies=[auth.access.signed_in])
    def private():
        return {"ok": True}

    @app.get("/api/peer", dependencies=[auth.access.public])
    def peer(request: Request):
        return {"peer": proxyauth.peer_of(request), "client": request.client.host, "scheme": request.url.scheme}

    @app.post("/api/typed", dependencies=[auth.access.public])
    def typed():
        raise TypeError("a bug, not bad input")

    auth.install(app, **install)
    return app, auth


def basic(name, pw):
    return {"authorization": "Basic " + base64.b64encode(f"{name}:{pw}".encode()).decode()}


@pytest.fixture(autouse=True)
def no_attempts():
    users._attempts.clear()
    apppasswords.forget()
    yield
    users._attempts.clear()
    apppasswords.forget()


def test_basic_and_bearer_are_throttled_before_any_hash(monkeypatch):
    app, _ = make_app()
    u = users.create("alex", "secret123", "editor", "iocontacts")
    pw = apppasswords.create(u["id"], "iocontacts", "Mac")
    c = TestClient(app, base_url="http://contacts.example.com")
    hashes = []
    monkeypatch.setattr("iosuite_shared.passwords.check_password", lambda p, h: hashes.append(p) or False)
    for i in range(users.LOCK_AFTER + 1):  # LOCK_AFTER free tries, the next one earns the first wait
        assert c.get("/api/private", headers=basic("alex", f"wrong{i}")).status_code == 401
    r = c.get("/api/private", headers=basic("alex", "wrong-again"))
    assert r.status_code == 429 and r.headers["retry-after"] and "try again" in r.json()["detail"]
    n = len(hashes)
    assert c.get("/api/private", headers=basic("alex", "wrong-again")).status_code == 429
    assert len(hashes) == n, "a throttled attempt costs no scrypt"
    # the lock is on (visitor, name) and on the name: a bearer token (no name) from the same visitor still works
    monkeypatch.undo()
    assert c.get("/api/private", headers={"authorization": "Bearer " + pw}).status_code == 200
    assert c.get("/api/private", headers=basic("alex", pw)).status_code == 429
    users._attempts.clear()
    assert c.get("/api/private", headers=basic("alex", pw)).status_code == 200


def test_a_repeated_wrong_password_is_refused_from_a_short_memory(monkeypatch):
    users.create("alex", "secret123", "editor", "iocontacts")
    calls = []
    real = apppasswords.check_password
    monkeypatch.setattr(apppasswords, "check_password", lambda p, h: calls.append(p) or real(p, h))
    monkeypatch.setattr("iosuite_shared.users.check_password", lambda p, h: calls.append(p) or real(p, h))
    assert apppasswords.authenticate("alex", "nope", "iocontacts") is None
    n = len(calls)
    assert apppasswords.authenticate("alex", "nope", "iocontacts") is None
    assert len(calls) == n, "the same wrong password within a few seconds is answered from memory"
    apppasswords.forget()
    assert apppasswords.authenticate("alex", "nope", "iocontacts") is None and len(calls) > n


def test_login_route_locks_the_name_and_the_address():
    app, _ = make_app()
    users.create("alex", "secret123", "editor", "iocontacts")
    c = TestClient(app, base_url="http://contacts.example.com")
    for _ in range(users.LOCK_AFTER + 1):
        assert c.post("/api/auth/login", json={"name": "alex", "password": "wrong"}).status_code == 401
    r = c.post("/api/auth/login", json={"name": "alex", "password": "secret123"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= users.LOCK_BASE
    assert users.attempt_wait("testclient", "alex") >= users.LOCK_BASE
    users.attempt_succeeded("testclient", "alex")
    assert c.post("/api/auth/login", json={"name": "alex", "password": "secret123"}).status_code == 200


def test_cross_site_uses_the_forwarded_host():
    app, _ = make_app()
    users.create("alex", "secret123", "editor", "iocontacts")
    c = TestClient(app, base_url="http://contacts.example.com")
    assert c.post("/api/auth/login", json={"name": "alex", "password": "secret123"}).status_code == 200
    ok = {"origin": "https://contacts.example.com", "x-forwarded-host": "contacts.example.com", "host": "10.0.0.5:8640"}
    assert c.post("/api/auth/settings", json={"a": 1}, headers=ok).status_code == 200
    bad = {"origin": "https://evil.example.net", "x-forwarded-host": "contacts.example.com"}
    assert c.post("/api/auth/settings", json={"a": 1}, headers=bad).status_code == 403


def test_forwarded_headers_keep_the_real_peer():
    app, _ = make_app(forwarded_allow_ips="10.0.0.0/8")
    c = TestClient(app, base_url="http://contacts.example.com")
    r = c.get("/api/peer", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2", "x-forwarded-proto": "https"}).json()
    assert r["peer"] == "testclient"  # the TestClient's address is not in 10/8: the headers are not believed
    assert r["client"] == "testclient" and r["scheme"] == "http"
    app, _ = make_app(forwarded_allow_ips="*")
    c = TestClient(app, base_url="http://contacts.example.com")
    r = c.get("/api/peer", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2", "x-forwarded-proto": "https"}).json()
    assert r == {"peer": "testclient", "client": "203.0.113.9", "scheme": "https"}


def test_a_header_proxy_needs_its_address_named():
    db.set_setting("auth_proxy", "nginx", "")
    pr = proxyauth.Proxy("iocontacts")
    assert pr.trusted() == [] and pr.identity({"Remote-User": "alex"}, "127.0.0.1") is None
    with pytest.raises(ValueError):
        proxyauth.save_suite({"AUTH_PROXY": "haproxy"})
    proxyauth.save_suite({"AUTH_PROXY": "haproxy", "AUTH_PROXY_TRUSTED": "10.0.0.1"})
    assert pr.identity({"Remote-User": "alex"}, "10.0.0.1") == "alex" and pr.identity({"Remote-User": "alex"}, "10.0.0.2") is None
    db.set_setting("auth_proxy", "cloudflare", "")
    db.set_setting("auth_proxy_trusted", "", "")
    assert [str(n) for n in pr.trusted()] == ["127.0.0.1/32", "::1/128"]  # Cloudflare: the token is verified, loopback stands


def test_cloudflare_token_is_verified_against_the_team_keys(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    team, aud = "team.cloudflareaccess.com", ["a" * 64]

    class Signing:
        def __init__(self, k):
            self.key = k.public_key()

    class Client:
        def __init__(self, *a, **kw):
            pass

        def get_signing_key_from_jwt(self, token):
            return Signing(key)

    monkeypatch.setattr(jwt, "PyJWKClient", Client)
    proxyauth._jwks.update(team=None, client=None)
    claims = {"email": "alex@example.com", "aud": aud, "iss": f"https://{team}", "exp": int(time.time()) + 60}
    good = jwt.encode(claims, key, algorithm="RS256")
    assert proxyauth._cf_email(good, team, aud) == "alex@example.com"
    assert proxyauth._cf_email(jwt.encode(claims, other, algorithm="RS256"), team, aud) is None  # another key
    assert proxyauth._cf_email(jwt.encode({**claims, "aud": ["b" * 64]}, key, algorithm="RS256"), team, aud) is None
    assert proxyauth._cf_email(jwt.encode({**claims, "exp": int(time.time()) - 120}, key, algorithm="RS256"), team, aud) is None
    assert proxyauth._cf_email(jwt.encode({**claims, "iss": "https://other.cloudflareaccess.com"}, key, algorithm="RS256"), team, aud) is None


def test_session_is_renewed_once_a_day_and_the_cookie_set_again():
    app, _ = make_app()
    u = users.create("alex", "secret123", "editor", "iocontacts")
    c = TestClient(app, base_url="http://contacts.example.com")
    assert c.post("/api/auth/login", json={"name": "alex", "password": "secret123"}).status_code == 200
    tok = c.cookies[sessions.COOKIE]
    assert "set-cookie" not in c.get("/api/private").headers  # fresh: nothing to renew
    db.ex("UPDATE ios_sessions SET renewed_at = renewed_at - 90000, expires_at = expires_at - 90000 WHERE user_id = %s", (u["id"],))
    sessions.invalidate()
    r = c.get("/api/private")
    assert r.status_code == 200 and sessions.COOKIE in r.headers.get("set-cookie", "")
    row = db.one("SELECT renewed_at, expires_at FROM ios_sessions WHERE user_id = %s", (u["id"],))
    assert time.time() - row["renewed_at"] < 60 and row["expires_at"] > time.time() + (sessions.SESSION_DAYS - 1) * 86400
    assert c.cookies[sessions.COOKIE] == tok


def test_expired_sessions_are_purged_at_most_hourly():
    users.create("alex", "secret123", "editor", "iocontacts")
    uid = users.find("alex")["id"]
    db.ex("INSERT INTO ios_sessions (token_hash, user_id, app, via, created_at, expires_at, renewed_at) VALUES ('x' , %s, 'iocontacts', 'password', 1, 2, 1)", (uid,))
    sessions._purged["at"] = 0
    sessions.create(uid, "iocontacts", "password")
    assert db.one("SELECT 1 AS x FROM ios_sessions WHERE token_hash = 'x'") is None
    db.ex("INSERT INTO ios_sessions (token_hash, user_id, app, via, created_at, expires_at, renewed_at) VALUES ('y' , %s, 'iocontacts', 'password', 1, 2, 1)", (uid,))
    sessions.create(uid, "iocontacts", "password")
    assert db.one("SELECT 1 AS x FROM ios_sessions WHERE token_hash = 'y'"), "within the hour nothing is purged"


def test_pool_reuses_connections_and_drops_broken_ones():
    pool = db.Pool(db.connect, size=2)
    with pool.tx() as con:
        first = con
    with pool.tx() as con:
        assert con is first
    with pytest.raises(RuntimeError):
        with pool.tx() as con:
            raise RuntimeError("the unit of work failed: rolled back, connection kept")
    with pool.tx() as con:
        assert con is first
    first.close()  # the server dropped it: the next borrow notices and replaces it
    with pool.tx() as con, con.cursor() as cur:
        cur.execute("SELECT 1 AS x")
        assert cur.fetchone()["x"] == 1
    pool.drain()
    assert pool._kept.empty()


def test_a_type_error_in_a_route_is_a_500_not_a_400():
    app, _ = make_app()
    c = TestClient(app, base_url="http://contacts.example.com", raise_server_exceptions=False)
    assert c.post("/api/typed").status_code == 500


def test_custom_access_marker_is_declared_and_enforced():
    from iosuite_shared import testing
    auth = Auth("iohealth", register=False)
    auth.access.uploader = auth.access.custom("uploader", lambda user, request: bool(user) and user.get("role") == "editor", "Uploads need an editor")
    app = FastAPI()
    app.middleware("http")(auth.middleware(open_prefixes=("/api/auth/",)))
    app.include_router(auth.router())

    @app.post("/v1/sessions", dependencies=[auth.access.uploader])
    def upload():
        return {"ok": True}

    testing.check_access_declared(app, auth.access.declared, prefix="/")
    users.create("alex", "secret123", "editor", "iohealth")
    users.create("vic", "secret123", "viewer", "iohealth")
    c = TestClient(app, base_url="http://health.example.com")
    assert c.post("/v1/sessions").status_code == 401
    assert c.post("/v1/sessions", headers=basic("vic", "secret123")).status_code == 403
    assert c.post("/v1/sessions", headers=basic("alex", "secret123")).status_code == 200


def test_mcp_session_manager_can_be_started_again():
    """mcp.running resets a private attribute of the MCP library; this fails the day the library renames it."""
    from iosuite_shared import mcp
    s = mcp.make_server("t", "T", "1", "")
    s.streamable_http_app()  # the session manager exists once the app is built
    assert hasattr(s.session_manager, "_has_started")
