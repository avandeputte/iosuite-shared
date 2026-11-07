import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iosuite_shared import admin, apppasswords, db, sessions, suite, users
from iosuite_shared.fastapi import Auth


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


def test_overview_lists_every_app_and_effective_roles():
    suite.register("iopix", name="ioPix", url="https://photo.example.com", version="1.0")
    alex = users.create("alex", "secret123", suite_admin=True)
    kid = users.create("kid", "")
    ed = users.create("ed", "secret123", "editor", "iopix")
    users.set_role(ed["id"], "oldapp", "admin")  # an app that is not registered any more
    db.set_setting("default_role", "none", "iocalendar")
    apppasswords.create(ed["id"], "iocontacts", "Phone")
    o = admin.overview()
    keys = [a["app"] for a in o["apps"]]
    assert keys[0] == "iopix" and set(keys) == {"iopix", "oldapp", "iocalendar", "iocontacts"}
    by = {a["app"]: a for a in o["apps"]}
    assert by["iopix"]["registered"] and not by["oldapp"]["registered"] and by["iocalendar"]["defaultRole"] == "none"
    assert by["iocontacts"]["appPasswords"] == 1
    u = {x["name"]: x for x in o["users"]}
    assert u["alex"]["effective"] == {k: "admin" for k in keys}
    assert u["kid"]["effective"]["iopix"] == "viewer" and u["kid"]["effective"]["iocalendar"] == "none"
    assert u["ed"]["roles"] == {"iopix": "editor", "oldapp": "admin"} and u["ed"]["appPasswords"] == 1
    assert alex["id"] and kid["passwordless"]


def test_roles_reset_to_default_and_checks():
    suite.register("ionotes", name="ioNotes")
    me = users.create("alex", "secret123", suite_admin=True)
    kid = users.create("kid", "")
    with pytest.raises(ValueError):
        admin.update(me, kid["id"], {"roles": {"iopix": "editor"}})  # no password: viewer at most
    got = admin.update(me, kid["id"], {"password": "longenough", "roles": {"iopix": "editor", "ionotes": "none"}})
    assert got["roles"] == {"iopix": "editor", "ionotes": "none"} and not got["passwordless"]
    got = admin.update(me, kid["id"], {"roles": {"ionotes": None}})
    assert got["roles"] == {"iopix": "editor"} and got["effective"]["ionotes"] == "viewer"
    with pytest.raises(ValueError):
        admin.update(me, me["id"], {"suiteAdmin": False})  # not your own
    with pytest.raises(ValueError):
        admin.delete(me, me["id"])
    with pytest.raises(ValueError):
        admin.update(me, kid["id"], {"roles": {"Bad App!": "viewer"}})
    with pytest.raises(LookupError):
        admin.update(me, 9999, {"name": "x"})
    with pytest.raises(ValueError):
        admin.set_app("iopix", default_role="admin")
    a = admin.set_app("iopix", default_role="none", require_login=True)
    assert a["defaultRole"] == "none" and a["requireLogin"]
    assert users.role_of(users.get(me["id"]), "iopix") == "admin"
    admin.set_app("iopix", default_role="viewer", require_login=False)
    assert db.settings_of("iopix") == {}


def test_app_passwords_and_forget_app():
    me = users.create("alex", "secret123", suite_admin=True)
    ed = users.create("ed", "secret123")
    kid = users.create("kid", "")
    with pytest.raises(ValueError):
        admin.new_app_password(kid["id"], "iocontacts", "Phone")
    pw = admin.new_app_password(ed["id"], "iocontacts", "Phone", "dav")["password"]
    assert apppasswords.authenticate("ed", pw, "iocontacts")["id"] == ed["id"]
    items = admin.app_passwords()
    assert [(i["userName"], i["app"], i["label"], i["scope"]) for i in items] == [("ed", "iocontacts", "Phone", "dav")]
    with pytest.raises(ValueError):
        admin.set_app_password_scope(items[0]["id"], "bogus")
    admin.set_app_password_scope(items[0]["id"], "")
    assert admin.app_passwords()[0]["scope"] == ""
    assert apppasswords.authenticate_scoped("ed", pw, "iocontacts")[1] == ""
    admin.revoke_app_password(items[0]["id"])
    assert admin.app_passwords() == [] and apppasswords.authenticate(None, pw, "iocontacts") is None
    users.set_role(ed["id"], "oldapp", "editor")
    admin.new_app_password(ed["id"], "oldapp", "x")
    assert admin.forget_app("oldapp", data=True)
    assert users.roles_of(ed["id"]) == {} and admin.app_passwords() == []
    assert me


def test_api_suite_admins_only():
    app = make_app()
    users.create("alex", "secret123", suite_admin=True)
    users.create("ann", "secret123", "admin", "iosuite")  # admin of this app, not of the suite
    c = signed_in(app, "alex", "secret123")
    o = c.get("/api/suite-admin").json()
    assert {u["name"] for u in o["users"]} == {"alex", "ann"} and o["me"]
    assert signed_in(app, "ann", "secret123").get("/api/suite-admin").status_code == 403
    assert TestClient(app, base_url="http://home.iothing.net").get("/api/suite-admin").status_code == 401
    r = c.post("/api/suite-admin/users", json={"name": "bob", "password": "secret123", "roles": {"iopix": "editor"}, "listed": False})
    assert r.status_code == 200 and r.json()["roles"] == {"iopix": "editor"} and not r.json()["listed"]
    bob = r.json()["id"]
    assert c.post("/api/suite-admin/users", json={"name": "bob", "password": "secret123"}).status_code == 400
    assert c.patch(f"/api/suite-admin/users/{bob}", json={"displayName": "Bobby"}).json()["displayName"] == "Bobby"
    assert c.patch("/api/suite-admin/users/9999", json={"displayName": "x"}).status_code == 404
    b = signed_in(app, "bob", "secret123")
    assert len(c.get(f"/api/suite-admin/users/{bob}/sessions").json()["sessions"]) == 1
    assert c.delete(f"/api/suite-admin/users/{bob}/sessions").status_code == 200
    assert b.get("/api/auth/me").json()["user"] is None
    r = c.post("/api/suite-admin/app-passwords", json={"userId": bob, "app": "iocontacts", "label": "Phone"})
    assert r.json()["password"].startswith("iocontacts-")
    items = c.get(f"/api/suite-admin/app-passwords?user={bob}").json()["items"]
    assert len(items) == 1 and c.delete(f"/api/suite-admin/app-passwords/{items[0]['id']}").status_code == 200
    assert c.delete("/api/suite-admin/app-passwords/9999").status_code == 404
    assert c.patch("/api/suite-admin/apps/iopix", json={"defaultRole": "none"}).json()["defaultRole"] == "none"
    assert c.delete(f"/api/suite-admin/users/{bob}").status_code == 200
    assert c.delete("/api/suite-admin/apps/nothere").status_code == 404
    assert sessions


def test_panel_module_for_suite_admins():
    app = make_app()
    users.create("alex", "secret123", suite_admin=True)
    users.create("ann", "secret123", "admin", "iosuite")
    r = signed_in(app, "alex", "secret123").get("/api/suite-admin/ui.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "export async function mount" in r.text
    assert signed_in(app, "ann", "secret123").get("/api/suite-admin/ui.js").status_code == 403
