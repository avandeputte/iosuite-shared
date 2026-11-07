"""Who may call a route, and the pieces of the request handling that are the same in every app: the access
markers (Access), the JSON body reader, the public shape of an account, and the body cap middleware (BodyCap)."""
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from iosuite_shared import users

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

    def custom(self, name: str, allowed, message: str = "Not allowed", write: bool = False):
        """A marker of the app's own: `allowed(user, request)` says whether the request may proceed (ioHealth's
        uploader, ioPix's downloads). Carries the name as its role, so check_access_declared counts it and a reader
        sees what the route needs instead of a `public` that lies."""
        if name in ACCESS_ROLES:
            raise ValueError(f"{name} is a built-in access level")

        async def check(request: Request):
            user = getattr(request.state, "user", None)
            if write and self.read_only and self.read_only():
                raise HTTPException(403, self.read_only_message)
            if not allowed(user, request):
                raise HTTPException(403 if user else 401, message)

        check.access_role, check.access_write = name, write
        return Depends(check)

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


