"""The FastAPI glue: one Auth object per app gives the middleware that works out who is asking, the route
dependencies that say who may call a route, and the routers with the sign-in and user-management API.

    auth = Auth("iopix")
    app.middleware("http")(auth.middleware(open_paths=..., open_prefixes=..., before=..., after=...))
    @router.get("/api/x", dependencies=[auth.access.admin])
    app.include_router(auth.router())        # /api/auth/* : login, accounts, logout, logout_all, me, settings, password, setup
    app.include_router(auth.users_router())  # /api/users  : list, create, change, delete (admins of this app)
    app.include_router(auth.admin_router())  # /api/suite-admin: every account, role, app password, session and app (suite admins)
    app.include_router(auth.status_router(updater, extra=...))   # GET /api/status, public, the same shape in every app
    app.include_router(auth.server_router(updater, log_file=...)) # GET/POST /api/server, GET /api/log (admins)
    auth.install(app, limit_for=...)         # 400 for bad input, the security headers, no-store on the API, the body cap

The API shapes are those of iopix (ionotes copied them), so the apps' pages keep working.
"""
import base64
import json
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from iosuite_shared import admin as suite_admin, apppasswords, db, proxyauth, sessions, suite, users
from iosuite_shared.proxyauth import Proxy, authlog, peer_of, remember_peer, seen

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


STATUS_PATH = "/api/status"
PUBLIC_USER_FIELDS = ("id", "name", "displayName", "email", "role", "suiteAdmin", "passwordless")


def public_user(u) -> dict | None:
    """The account as /api/status shows it to its own browser: the fields every app's shell uses, nothing else."""
    if not u:
        return None
    return {k: u.get(k) for k in PUBLIC_USER_FIELDS if k in u}


def content_type(headers) -> str:
    return (headers.get("content-type") or "").split(";")[0].strip().lower()


class TooLarge(Exception):
    pass


def _has_too_large(e: BaseException) -> bool:
    if isinstance(e, TooLarge):
        return True
    return any(_has_too_large(x) for x in getattr(e, "exceptions", ()))  # inside an ExceptionGroup (task groups)


class BodyCap:
    """ASGI middleware: the request body counted as it is read and refused (413) once past `limit_for(path, content
    type)`, for a body that comes without a Content-Length (chunked) or longer than it said. The outermost layer, so
    the refusal reaches the client through every other middleware. Auth.install() adds it."""

    def __init__(self, app, limit_for, error=None):
        self.app, self.limit_for = app, limit_for
        self.error = error or (lambda msg, status: JSONResponse({"detail": msg}, status_code=status))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        limit = self.limit_for(scope["path"], content_type(headers))
        seen, started = 0, False

        async def capped():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    raise TooLarge()
            return msg

        async def sending(msg):
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, capped, sending)
        except BaseException as e:
            if not _has_too_large(e) or started:
                raise
            await self.error("That request is too large", 413)(scope, receive, send)


class Auth:
    def __init__(self, app: str, read_only=None, read_only_message: str = None, app_label: str = None, basic_realm: str = None,
                 version: str = None, public_url: str = None, description: str = None, color: str = None,
                 register: bool = True, internal_url: str = None):
        """version, public_url (the app's PUBLIC_URL; empty: the address it is reached at), description and color (the
        icon is suite.ICON, the same path in every app) and internal_url (where it is inside the network; None:
        suite.local_url(), from INTERNAL_URL or the LAN address and PORT) go into the suite's list of apps (iosuite_shared.suite) when the app first reaches the Shared ioSuite database; register=False
        keeps the app out of it."""
        self.app = app
        self.suite_entry = dict(name=app_label or app, url=public_url or None, version=version, description=description,
                                color=color, internal_url=internal_url or suite.local_url(app)) if register else None
        self._registered, self._retry_at = False, 0.0
        self._adopted, self._adopt_retry = False, 0.0
        self.label = app_label or app
        self.proxy = Proxy(app)
        self.access = Access(read_only, read_only_message or f"{self.label} is in read-only mode; nothing can be changed.")
        self.realm = basic_realm or self.label
        self.log = logging.getLogger(f"iosuite_shared.{app}")
        self.decorate = lambda user: user  # the app may add fields to the user dict it serves (iopix: the person tag)
        self.on_update = None  # on_update(user_id, body) after an admin changed an account: the app's own fields (iopix: the person tag)
        self.deny_exempt = ()  # path prefixes a denied proxy identity may still reach (iopix: /mcp, which signs in with its own token)
        # what a scoped app password may reach, by path prefix (apppasswords: 'dav', the app's sync protocols); anywhere
        # else it gets 403. An app without DAV sets it to {} so it offers no such passwords.
        self.scope_paths = {apppasswords.DAV: ("/dav", "/.well-known/caldav", "/.well-known/carddav")}
        # 'read' (apppasswords.READ, the apps' own service accounts) is the same everywhere: GET and HEAD under /api/
        remember_peer()

    # ---- the suite's list of apps
    def register(self) -> None:
        """Write this app's row in the suite's list (once per start; the middleware calls it). Never fails a request."""
        if self._registered or self.suite_entry is None or time.time() < self._retry_at:
            return
        try:
            suite.register(self.app, **self.suite_entry)
            self._registered = True
        except ValueError as e:  # a PUBLIC_URL or INTERNAL_URL that is not an address: registered without it
            self.log.warning("suite registration: %s; registering without the address", e)
            entry = dict(self.suite_entry)
            for k in ("url", "internal_url"):
                try:
                    suite.clean_url(entry[k])
                except ValueError:
                    entry[k] = None
            suite.register(self.app, **entry)
            self._registered = True
        except Exception as e:  # noqa: BLE001 - the table is the suite's convenience, never a reason to fail
            self.log.warning("could not register %s in the suite's list of apps (tried again in a minute): %s", self.app, e)
            self._retry_at = time.time() + 60

    def note(self, request: Request) -> None:
        """Register (once) and note the address this request reached the app at. resolve() calls it, so an app with
        its own middleware gets it too."""
        try:
            self.register()
            if self._registered:
                suite.note_seen(self.app, request)
        except Exception as e:  # noqa: BLE001
            self.log.debug("suite: %s", e)

    def adopt_proxy(self) -> None:
        """Once per start: move what config.env and this app's own rows hold of the proxy sign-in settings into the
        suite-wide ones (proxyauth.adopt). Never fails a request."""
        if self._adopted:
            return
        try:
            proxyauth.adopt(self.app)
            self._adopted = True
        except Exception as e:  # noqa: BLE001
            self.log.warning("could not move the proxy sign-in settings to the Shared ioSuite database (tried again in a minute): %s", e)
            self._adopt_retry = time.time() + 60

    # ---- who is asking
    def current_user(self, request: Request):
        return getattr(request.state, "user", None)

    async def resolve(self, request: Request):
        """Who is asking: the session cookie, the proxy's identity, HTTP Basic or a bearer app password.
        Sets request.state.user, .via_proxy, .cookie_uid, .proxy_ident; returns a response to send instead when the
        proxy let in someone who has no account and that is denied."""
        if self.suite_entry is not None:
            await run_in_threadpool(self.note, request)
        if not self._adopted and time.time() >= self._adopt_retry:
            await run_in_threadpool(self.adopt_proxy)
        session = request.cookies.get(sessions.COOKIE)
        request.state.origin = sessions.describe(request, self.proxy)
        user, renewed = (await run_in_threadpool(sessions.lookup, session, self.app, request.state.origin)) if session else (None, False)
        request.state.cookie_uid = user["id"] if user else None
        request.state.renew_cookie = renewed
        request.state.via_proxy = False
        request.state.proxy_ident = None
        request.state.app_password_scope = ""
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
            user, scope = apppasswords.authenticate_scoped(name, pw, self.app)
        elif h.lower().startswith("bearer "):
            user, scope = apppasswords.authenticate_scoped(None, h[7:].strip(), self.app)
        else:
            return None
        request.state.app_password_scope = scope or ""
        return user

    def scope_refusal(self, request: Request):
        """A 403 when the request signed in with a scoped app password and asks for something outside its scope."""
        scope = getattr(request.state, "app_password_scope", "")
        if not scope or not getattr(request.state, "user", None):
            return None
        if scope == apppasswords.READ:
            if request.method in ("GET", "HEAD") and request.url.path.startswith("/api/"):
                return None
            return JSONResponse({"detail": f"This app password may only read {self.label}'s API."}, status_code=403)
        if request.url.path.startswith(tuple(self.scope_paths.get(scope, ())) or ("\0",)):
            return None
        return JSONResponse({"detail": f"This app password is for {self.label}'s sync protocols only (CalDAV / CardDAV). "
                                       "Make one without that limit in Settings › Apps and devices."}, status_code=403)

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
        `is_open(path)` says, stay reachable; /api/auth/ and /api/status always are), the cross-site check on writes,
        and the cookie afterwards. `before(request)` runs once the user is known and may return a response (the app's own gates:
        MCP tokens...); `after(request, response)` runs at the end."""
        auth = self
        open_prefixes = tuple(open_prefixes) + ("/api/auth/",)
        open_paths = tuple(open_paths) + (STATUS_PATH,)  # the status answers everyone, as the sign-in routes do

        async def guard(request: Request, call_next):
            path = request.url.path
            if not await run_in_threadpool(db.available):  # the Shared ioSuite database is down: the shell and the open paths only, with a clear answer
                request.state.user, request.state.via_proxy = None, False
                if path in open_paths or (is_open and is_open(path)) or (path.startswith(open_prefixes) and not path.startswith("/api/auth/")):
                    return await call_next(request)
                return JSONResponse({"detail": f"The Shared ioSuite database cannot be reached ({db.error}); sign-in is unavailable until it is back."}, status_code=503)
            refused = await auth.resolve(request)
            if refused is None:
                refused = auth.scope_refusal(request)
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
            u = auth.current_user(request)
            if users.any_users() and not users.is_suite_admin(u):
                raise HTTPException(403, "Suite admins only")
            return u

        @r.get("", dependencies=[access.suite_admin])
        def api_overview(request: Request):
            me(request)
            return {**suite_admin.overview(), "me": (auth.current_user(request) or {}).get("id")}

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

        @r.get("/users/{user_id}/sessions", dependencies=[access.suite_admin])
        def api_sessions(user_id: int, request: Request):
            me(request)
            if not users.get(user_id):
                raise HTTPException(404, "No such user")
            return {"sessions": sessions.list_sessions(user_id, request.cookies.get(sessions.COOKIE))}

        @r.delete("/users/{user_id}/sessions", dependencies=[access.suite_admin])
        def api_end_sessions(user_id: int, request: Request):
            """Every session of the account, or one with ?session=<id>."""
            me(request)
            call(suite_admin.end_sessions, user_id, request.query_params.get("session"))
            return {"ok": True}

        @r.get("/app-passwords", dependencies=[access.suite_admin])
        def api_app_passwords(request: Request, user: int = None, app: str = None):
            me(request)
            return {"items": call(suite_admin.app_passwords, user, app)}

        @r.post("/app-passwords", dependencies=[access.suite_admin])
        async def api_new_app_password(request: Request):
            body = await json_body(request)
            me(request)
            try:
                uid = int(body.get("userId"))
            except (TypeError, ValueError):
                raise HTTPException(400, "userId is needed")
            return await run_in_threadpool(call, suite_admin.new_app_password, uid, body.get("app"), str(body.get("label") or ""),
                                           str(body.get("scope") or ""))

        @r.patch("/app-passwords/{pid}", dependencies=[access.suite_admin])
        async def api_app_password_scope(pid: int, request: Request):
            body = await json_body(request)
            me(request)
            call(suite_admin.set_app_password_scope, pid, str(body.get("scope") or ""))
            return {"ok": True}

        @r.delete("/app-passwords/{pid}", dependencies=[access.suite_admin])
        def api_revoke_app_password(pid: int, request: Request):
            me(request)
            call(suite_admin.revoke_app_password, pid)
            return {"ok": True}

        @r.get("/keys", dependencies=[access.suite_admin])
        def api_keys(request: Request, app: str = None):
            """Every key and token the apps keep (never the values)."""
            me(request)
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
            me(request)
            return call(suite_admin.set_app, key, default_role=body.get("defaultRole"),
                        require_login=None if body.get("requireLogin") is None else bool(body["requireLogin"]))

        @r.delete("/apps/{key}", dependencies=[access.suite_admin])
        def api_forget_app(key: str, request: Request, data: int = 0):
            """Take an app off the list; ?data=1 also removes its roles, settings and app passwords."""
            me(request)
            if not call(suite_admin.forget_app, key, bool(data)):
                raise HTTPException(404, "No such app")
            if key == auth.app:
                auth._registered = False
            return {"ok": True}

        @r.get("/proxy", dependencies=[access.suite_admin])
        def api_proxy(request: Request):
            """Sign-in at a reverse proxy (Cloudflare Access...) for the whole suite: the suite-wide values, any app's
            own override, values an app's config.env or environment still holds that disagree, the last token seen."""
            me(request)
            return {**proxyauth.suite_status(), "here": auth.proxy.status()}

        @r.put("/proxy", dependencies=[access.suite_admin])
        async def api_proxy_save(request: Request):
            """Body: any of AUTH_PROXY, AUTH_PROXY_HEADER, AUTH_PROXY_TRUSTED, AUTH_PROXY_CREATE, CF_ACCESS_TEAM,
            CF_ACCESS_AUD (empty clears one). CF_ACCESS_AUD: one AUD tag or several, comma-separated."""
            body = await json_body(request)
            me(request)
            call(proxyauth.save_suite, body)
            return {**proxyauth.suite_status(), "here": auth.proxy.status()}

        @r.delete("/proxy/overrides/{key}", dependencies=[access.suite_admin])
        def api_proxy_clear(key: str, request: Request):
            """Drop an app's own proxy settings so the suite-wide ones apply there."""
            me(request)
            proxyauth.clear_override(call(suite_admin.clean_app, key))
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
            return await run_in_threadpool(auth.status, request, updater, extra)

        return r

    def status(self, request: Request, updater=None, extra=None) -> dict:
        """What GET /api/status answers (also for an app that has to serve it from its own route)."""
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

    # ---- the MCP endpoint's gate
    def mcp_gate(self, enabled, path: str = "/mcp", on_user=None, settings_page: str = "Settings › Server"):
        """A `before` hook for middleware() (or something an app's own hook calls first): a request to the MCP endpoint
        is refused with 404 while the switch is off (`enabled()` is false), with 401 when nobody is signed in (MCP
        clients sign in with an app password: HTTP Basic, or a bearer token), and otherwise `on_user(user)` runs (an app
        sets its MCP context variable there). Anything else passes."""
        auth = self

        def gate(request: Request):
            p = request.url.path
            if p != path and not p.startswith(path + "/"):
                return None
            if not enabled():
                return JSONResponse({"detail": f"The MCP server is switched off ({settings_page})."}, status_code=404)
            user = auth.current_user(request)
            if not user:
                return JSONResponse({"detail": "Sign in with your user name and an app password (HTTP Basic), or send 'Authorization: Bearer "
                                               "<app password>'. Make one in Settings › Apps and devices."},
                                    status_code=401, headers={"WWW-Authenticate": f'Basic realm="{auth.realm}", Bearer realm="{auth.realm}"'})
            if on_user:
                on_user(user)
            return None
        return gate

    # ---- what every app's FastAPI gets the same way
    def install(self, app, api_prefix: str = "/api/", body_limit: int = 4 << 20, limit_for=None, error_key: str = None,
                not_found=()) -> None:
        """The pieces every app of the suite adds to its FastAPI, once:
        - a ValueError, TypeError or malformed JSON out of a route is the client's mistake: a 400 that says so, not a
          500 and a traceback; `not_found` names the app's own exception classes that mean 404;
        - the headers: X-Content-Type-Options: nosniff and Referrer-Policy: same-origin on everything, Cache-Control:
          no-store on the API (api_prefix) and no-cache elsewhere, unless the route set its own;
        - the body cap: a request body larger than `limit_for(path, content_type)` bytes (`body_limit` by default) is
          refused with 413, by its Content-Length before it is read, and as it arrives when it comes chunked or longer
          than it said, so nothing large is ever held in memory.
        `error_key` names a second key the error JSON carries beside `detail` (ioHealth's phone app reads `error`)."""
        log = self.log
        limit_for = limit_for or (lambda path, ctype: body_limit)

        def error(msg: str, status: int, headers=None):
            body = {"detail": msg}
            if error_key:
                body[error_key] = msg
            return JSONResponse(body, status_code=status, headers=headers)

        if error_key:
            @app.exception_handler(HTTPException)
            async def _http_error(request: Request, e: HTTPException):
                return error(e.detail, e.status_code, getattr(e, "headers", None))

        for exc in not_found:
            @app.exception_handler(exc)
            async def _not_found(request: Request, e: Exception):
                return error(str(e) or "Not found", 404)

        @app.exception_handler(ValueError)
        @app.exception_handler(TypeError)
        @app.exception_handler(json.JSONDecodeError)
        async def _bad_input(request: Request, e: Exception):
            log.warning("bad input on %s %s: %s: %s", request.method, request.url.path, type(e).__name__, str(e)[:200])
            return error(f"Bad request: {str(e)[:200] or type(e).__name__}", 400)

        @app.middleware("http")
        async def _headers_and_limits(request: Request, call_next):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                try:
                    size = int(request.headers.get("content-length") or 0)
                except ValueError:
                    return error("Bad Content-Length", 400)
                if size > limit_for(request.url.path, content_type(request.headers)):
                    return error("That request is too large", 413)
            response = await call_next(request)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            if "cache-control" not in response.headers:
                response.headers["Cache-Control"] = "no-store" if request.url.path.startswith(api_prefix) else "no-cache"
            return response

        app.add_middleware(BodyCap, limit_for=limit_for, error=error)  # added last: the outermost layer

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
        from iosuite_shared import proxyauth
        proxyauth.forget_identities()
