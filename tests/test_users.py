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


def test_a_hash_with_other_parameters_is_upgraded_and_the_old_format_refused():
    from iosuite_shared import db
    from iosuite_shared.passwords import hash_password, needs_rehash
    import base64, hashlib, os
    salt = os.urandom(16)
    h = hashlib.scrypt(b"oldpass123", salt=salt, n=2 ** 13, r=8, p=1, dklen=32)
    weaker = f"scrypt2$8192$8$1${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"
    db.ex("INSERT INTO ios_users (name, hash, created_at) VALUES ('old', %s, 1)", (weaker,))
    assert needs_rehash(weaker) and not needs_rehash(hash_password("x"))
    u = users.authenticate("old", "oldpass123")
    assert u and not needs_rehash(db.one("SELECT hash FROM ios_users WHERE id = %s", (u["id"],))["hash"])
    # the format from before the suite (scrypt$salt$hash) is not read any more
    legacy = f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"
    db.ex("INSERT INTO ios_users (name, hash, created_at) VALUES ('older', %s, 1)", (legacy,))
    assert users.authenticate("older", "oldpass123") is None


def test_sign_in_by_name_never_picks_another_account_by_its_email():
    bob = users.create("bob", "secret123", "viewer", "iopix")
    users.create("alex@example.com", "other-pass", "viewer", "iopix")
    users.update(bob["id"], email="alex@example.com")
    assert users.authenticate("alex@example.com", "other-pass")["name"] == "alex@example.com"
    assert users.authenticate("alex@example.com", "secret123") is None  # the name wins; bob's email is not tried
    assert users.authenticate("bob", "secret123")["name"] == "bob"
