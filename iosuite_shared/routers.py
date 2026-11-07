"""The routers every app mounts: /api/auth/* (sign-in, the account's own settings, passwords, sessions, app
passwords), /api/users (this app's admins), /api/suite-admin (suite admins), /api/server + /api/log (maintenance)
and /api/status. Each takes the app's Auth; Auth.router() and friends call them."""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from iosuite_shared import admin as suite_admin, apppasswords, proxyauth, sessions, suite, users
from iosuite_shared.access import json_body, public_user
from iosuite_shared.proxyauth import authlog, peer_of

STATUS_PATH = "/api/status"
log = logging.getLogger("iosuite_shared.routers")


def auth_router(self) -> APIRouter:
    auth, access, proxy, app = self, self.access, self.proxy, self.app
    r = APIRouter()

    @r.post("/api/auth/login", dependencies=[access.public])
    async def api_login(request: Request):
        body = await json_body(request)
        name, src = str(body.get("name", ""))[:64], proxy.source(request)
        visitor = proxy.visitor_of(request)
        wait = users.attempt_wait(visitor, name)
        if wait:
            authlog.warning("sign-in as %r from %s refused: %d s to wait after repeated failures", name, src, wait)
            raise HTTPException(429, f"Too many attempts; try again in {wait} seconds", headers={"Retry-After": str(wait)})
        u = await run_in_threadpool(users.authenticate, name, str(body.get("password", ""))[:1024], app)
        if not u:
            wait = users.attempt_failed(visitor, name)
            authlog.warning("failed sign-in as %r from %s (wrong user name or password%s)", name, src, f"; {wait} s wait" if wait else "")
            raise HTTPException(401, "Wrong user name or password")
        users.attempt_succeeded(visitor, name)
        if u.get("passwordless") and proxy.external(request.headers, peer_of(request)):
            authlog.warning("one-click sign-in as %s refused from %s (outside: a password is required)", u["name"], src)
            raise HTTPException(403, "From outside your network, sign in with an account that has a password")
        if u["role"] == "none":
            authlog.warning("sign-in as %s refused: no access to %s", u["name"], app)
            raise HTTPException(403, f"Your account has no access to {auth.label}")
        authlog.info("signed in: %s (%s, %s) from %s", u["name"], u["role"], "one click" if u.get("passwordless") else "password", src)
        u = auth.decorate(u)
        resp = JSONResponse({"user": u, "settings": users.get_settings(u["id"], app)})
        auth.sign_in(resp, request, u, "oneclick" if u.get("passwordless") else "password")
        return resp

    @r.get("/api/auth/accounts", dependencies=[access.public])
    def api_accounts(request: Request):
        out = users.accounts(app) if users.any_users() else []
        if proxy.external(request.headers, peer_of(request)):
            out = [a for a in out if not a["passwordless"]]
        return {"accounts": [auth.decorate(a) for a in out]}

    @r.post("/api/auth/logout", dependencies=[access.public])
    def api_logout(request: Request):
        u = auth.current_user(request)
        if u:
            authlog.info("signed out: %s from %s", u["name"], proxy.source(request))
        sessions.end(request.cookies.get(sessions.COOKIE))
        outside = proxy.external(request.headers, peer_of(request))
        resp = JSONResponse({"ok": True, "cloudflareLogout": "/cdn-cgi/access/logout" if outside else None})
        sessions.clear_cookie(resp, sessions.host_of(request), cloudflare_cookie=not outside)
        return resp

    @r.post("/api/auth/logout_all", dependencies=[access.signed_in])
    def api_logout_all(request: Request):
        u = auth.current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first")
        sessions.end_all(u["id"])
        authlog.info("signed out everywhere: %s from %s", u["name"], proxy.source(request))
        resp = JSONResponse({"ok": True})
        auth.sign_in(resp, request, u, request.cookies.get(sessions.VIA_COOKIE) or "password")
        return resp

    @r.get("/api/auth/me", dependencies=[access.public])
    def api_me(request: Request):
        return auth.me(request)

    @r.post("/api/auth/settings", dependencies=[access.signed_in])
    async def api_my_settings(request: Request):
        u = auth.current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first")
        return users.set_settings(u["id"], app, await json_body(request))

    @r.post("/api/auth/password", dependencies=[access.signed_in])
    async def api_my_password(request: Request):
        u = auth.current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first")
        body = await json_body(request)
        if u.get("passwordless"):
            raise HTTPException(403, "This account signs in with one click; an admin can give it a password under Settings › Users")
        pkey = f"pw|{u['id']}"
        wait = users.login_wait(pkey)
        if wait:
            raise HTTPException(429, f"Too many attempts; try again in {wait} seconds", headers={"Retry-After": str(wait)})
        current, new = str(body.get("current", ""))[:1024], str(body.get("password", ""))[:1024]
        if not new:
            raise HTTPException(400, "A new password is needed")  # removing a password is an admin's decision, never this route's
        if not await run_in_threadpool(users.authenticate, u["name"], current):
            users.login_failed(pkey)
            authlog.warning("password change for %s refused from %s (wrong current password)", u["name"], proxy.source(request))
            raise HTTPException(403, "The current password is wrong")
        users.login_succeeded(pkey)
        try:
            users.update(u["id"], password=new)
        except ValueError as e:
            raise HTTPException(400, str(e))
        apppasswords.forget()
        authlog.info("password changed: %s from %s", u["name"], proxy.source(request))
        resp = JSONResponse({"ok": True})
        auth.sign_in(resp, request, u, request.cookies.get(sessions.VIA_COOKIE) or "password")  # the old sessions are dead now; this one goes on
        return resp

    @r.post("/api/auth/setup", dependencies=[access.public])
    async def api_setup(request: Request):
        """The first account (a suite admin), allowed only while no account exists, from inside the network."""
        if users.any_users():
            raise HTTPException(409, "Users already exist; sign in as an admin")
        if proxy.external(request.headers, peer_of(request)):
            authlog.warning("first-account setup refused from outside (%s)", proxy.source(request))
            raise HTTPException(403, "The first account is created from inside your network (or on the server itself)")
        body = await json_body(request)
        try:
            u = users.create(str(body.get("name", ""))[:64], str(body.get("password", ""))[:1024], "admin", app,
                             email=str(body.get("email") or ""), display_name=str(body.get("displayName") or ""), suite_admin=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        authlog.info("first account created and signed in: %s (suite admin) from %s", u["name"], proxy.source(request))
        resp = JSONResponse({"user": u})
        auth.sign_in(resp, request, u, "password")
        return resp

    @r.get("/api/auth/sessions", dependencies=[access.signed_in])
    def api_sessions(request: Request):
        """The signed-in account's sessions in every app: where and how each began and was last seen."""
        u = auth.current_user(request)
        return {"sessions": sessions.list_sessions(u["id"], request.cookies.get(sessions.COOKIE)) if u else []}

    @r.delete("/api/auth/sessions/{session_id}", dependencies=[access.signed_in])
    def api_end_session(session_id: str, request: Request):
        """End one of the signed-in account's sessions (another browser or device)."""
        u = auth.current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first")
        sessions.end_one(u["id"], session_id)
        return {"ok": True}

    @r.get("/api/auth/app-passwords", dependencies=[access.signed_in])
    def api_app_passwords(request: Request):
        """The account's app passwords here; scope '' works everywhere, 'dav' for the sync protocols only. scopes:
        what this app offers (no 'dav' in an app without DAV)."""
        return {"items": apppasswords.list_for(auth.current_user(request)["id"], app), "scopes": ["", *auth.scope_paths]}

    @r.post("/api/auth/app-passwords", dependencies=[access.signed_in])
    async def api_new_app_password(request: Request):
        u = auth.current_user(request)
        if u.get("passwordless"):
            raise HTTPException(403, "A one-click account cannot have app passwords")
        body = await json_body(request)
        scope = str(body.get("scope") or "")
        if scope and scope not in auth.scope_paths:
            raise HTTPException(400, f"Unknown scope: {scope}")
        pw = await run_in_threadpool(apppasswords.create, u["id"], app, str(body.get("label") or "Device").strip(), scope)
        return {"password": pw, "name": u["name"], "scope": scope}

    @r.delete("/api/auth/app-passwords/{pid}", dependencies=[access.signed_in])
    def api_del_app_password(pid: int, request: Request):
        apppasswords.delete(auth.current_user(request)["id"], pid)
        return {"ok": True}

    @r.get("/api/auth/proxy", dependencies=[access.admin])
    def api_proxy_status():
        return proxy.status()

    @r.post("/api/auth/proxy", dependencies=[access.admin])
    async def api_proxy_save(request: Request):
        """{AUTH_PROXY, ...}: suite-wide, for every app."""
        proxy.save(await json_body(request))
        return proxy.status()

    @r.get("/api/auth/suite", dependencies=[access.signed_in])
    def api_suite(request: Request):
        """The suite's installed apps and where they are, for the start page and app switchers."""
        return {"items": suite.apps(), "app": app}

    @r.delete("/api/auth/suite/{key}", dependencies=[access.suite_admin])
    def api_suite_forget(key: str, request: Request):
        """Forget an app that was uninstalled (suite admins); it registers again if it starts."""
        if not suite.forget(key):
            raise HTTPException(404, "No such app")
        if key == app:
            auth._registered = False
        return {"items": suite.apps(), "app": app}

    return r

def users_router(self) -> APIRouter:
    """Accounts, as this app's admins see them: the role column is the role here. A suite admin may also set
    suiteAdmin and roles in other apps ({roles: {app: role}})."""
    auth, access, app = self, self.access, self.app
    r = APIRouter()

    @r.get("/api/users", dependencies=[access.admin])
    def api_users(request: Request):
        out = []
        for u in users.list_users(app):
            u = auth.decorate(u)
            u["roles"] = users.roles_of(u["id"])
            out.append(u)
        return {"users": out, "roles": list(users.APP_ROLES), "app": app, "suiteAdmin": users.is_suite_admin(auth.current_user(request))}

    @r.post("/api/users", dependencies=[access.admin])
    async def api_users_create(request: Request):
        body = await json_body(request)
        try:
            u = users.create(str(body.get("name", "")), str(body.get("password", "")), str(body.get("role", "viewer")), app,
                             email=str(body.get("email") or ""), display_name=str(body.get("displayName") or ""),
                             suite_admin=bool(body.get("suiteAdmin")) and users.is_suite_admin(auth.current_user(request)))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return auth.decorate(u)

    @r.patch("/api/users/{user_id}", dependencies=[access.admin])
    async def api_users_update(user_id: int, request: Request):
        body = await json_body(request)
        if not users.get(user_id):
            raise HTTPException(404, "No such user")
        try:
            auth._update(auth.current_user(request), user_id, body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "user": auth.decorate(users.get(user_id, app))}

    @r.get("/api/users/{user_id}/sessions", dependencies=[access.admin])
    def api_user_sessions(user_id: int):
        """An account's sessions (admins): app, client, address, through Cloudflare or not, last seen."""
        if not users.get(user_id):
            raise HTTPException(404, "No such user")
        return {"sessions": sessions.list_sessions(user_id)}

    @r.delete("/api/users/{user_id}/sessions", dependencies=[access.admin])
    def api_user_end_sessions(user_id: int, request: Request):
        """Sign an account out everywhere (admins), or one session with ?session=<id>."""
        if not users.get(user_id):
            raise HTTPException(404, "No such user")
        sid = request.query_params.get("session")
        if sid:
            sessions.end_one(user_id, sid)
        else:
            sessions.end_all(user_id)
        return {"ok": True}

    @r.delete("/api/users/{user_id}", dependencies=[access.admin])
    def api_users_delete(user_id: int, request: Request):
        if not users.get(user_id):
            raise HTTPException(404, "No such user")
        try:
            auth._delete(auth.current_user(request), user_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    return r

def admin_router(self) -> APIRouter:
    """Suite administration (suite admins only), everything in the Shared ioSuite database at /api/suite-admin/*:
    accounts with their role in every app, every app password, sessions, every app's keys and tokens (never their
    values), and the apps with their access settings (iosuite_shared.admin)."""
    auth, access = self, self.access
    r = APIRouter(prefix="/api/suite-admin")

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except LookupError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except ValueError as e:
            raise HTTPException(400, str(e))

    def me(request: Request):
        """The caller; the route's access.suite_admin marker has already let only a suite admin this far."""
        return auth.current_user(request)

    @r.get("/ui.js", dependencies=[access.suite_admin])
    def api_ui():
        """The panel itself (static/suite-admin.js), one ES module every app mounts under its Settings › Suite:
        ui = await import(new URL('api/suite-admin/ui.js', document.baseURI).href); ui.mount(el, {sub, toast})."""
        return FileResponse(Path(__file__).with_name("static") / "suite-admin.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @r.get("", dependencies=[access.suite_admin])
    def api_overview(request: Request):
        return {**suite_admin.overview(), "me": (me(request) or {}).get("id")}

    @r.post("/users", dependencies=[access.suite_admin])
    async def api_create(request: Request):
        body = await json_body(request)
        u = await run_in_threadpool(call, suite_admin.create, me(request), body)
        authlog.info("suite admin: account %s made by %s", u["name"], (auth.current_user(request) or {}).get("name"))
        return u

    @r.patch("/users/{user_id}", dependencies=[access.suite_admin])
    async def api_update(user_id: int, request: Request):
        body = await json_body(request)
        u = await run_in_threadpool(call, suite_admin.update, me(request), user_id, body)
        if self.on_update:
            self.on_update(user_id, body)
        return u

    @r.delete("/users/{user_id}", dependencies=[access.suite_admin])
    def api_delete(user_id: int, request: Request):
        call(suite_admin.delete, me(request), user_id)
        return {"ok": True}

    @r.get("/photo-people", dependencies=[access.suite_admin])
    def api_photo_people(request: Request):
        """The people in ioPix's People, to link an account to (its person in photos), read from ioPix."""
        return suite_admin.photo_people(auth.app)

    @r.get("/photo-people/{person}/face.jpg", dependencies=[access.suite_admin])
    def api_photo_face(person: int, request: Request):
        return Response(call(suite_admin.photo_face, auth.app, person), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    @r.get("/users/{user_id}/sessions", dependencies=[access.suite_admin])
    def api_sessions(user_id: int, request: Request):
        if not users.get(user_id):
            raise HTTPException(404, "No such user")
        return {"sessions": sessions.list_sessions(user_id, request.cookies.get(sessions.COOKIE))}

    @r.delete("/users/{user_id}/sessions", dependencies=[access.suite_admin])
    def api_end_sessions(user_id: int, request: Request):
        """Every session of the account, or one with ?session=<id>."""
        call(suite_admin.end_sessions, user_id, request.query_params.get("session"))
        return {"ok": True}

    @r.get("/app-passwords", dependencies=[access.suite_admin])
    def api_app_passwords(request: Request, user: int = None, app: str = None):
        return {"items": call(suite_admin.app_passwords, user, app)}

    @r.post("/app-passwords", dependencies=[access.suite_admin])
    async def api_new_app_password(request: Request):
        body = await json_body(request)
        try:
            uid = int(body.get("userId"))
        except (TypeError, ValueError):
            raise HTTPException(400, "userId is needed")
        return await run_in_threadpool(call, suite_admin.new_app_password, uid, body.get("app"), str(body.get("label") or ""),
                                       str(body.get("scope") or ""))

    @r.patch("/app-passwords/{pid}", dependencies=[access.suite_admin])
    async def api_app_password_scope(pid: int, request: Request):
        body = await json_body(request)
        call(suite_admin.set_app_password_scope, pid, str(body.get("scope") or ""))
        return {"ok": True}

    @r.delete("/app-passwords/{pid}", dependencies=[access.suite_admin])
    def api_revoke_app_password(pid: int, request: Request):
        call(suite_admin.revoke_app_password, pid)
        return {"ok": True}

    @r.get("/keys", dependencies=[access.suite_admin])
    def api_keys(request: Request, app: str = None):
        """Every key and token the apps keep (never the values)."""
        return {"items": call(suite_admin.keys, app)}

    @r.put("/keys/{app}/{name}", dependencies=[access.suite_admin])
    async def api_set_key(app: str, name: str, request: Request):
        """{value}: replace it; '' clears it."""
        body = await json_body(request)
        who = me(request)
        k = await run_in_threadpool(call, suite_admin.set_key, who, app, name, body.get("value"))
        authlog.info("suite admin: %s %s of %s by %s", "set" if k["set"] else "cleared", name, app, (who or {}).get("name"))
        return k

    @r.delete("/keys/{app}/{name}", dependencies=[access.suite_admin])
    def api_clear_key(app: str, name: str, request: Request):
        who = me(request)
        call(suite_admin.set_key, who, app, name, None)
        authlog.info("suite admin: cleared %s of %s by %s", name, app, (who or {}).get("name"))
        return {"ok": True}

    @r.patch("/apps/{key}", dependencies=[access.suite_admin])
    async def api_app(key: str, request: Request):
        body = await json_body(request)
        return call(suite_admin.set_app, key, default_role=body.get("defaultRole"),
                    require_login=None if body.get("requireLogin") is None else bool(body["requireLogin"]))

    @r.delete("/apps/{key}", dependencies=[access.suite_admin])
    def api_forget_app(key: str, request: Request, data: int = 0):
        """Take an app off the list; ?data=1 also removes its roles, settings and app passwords."""
        if not call(suite_admin.forget_app, key, bool(data)):
            raise HTTPException(404, "No such app")
        if key == auth.app:
            auth._registered = False
        return {"ok": True}

    @r.get("/proxy", dependencies=[access.suite_admin])
    def api_proxy(request: Request):
        """Sign-in at a reverse proxy (Cloudflare Access...) for the whole suite: the values, the last token seen."""
        return {**proxyauth.suite_status(), "here": auth.proxy.status()}

    @r.put("/proxy", dependencies=[access.suite_admin])
    async def api_proxy_save(request: Request):
        """Body: any of AUTH_PROXY, AUTH_PROXY_HEADER, AUTH_PROXY_TRUSTED, AUTH_PROXY_CREATE, CF_ACCESS_TEAM,
        CF_ACCESS_AUD (empty clears one). CF_ACCESS_AUD: one AUD tag or several, comma-separated."""
        body = await json_body(request)
        call(proxyauth.save_suite, body)
        return {**proxyauth.suite_status(), "here": auth.proxy.status()}

    return r

def server_router(self, updater, log_file=None, info=None, apply=None) -> APIRouter:
    """Maintenance › Updates and logs, the same in every app (admins):
        GET  /api/server                 the installed version, the last check, whether update.sh can run, the switches
        POST /api/server {action, ref?}  action: check | update | restart; {autoCheck: bool}
                                         saves the daily-check switch; anything else goes to apply(body)
        GET  /api/log?lines&level&contains&requests   the tail of the server log (with log_file)
        GET  /api/log/download
    `info()` returns extra fields for GET /api/server (the app's: mcpEnabled, database...); `apply(body)` saves the
    app's own switches sent in the POST body and raises ValueError when one cannot be set."""
    from fastapi.responses import FileResponse
    from iosuite_shared import updates as upd
    access, label = self.access, self.label
    r = APIRouter()

    def server_info():
        extra = info() if info else {}
        return {**updater.status(), **(extra or {})}

    @r.get("/api/server", dependencies=[access.admin])
    async def api_server_info():
        return await run_in_threadpool(server_info)

    @r.post("/api/server", dependencies=[access.admin])
    async def api_server_action(request: Request):
        body = await json_body(request) if int(request.headers.get("content-length") or 0) else {}
        try:
            if "autoCheck" in body:
                await run_in_threadpool(updater.set_auto_check, bool(body["autoCheck"]))
            if apply:
                rr = apply(body)
                if hasattr(rr, "__await__"):
                    await rr
        except ValueError as e:
            raise HTTPException(400, str(e))
        action = body.get("action")
        if action == "check":
            return await run_in_threadpool(updater.check)
        if action == "update":
            try:
                updater.run(str(body.get("ref") or ""))
            except RuntimeError as e:
                raise HTTPException(409, str(e))
            return {"started": True}
        if action == "restart":
            authlog.info("%s restarted by %s", label, (self.current_user(request) or {}).get("name"))
            updater.restart()
            return {"ok": True}
        if action:
            raise HTTPException(400, "action must be check, update or restart")
        return await run_in_threadpool(server_info)

    if log_file:
        @r.get("/api/log", dependencies=[access.admin])
        def api_log(lines: int = 300, level: str = "any", contains: str = "", requests: int = 0):
            return upd.tail(log_file, lines, contains[:200], level if level in upd.LEVELS else "any", bool(requests))

        @r.get("/api/log/download", dependencies=[access.admin])
        def api_log_download():
            from pathlib import Path
            if not Path(log_file).exists():
                raise HTTPException(404, "No log file yet")
            return FileResponse(str(log_file), media_type="text/plain", filename=f"{self.app}.log", content_disposition_type="attachment")

    return r


# ---- the status: public, the same shape in every app
def status_router(self, updater=None, extra=None) -> APIRouter:
    """GET /api/status, open to everyone (the middleware keeps it reachable when sign-in is required): what the shell
    needs before anyone signs in, and for admins whether a newer version is out.
        {ok, app, name, version, usersExist, requireLogin, signedIn, isAdmin, viaProxy, user,
         commit (signed in), update (admins, while the daily check is on: {version, remote, ..., canRun, docker} or null)}
    `extra(request)` adds the app's own fields (a dict); it is called after the user is known, so it may answer
    differently for a visitor, an account and an admin."""
    auth, access = self, self.access
    r = APIRouter()

    @r.get(STATUS_PATH, dependencies=[access.public])
    async def api_status(request: Request):
        return await run_in_threadpool(status, auth, request, updater, extra)

    return r


def status(self, request: Request, updater=None, extra=None) -> dict:
    """What GET /api/status answers (also for an app that has to serve it from its own route): Auth.status."""
    u = self.current_user(request)
    signed, admin = users.is_signed_in(u), users.is_admin(u)
    out = {"ok": True, "app": self.app, "name": self.label, "version": (self.suite_entry or {}).get("version") or (updater.version if updater else None),
           "usersExist": users.any_users(), "requireLogin": users.require_login(self.app), "signedIn": signed, "isAdmin": admin,
           "viaProxy": bool(getattr(request.state, "via_proxy", False)), "user": public_user(u)}
    if updater is not None:
        if signed:
            out["commit"] = updater.local_commit()[:7]
        if admin:
            a = updater.available() if updater.auto_check_enabled() else None
            if a:
                a.update(canRun=updater.can_run(), docker=updater.in_docker())
            out["update"] = a
    if extra:
        out.update(extra(request) or {})
    return out

