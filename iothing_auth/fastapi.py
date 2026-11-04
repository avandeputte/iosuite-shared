"""The FastAPI glue: one Auth object per app gives the middleware that works out who is asking, the route
dependencies that say who may call a route, and the routers with the sign-in and user-management API.

    auth = Auth("iopix")
    app.middleware("http")(auth.middleware(open_paths=..., open_prefixes=..., before=..., after=...))
    @router.get("/api/x", dependencies=[auth.access.admin])
    app.include_router(auth.router())        # /api/auth/* : login, accounts, logout, logout_all, me, settings, password, setup
    app.include_router(auth.users_router())  # /api/users  : list, create, change, delete (admins of this app)

The API shapes are those of iopix (ionotes copied them), so the apps' pages keep working.
"""
import base64
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from iothing_auth import apppasswords, db, sessions, suite, users
from iothing_auth.proxyauth import Proxy, authlog, peer_of, remember_peer, seen

ACCESS_ROLES = ("public", "signed_in", "editor", "admin", "suite_admin")


class Access:
    """The dependencies: public, signed_in, editor, editor_write, admin, admin_write, suite_admin. `read_only` is a
    callable the app gives: when it says True, every *_write route answers 403."""

    def __init__(self, read_only=None, read_only_message: str = "This app is in read-only mode; nothing can be changed."):
        self.read_only, self.read_only_message = read_only, read_only_message
        self.public = self.require("public")
        self.signed_in = self.require("signed_in")
        self.editor = self.require("editor")
        self.editor_write = self.require("editor", write=True)
        self.admin = self.require("admin")
        self.admin_write = self.require("admin", write=True)
        self.suite_admin = self.require("suite_admin")

    def require(self, role: str, write: bool = False):
        if role not in ACCESS_ROLES:
            raise ValueError(role)

        async def check(request: Request):
            user = getattr(request.state, "user", None)
            if write and self.read_only and self.read_only():
                raise HTTPException(403, self.read_only_message)
            if role == "public":
                return
            reading = request.method in ("GET", "HEAD", "OPTIONS")
            if role == "suite_admin" and not users.is_suite_admin(user):
                raise HTTPException(403 if user else 401, "Suite admins only")
            if role == "admin" and not users.is_admin(user):
                raise HTTPException(403 if user else 401, "Admins only" if reading else "Only an admin can change settings or run maintenance.")
            if role == "editor" and not users.can_write(user):
                raise HTTPException(403 if user else 401, "Editors only" if reading else "Sign in as an editor to make changes.")
            if role == "signed_in" and not users.is_signed_in(user):
                raise HTTPException(401, "Sign in to use this")

        check.access_role, check.access_write = role, write
        return Depends(check)

    @staticmethod
    def declared(route):
        """(role, write) a route declares, or None: for a test that checks every API route has one."""
        found = [(d.dependency.access_role, d.dependency.access_write) for d in route.dependencies if hasattr(d.dependency, "access_role")]
        return found[0] if len(found) == 1 else None


def json_body(request: Request):
    async def read():
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "The request body is not JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "A JSON object is expected")
        return body
    return read()


class Auth:
    def __init__(self, app: str, read_only=None, read_only_message: str = None, app_label: str = None, basic_realm: str = None,
                 version: str = None, public_url: str = None, description: str = None, color: str = None, icon: str = None,
                 register: bool = True):
        """version, public_url (the app's PUBLIC_URL; empty: the address it is reached at), description, color and icon
        go into the suite's list of apps (iothing_auth.suite) when the app first reaches the auth database; register=False
        keeps the app out of it."""
        self.app = app
        self.suite_entry = dict(name=app_label or app, url=public_url or None, version=version, description=description,
                                color=color, icon=icon) if register else None
        self._registered = False
        self.label = app_label or app
        self.proxy = Proxy(app)
        self.access = Access(read_only, read_only_message or f"{self.label} is in read-only mode; nothing can be changed.")
        self.realm = basic_realm or self.label
        self.log = logging.getLogger(f"iothing_auth.{app}")
        self.decorate = lambda user: user  # the app may add fields to the user dict it serves (iopix: the person tag)
        self.on_update = None  # on_update(user_id, body) after an admin changed an account: the app's own fields (iopix: the person tag)
        self.deny_exempt = ()  # path prefixes a denied proxy identity may still reach (iopix: /mcp, which signs in with its own token)
        remember_peer()

    # ---- the suite's list of apps
    def register(self) -> None:
        """Write this app's row in the suite's list (once per start; the middleware calls it). Never fails a request."""
        if self._registered or self.suite_entry is None:
            return
        try:
            suite.register(self.app, **self.suite_entry)
            self._registered = True
        except ValueError as e:  # a PUBLIC_URL that is not an address: registered without it
            self.log.warning("suite registration: %s; registering without the address", e)
            suite.register(self.app, **{**self.suite_entry, "url": None})
            self._registered = True
        except Exception as e:  # noqa: BLE001 - the table is the suite's convenience, never a reason to fail
            self.log.warning("could not register %s in the suite's list of apps: %s", self.app, e)
            self._registered = True

    def _note(self, request: Request) -> None:
        try:
            self.register()
            if self.suite_entry is not None:
                suite.note_seen(self.app, request)
        except Exception as e:  # noqa: BLE001
            self.log.debug("suite: %s", e)

    # ---- who is asking
    def current_user(self, request: Request):
        return getattr(request.state, "user", None)

    async def resolve(self, request: Request):
        """Who is asking: the session cookie, the proxy's identity, HTTP Basic or a bearer app password.
        Sets request.state.user, .via_proxy, .cookie_uid, .proxy_ident; returns a response to send instead when the
        proxy let in someone who has no account and that is denied."""
        session = request.cookies.get(sessions.COOKIE)
        request.state.origin = sessions.describe(request, self.proxy)
        user, renewed = (await run_in_threadpool(sessions.lookup, session, self.app, request.state.origin)) if session else (None, False)
        request.state.cookie_uid = user["id"] if user else None
        request.state.renew_cookie = renewed
        request.state.via_proxy = False
        request.state.proxy_ident = None
        peer = peer_of(request)
        if "cf-access-jwt-assertion" in request.headers and time.time() - seen.get("at", 0) > 60:
            self.proxy.note(request.headers, peer)
        if self.proxy.mode() != "off":
            pu = await run_in_threadpool(self.proxy.user, request)
            if pu:
                user, request.state.via_proxy = pu, True
            elif request.state.proxy_ident and self.proxy.deny_unknown() and not request.url.path.startswith(tuple(self.deny_exempt) or ("\0",)):
                request.state.user = None
                return self.no_access(request, request.state.proxy_ident)
        if not user and "authorization" in request.headers:
            user = await run_in_threadpool(self._authorization, request)
        if user and not request.state.via_proxy and user.get("passwordless") and self.proxy.external(request.headers, peer):
            user = None  # a passwordless account's session (begun on the LAN) does not work from outside
        if user and user.get("role") == "none":
            request.state.user = None
            request.state.no_role_user = user
            return None
        request.state.user = self.decorate(user) if user else None
        return None

    def _authorization(self, request: Request):
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                name, _, pw = base64.b64decode(h[6:].strip()).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            return apppasswords.authenticate(name, pw, self.app)
        if h.lower().startswith("bearer "):
            return apppasswords.authenticate(None, h[7:].strip(), self.app)
        return None

    def finish(self, request: Request, response):
        """After the route: set the session cookie when a proxy sign-in needs one here too (so the same browser stays
        signed in on the home network, where requests reach the app directly without Cloudflare)."""
        user, path = getattr(request.state, "user", None), request.url.path
        if user and getattr(request.state, "renew_cookie", False) and request.state.cookie_uid == user["id"] and "set-cookie" not in response.headers:
            via = request.cookies.get(sessions.VIA_COOKIE) or "password"
            sessions.set_cookie(response, request.cookies.get(sessions.COOKIE), sessions.host_of(request), via, sessions.over_https(request))
        if request.state.via_proxy and user and request.state.cookie_uid != user["id"] and path != "/api/auth/logout":
            via = "cloudflare" if self.proxy.mode() == "cloudflare" else "proxy"
            tok = sessions.create(user["id"], self.app, via, getattr(request.state, "origin", None))
            sessions.set_cookie(response, tok, sessions.host_of(request), via, sessions.over_https(request))
        return response

    def cross_site(self, request: Request) -> bool:
        """A write from a page on another site (or another port) driving this one with the browser's cookies."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return False
        origin, site = request.headers.get("origin"), request.headers.get("sec-fetch-site")
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
        return site == "cross-site" or bool(origin and (origin == "null" or urlsplit(origin).netloc.lower() != host))

    def middleware(self, open_paths=(), open_prefixes=("/api/auth/",), is_open=None, before=None, after=None, api_prefix="/api/"):
        """A complete auth middleware: resolve, the require-sign-in rule (open_paths / open_prefixes, or whatever
        `is_open(path)` says, stay reachable; /api/auth/ always is), the cross-site check on writes, and the cookie
        afterwards. `before(request)` runs once the user is known and may return a response (the app's own gates:
        MCP tokens...); `after(request, response)` runs at the end."""
        auth = self
        open_prefixes = tuple(open_prefixes) + ("/api/auth/",)

        async def guard(request: Request, call_next):
            path = request.url.path
            if not await run_in_threadpool(db.available):  # the auth database is down: the shell and the open paths only, with a clear answer
                request.state.user, request.state.via_proxy = None, False
                if path in open_paths or (is_open and is_open(path)) or (path.startswith(open_prefixes) and not path.startswith("/api/auth/")):
                    return await call_next(request)
                return JSONResponse({"detail": f"The account database cannot be reached ({db.error}); sign-in is unavailable until it is back."}, status_code=503)
            await run_in_threadpool(auth._note, request)
            refused = await auth.resolve(request)
            if refused is not None:
                return refused
            path = request.url.path
            if before:
                r = before(request)
                if hasattr(r, "__await__"):
                    r = await r
                if r is not None:
                    return r
            if request.state.user is None and await run_in_threadpool(users.require_login, auth.app):
                if path not in open_paths and not path.startswith(open_prefixes) and not (is_open and is_open(path)):
                    if getattr(request.state, "no_role_user", None):
                        return auth.no_access(request, request.state.no_role_user["name"])
                    return JSONResponse({"detail": f"Sign in to use {auth.label}."}, status_code=401,
                                        headers={"WWW-Authenticate": f'Basic realm="{auth.realm}"'} if "authorization" in request.headers else {})
            if path.startswith(api_prefix) and auth.cross_site(request):
                return JSONResponse({"detail": "Requests from another site are refused"}, status_code=403)
            response = await call_next(request)
            auth.finish(request, response)
            if after:
                r = after(request, response)
                if hasattr(r, "__await__"):
                    await r
            return response
        return guard

    def no_access(self, request: Request, ident: str):
        msg = f"{ident} has no account on {self.label}. Ask its owner for access."
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": msg, "noAccess": True}, status_code=403)
        from html import escape
        out = "/cdn-cgi/access/logout" if self.proxy.external(request.headers, peer_of(request)) else None
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>No access</title><style>body{{font:16px system-ui,sans-serif;max-width:520px;margin:15vh auto;padding:0 16px;color:#222;background:#fff}}
@media (prefers-color-scheme:dark){{body{{color:#eee;background:#111}}a{{color:#8ab4f8}}}}</style>
<h1>No access</h1><p>{escape(msg)}</p>{f'<p><a href="{out}">Sign in with another account</a></p>' if out else ''}""", status_code=403)

    # ---- sign-in helpers
    def sign_in(self, response, request: Request, user: dict, via: str):
        tok = sessions.create(user["id"], self.app, via, getattr(request.state, "origin", None) or sessions.describe(request, self.proxy))
        sessions.set_cookie(response, tok, sessions.host_of(request), via, sessions.over_https(request))

    def signed_in_by(self, request: Request, u):
        if not u:
            return None
        if getattr(request.state, "via_proxy", False):
            return "cloudflare" if self.proxy.mode() == "cloudflare" else "proxy"
        via = request.cookies.get(sessions.VIA_COOKIE)
        return via if via in ("cloudflare", "proxy", "password", "oneclick") else ("oneclick" if u.get("passwordless") else "password")

    def me(self, request: Request) -> dict:
        u = self.current_user(request)
        return {"user": u, "settings": users.get_settings(u["id"], self.app) if u else {}, "usersExist": users.any_users(),
                "isAdmin": users.is_admin(u), "isSuiteAdmin": users.is_suite_admin(u), "canWrite": users.can_write(u),
                "signedIn": users.is_signed_in(u), "requireLogin": users.require_login(self.app),
                "viaProxy": bool(getattr(request.state, "via_proxy", False)), "signedInBy": self.signed_in_by(request, u),
                "throughCloudflare": self.proxy.external(request.headers, peer_of(request)), "app": self.app}

    # ---- the routers
    def router(self) -> APIRouter:
        auth, access, proxy, app = self, self.access, self.proxy, self.app
        r = APIRouter()

        @r.post("/api/auth/login", dependencies=[access.public])
        async def api_login(request: Request):
            body = await json_body(request)
            name, src = str(body.get("name", ""))[:64], proxy.source(request)
            visitor = proxy.visitor_of(request)
            key, nkey, akey = f"{visitor}|{name.strip().lower()}", f"name|{name.strip().lower()}", f"addr|{visitor}"
            wait = max(users.login_wait(key), users.login_wait(nkey), users.login_wait(akey))
            if wait:
                authlog.warning("sign-in as %r from %s refused: %d s to wait after repeated failures", name, src, wait)
                raise HTTPException(429, f"Too many attempts; try again in {wait} seconds", headers={"Retry-After": str(wait)})
            u = await run_in_threadpool(users.authenticate, name, str(body.get("password", ""))[:1024], app)
            if not u:
                wait = max(users.login_failed(key), users.login_failed(nkey, users.LOCK_AFTER_NAME), users.login_failed(akey, users.LOCK_AFTER_NAME * 4))
                authlog.warning("failed sign-in as %r from %s (wrong user name or password%s)", name, src, f"; {wait} s wait" if wait else "")
                raise HTTPException(401, "Wrong user name or password")
            users.login_succeeded(key); users.login_succeeded(nkey); users.login_succeeded(akey)
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
            current = str(body.get("current", body.get("old", "")))[:1024]
            if not await run_in_threadpool(users.authenticate, u["name"], current):
                users.login_failed(pkey)
                authlog.warning("password change for %s refused from %s (wrong current password)", u["name"], proxy.source(request))
                raise HTTPException(403, "The current password is wrong")
            users.login_succeeded(pkey)
            try:
                users.update(u["id"], password=str(body.get("password", body.get("new", ""))))
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
            return {"items": apppasswords.list_for(auth.current_user(request)["id"], app)}

        @r.post("/api/auth/app-passwords", dependencies=[access.signed_in])
        async def api_new_app_password(request: Request):
            u = auth.current_user(request)
            if u.get("passwordless"):
                raise HTTPException(403, "A one-click account cannot have app passwords")
            body = await json_body(request)
            pw = await run_in_threadpool(apppasswords.create, u["id"], app, str(body.get("label") or "Device").strip())
            return {"password": pw, "name": u["name"]}

        @r.delete("/api/auth/app-passwords/{pid}", dependencies=[access.signed_in])
        def api_del_app_password(pid: int, request: Request):
            apppasswords.delete(auth.current_user(request)["id"], pid)
            return {"ok": True}

        @r.get("/api/auth/proxy", dependencies=[access.admin])
        def api_proxy_status():
            return proxy.status()

        @r.post("/api/auth/proxy", dependencies=[access.admin])
        async def api_proxy_save(request: Request):
            """{AUTH_PROXY, ..., suiteWide: true} : suite-wide by default (one Cloudflare Access application for every app); false: this app only."""
            body = await json_body(request)
            proxy.save(body, suite_wide=bool(body.get("suiteWide", True)))
            return proxy.status()

        @r.get("/api/auth/suite", dependencies=[access.signed_in])
        def api_suite(request: Request):
            """The suite's installed apps and where they are, for the start page and app switchers."""
            if not users.is_signed_in(auth.current_user(request)):
                raise HTTPException(401, "Sign in first")
            return {"items": suite.apps(), "app": app}

        @r.delete("/api/auth/suite/{key}", dependencies=[access.suite_admin])
        def api_suite_forget(key: str, request: Request):
            """Forget an app that was uninstalled (suite admins); it registers again if it starts."""
            if not users.is_suite_admin(auth.current_user(request)):
                raise HTTPException(403, "Suite admins only")
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

        @r.post("/api/users/{user_id}", dependencies=[access.admin])
        @r.patch("/api/users/{user_id}", dependencies=[access.admin])
        async def api_users_update(user_id: int, request: Request):
            body = await json_body(request)
            me = auth.current_user(request)
            if not users.get(user_id):
                raise HTTPException(404, "No such user")
            try:
                if body.get("delete"):
                    auth._delete(me, user_id)
                else:
                    auth._update(me, user_id, body)
            except ValueError as e:
                raise HTTPException(400, str(e))
            return {"ok": True, "user": auth.decorate(users.get(user_id, app)) if not body.get("delete") else None}

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

    def _delete(self, me, user_id: int):
        if me and me["id"] == user_id:
            raise ValueError("You cannot delete your own account while signed in")
        target = users.get(user_id)
        if target["suiteAdmin"] and not users.is_suite_admin(me):
            raise ValueError("Only a suite admin can remove a suite admin")
        users.delete(user_id)

    def _update(self, me, user_id: int, body: dict):
        target = users.get(user_id, self.app)
        suite = users.is_suite_admin(me)
        if target["suiteAdmin"] and not suite and (body.get("role") is not None or body.get("password") is not None):
            raise ValueError("Only a suite admin can change a suite admin's role or password")
        role = body.get("role")
        if role is not None and me and me["id"] == user_id and role != "admin" and users.admins_of(self.app) <= 1:
            raise ValueError("That is the only admin; make someone else admin first")
        users.update(user_id, role=role, app=self.app, password=body.get("password"), name=body.get("name"), email=body.get("email"),
                     display_name=body.get("displayName"), listed=None if body.get("listed") is None else bool(body.get("listed")),
                     suite_admin=(None if body.get("suiteAdmin") is None or not suite else bool(body.get("suiteAdmin"))))
        if suite and isinstance(body.get("roles"), dict):
            for a, rr in body["roles"].items():
                users.set_role(user_id, str(a)[:32], str(rr))
        if self.on_update:
            self.on_update(user_id, body)
        apppasswords.forget()
        from iothing_auth import proxyauth
        proxyauth.forget_identities()
