import pytest

from iosuite_shared import apppasswords, sessions, users


def test_create_authenticate_roles():
    assert not users.any_users()
    u = users.create("alex", "secret123", "admin", "iopix", email="Alex@Example.com", suite_admin=True)
    assert u["email"] == "alex@example.com" and u["suiteAdmin"] and u["role"] == "admin"
    assert users.any_users()
    assert users.authenticate("alex", "wrong") is None
    assert users.authenticate("alex", "secret123", "ionotes")["role"] == "admin"  # a suite admin everywhere
    assert users.authenticate("ALEX@example.com", "secret123")["id"] == u["id"]  # by email, case-insensitive
    v = users.create("kid", "", "viewer", "iopix")
    assert v["passwordless"] and v["role"] == "viewer"
    assert users.role_of(v, "ionotes") == "viewer"  # the default role
    users.db.set_setting("default_role", "none", "ionotes")
    assert users.role_of(v, "ionotes") == "none"
    with pytest.raises(ValueError):
        users.set_role(v["id"], "iopix", "bogus")
    with pytest.raises(ValueError):
        users.update(v["id"], role="editor", app="iopix")  # no password: viewer at most
    users.update(v["id"], password="longenough", role="editor", app="iopix")
    assert users.get(v["id"], "iopix")["role"] == "editor"
    assert users.get(v["id"], "ionotes")["role"] == "none"
    with pytest.raises(ValueError):
        users.update(v["id"], password="")  # an editor somewhere: keeps its password
    with pytest.raises(ValueError):
        users.create("Alex", "another12")  # names are unique, case-insensitively
    with pytest.raises(ValueError):
        users.delete(u["id"])  # the only suite admin
    assert [a["name"] for a in users.accounts("ionotes")] == ["alex"]  # kid has no access there
    assert {a["name"] for a in users.accounts("iopix")} == {"alex", "kid"}


def test_settings_and_data():
    u = users.create("alex", "secret123")
    assert users.get_settings(u["id"], "iopix") == {}
    out = users.set_settings(u["id"], "iopix", {"theme": "dark", "grid": 4, "x" * 65: "ignored"})
    assert out == {"theme": "dark", "grid": 4}
    assert users.set_settings(u["id"], "iopix", {"theme": None}) == {"grid": 4}
    assert users.get_settings(u["id"], "ionotes") == {}  # per app
    users.set_value(u["id"], "iopix", "person", 42)
    assert users.get_value(u["id"], "iopix", "person") == 42
    users.set_value(u["id"], "iopix", "person", None)
    assert users.get_value(u["id"], "iopix", "person") is None


def test_sessions():
    u = users.create("alex", "secret123", "editor", "iopix")
    tok = sessions.create(u["id"], "iopix", "password")
    got = sessions.user_from_token(tok, "ionotes")  # the same session serves another app, with the role there
    assert got["id"] == u["id"] and got["role"] == "viewer"
    users.update(u["id"], password="changed123")  # ends every session
    assert sessions.user_from_token(tok, "iopix") is None
    tok = sessions.create(u["id"], "iopix", "password")
    sessions.end_all(u["id"])
    assert sessions.user_from_token(tok, "iopix") is None
    assert sessions.user_from_token("garbage", "iopix") is None


def test_cookie_domain(monkeypatch):
    monkeypatch.setattr("iosuite_shared.config.COOKIE_DOMAIN", "example.com")
    assert sessions.cookie_domain("photo.example.com") == "example.com"
    assert sessions.cookie_domain("notes.example.com:8610") == "example.com"
    assert sessions.cookie_domain("192.168.1.5:8600") is None
    assert sessions.cookie_domain("evil-example.com") is None


def test_app_passwords():
    u = users.create("alex", "secret123", "editor", "iocontacts")
    pw = apppasswords.create(u["id"], "iocontacts", "Phone")
    assert pw.startswith("iocontacts-")
    assert apppasswords.authenticate("alex", pw, "iocontacts")["id"] == u["id"]
    assert apppasswords.authenticate(None, pw, "iocontacts")["id"] == u["id"]  # bearer: no name needed
    assert apppasswords.authenticate("alex", pw, "iopix") is None  # one app only
    assert apppasswords.authenticate("alex", "secret123", "iocontacts")["id"] == u["id"]  # the account password works too
    assert apppasswords.authenticate("alex", "nope", "iocontacts") is None
    assert len(apppasswords.list_for(u["id"], "iocontacts")) == 1
    apppasswords.delete(u["id"], apppasswords.list_for(u["id"], "iocontacts")[0]["id"])
    assert apppasswords.authenticate("alex", pw, "iocontacts") is None


def test_legacy_hash_is_upgraded():
    from iosuite_shared import db
    from iosuite_shared.passwords import needs_rehash
    import base64, hashlib, os
    salt = os.urandom(16)
    h = hashlib.scrypt(b"oldpass123", salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    old = f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"
    db.ex("INSERT INTO ios_users (name, hash, created_at) VALUES ('old', %s, 1)", (old,))
    assert needs_rehash(old)
    u = users.authenticate("old", "oldpass123")
    assert u and not needs_rehash(db.one("SELECT hash FROM ios_users WHERE id = %s", (u["id"],))["hash"])


def test_shared_db_names(monkeypatch):
    import importlib
    from iosuite_shared import config
    for k in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
        monkeypatch.delenv("IOSUITE_DB_" + k, raising=False)
    try:
        importlib.reload(config)
        assert (config.DB_NAME, config.DB_USER, config.DB_PASSWORD) == ("iosuite", "iosuite", "iosuite")
        monkeypatch.setenv("IOSUITE_DB_NAME", "suite")
        monkeypatch.setenv("IOTHING_AUTH_DB_USER", "auth")  # a name from before 0.6: not read
        importlib.reload(config)
        assert (config.DB_NAME, config.DB_USER) == ("suite", "iosuite")
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_cli_admin_creates_then_promotes(monkeypatch, capsys):
    from iosuite_shared import cli, users
    monkeypatch.setenv("ADMIN_PASSWORD", "long-enough-1")
    cli.main(["admin", "alex", "--app", "iocalendar"])
    u = users.find("alex")
    assert u["suiteAdmin"] and users.roles_of(u["id"]).get("iocalendar") == "admin"
    cli.main(["admin", "sam", "--app", "iocalendar"])
    cli.main(["admin", "sam", "--app", "ionotes"])
    s = users.find("sam")
    assert not s["suiteAdmin"] and users.roles_of(s["id"]) == {"iocalendar": "admin", "ionotes": "admin"}
    cli.main(["check"])
    assert "2 account(s)" in capsys.readouterr().out
