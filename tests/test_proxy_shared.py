"""Proxy sign-in settings live once, suite-wide, in the Shared ioSuite database: adopt() moves an app's config.env
values and its own rows there, the suite admin API changes them."""
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from iosuite_shared import config, db, proxyauth, users
from iosuite_shared.fastapi import Auth

AUD1, AUD2 = "a" * 64, "b" * 64


def make_app():
    auth = Auth("iosuite", register=False)
    app = FastAPI()
    app.middleware("http")(auth.middleware(open_prefixes=("/api/auth/",)))
    app.include_router(auth.router())
    app.include_router(auth.admin_router())
    return app


def signed_in(app, name, password):
    c = TestClient(app, base_url="http://home.iothing.net")
    assert c.post("/api/auth/login", json={"name": name, "password": password}).status_code == 200
    return c


def env_file(tmp_path, monkeypatch, text):
    """A config.env read by load_file, as an app does at import."""
    (tmp_path / "config.env").write_text(text, encoding="utf-8")
    for k in proxyauth.KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "FROM_FILE", {})
    config.load_file(tmp_path)
    for k in config.FROM_FILE:  # monkeypatch removes them again after the test
        monkeypatch.setenv(k, os.environ[k])
    return tmp_path / "config.env"


def test_helpers():
    assert proxyauth.clean_team("https://MyTeam.cloudflareaccess.com/") == "myteam.cloudflareaccess.com"
    assert proxyauth.aud_tags(f" {AUD1}, {AUD2} {AUD1}") == [AUD1, AUD2]
    assert proxyauth.merged("CF_ACCESS_AUD", AUD1, AUD2) == f"{AUD1}, {AUD2}"
    assert proxyauth.merged("CF_ACCESS_TEAM", "", "https://t.cloudflareaccess.com") == "t.cloudflareaccess.com"
    assert proxyauth.merged("CF_ACCESS_TEAM", "t.cloudflareaccess.com", "https://T.cloudflareaccess.com/") == "t.cloudflareaccess.com"
    assert proxyauth.merged("CF_ACCESS_TEAM", "t.cloudflareaccess.com", "other.cloudflareaccess.com") is None
    assert proxyauth.merged("AUTH_PROXY", "cloudflare", "Cloudflare") == "cloudflare"


def test_adopt_moves_config_env_and_rows(tmp_path, monkeypatch):
    p = env_file(tmp_path, monkeypatch, f"PORT=8600\nAUTH_PROXY=cloudflare\nCF_ACCESS_TEAM=https://t.cloudflareaccess.com\nCF_ACCESS_AUD={AUD1}\n")
    db.set_setting("cf_access_aud", AUD2, "iocalendar")  # another app's own AUD, from before
    db.set_setting("auth_proxy_create", "deny", "iopix")
    r = proxyauth.adopt("iopix")
    assert not r["kept"] and len(r["moved"]) == 4
    s = db.settings_of("")
    assert s["auth_proxy"] == "cloudflare" and s["cf_access_team"] == "t.cloudflareaccess.com" and s["cf_access_aud"] == AUD1
    assert s["auth_proxy_create"] == "deny" and not db.settings_of("iopix")
    text = p.read_text()
    assert "PORT=8600\n" in text and "\nAUTH_PROXY=" not in text and "# moved to the Shared ioSuite database" in text
    assert "CF_ACCESS_AUD" not in os.environ and config.proxy_env("AUTH_PROXY") is None
    proxyauth.adopt("iocalendar")  # its AUD joins the suite-wide list
    db.settings_cache.clear()
    assert db.settings_of("")["cf_access_aud"] == f"{AUD1}, {AUD2}" and not db.settings_of("iocalendar")
    pr = proxyauth.Proxy("ionotes")
    assert pr.mode() == "cloudflare" and pr.cf_settings() == ("t.cloudflareaccess.com", [AUD1, AUD2])
    assert proxyauth.adopt("iopix") == {"moved": [], "kept": {}}  # nothing left to move


def test_adopt_leaves_a_disagreeing_value(tmp_path, monkeypatch):
    db.set_setting("cf_access_team", "t.cloudflareaccess.com", "")
    p = env_file(tmp_path, monkeypatch, "CF_ACCESS_TEAM=other.cloudflareaccess.com\n")
    assert proxyauth.adopt("iocontacts")["kept"] == {"CF_ACCESS_TEAM": "config.env"}
    assert "CF_ACCESS_TEAM=other" in p.read_text() and os.environ["CF_ACCESS_TEAM"] == "other.cloudflareaccess.com"
    assert proxyauth.suite_status()["left"] == {"iocontacts": {"CF_ACCESS_TEAM": "config.env"}}
    assert proxyauth.Proxy("iocontacts").status()["conflicts"] == {"CF_ACCESS_TEAM": "config.env"}


def test_suite_admin_api(monkeypatch):
    for k in proxyauth.KEYS:
        monkeypatch.delenv(k, raising=False)
    app = make_app()
    users.create("alex", "secret123", suite_admin=True)
    users.create("ann", "secret123", "admin", "iosuite")
    c = signed_in(app, "alex", "secret123")
    assert c.get("/api/suite-admin/proxy").json()["values"]["AUTH_PROXY"] == ""
    assert c.put("/api/suite-admin/proxy", json={"AUTH_PROXY": "cloudflare", "CF_ACCESS_TEAM": "t.cloudflareaccess.com"}).status_code == 400
    r = c.put("/api/suite-admin/proxy", json={"AUTH_PROXY": "cloudflare", "CF_ACCESS_TEAM": "https://t.cloudflareaccess.com/", "CF_ACCESS_AUD": f"{AUD1} {AUD2}"}).json()
    assert r["values"]["CF_ACCESS_TEAM"] == "t.cloudflareaccess.com" and r["auds"] == [AUD1, AUD2] and r["here"]["cloudflareVerified"]
    assert c.put("/api/suite-admin/proxy", json={"AUTH_PROXY": "squid"}).status_code == 400
    db.set_setting("auth_proxy_trusted", "10.0.0.0/8", "ionotes")
    assert c.get("/api/suite-admin/proxy").json()["overrides"] == {"ionotes": {"AUTH_PROXY_TRUSTED": "10.0.0.0/8"}}
    assert c.delete("/api/suite-admin/proxy/overrides/ionotes").json()["overrides"] == {}
    assert signed_in(app, "ann", "secret123").get("/api/suite-admin/proxy").status_code == 403
    # an app's own Settings saves suite-wide too
    assert TestClient(app).get("/api/suite-admin/proxy").status_code == 401
    proxyauth.Proxy("iopix").save({"AUTH_PROXY_CREATE": "viewer"}, suite_wide=False)
    assert db.settings_of("")["auth_proxy_create"] == "viewer" and not db.settings_of("iopix")
