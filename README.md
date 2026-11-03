# iothing-auth

One user database for the ioThing apps (ioPix, ioNotes, ioContacts): one account, one password, one session for
the whole suite. Signed in at `photo.iothing.net`, a person is signed in at `notes.iothing.net` too.

## What is shared, what is per app

| Shared (one row per person)            | Per app                                                   |
|----------------------------------------|-----------------------------------------------------------|
| name, display name, email, password    | role: `none`, `viewer`, `editor` or `admin`               |
| sessions (the `iothing_session` cookie)| settings (display preferences) and other per-user data    |
| suite admin flag, sign-in list flag    | app passwords (for CardDAV, LDAP, MCP clients…)           |
| proxy sign-in (Cloudflare Access…)     | optional overrides of the proxy settings, `require_login`, `default_role` |

A **suite admin** is admin in every app and manages accounts. An app's **admin** manages that app's settings and
the accounts' roles there. An account without a role row in an app holds the app's `default_role` (`viewer` unless
changed; set it to `none` to make an app invitation-only). A **passwordless** account signs in with one click on the
LAN and is a viewer at most, everywhere.

## Install

```
pip install git+ssh://git@github.com/avandeputte/iothing-auth.git
```

Environment (the apps' `.env` files):

```
IOTHING_AUTH_DB_HOST=192.168.1.89   IOTHING_AUTH_DB_PORT=3306
IOTHING_AUTH_DB_NAME=auth           IOTHING_AUTH_DB_USER=auth     IOTHING_AUTH_DB_PASSWORD=…
IOTHING_COOKIE_DOMAIN=iothing.net   # the session cookie is shared by every host under it; a LAN address gets its own
```

Proxy sign-in (`AUTH_PROXY`, `AUTH_PROXY_HEADER`, `AUTH_PROXY_TRUSTED`, `AUTH_PROXY_CREATE`, `CF_ACCESS_TEAM`,
`CF_ACCESS_AUD`) can come from the environment of each app (wins) or be saved once, suite-wide, from any app's
Settings (`POST /api/auth/proxy`). One Cloudflare Access application covering `*.iothing.net` is the simple setup:
one AUD, one sign-in for every app.

## In a FastAPI app

```python
from iothing_auth.fastapi import Auth
from iothing_auth import users

auth = Auth("iopix", read_only=lambda: appstate.read_only())
auth.decorate = lambda u: {**u, "person": users.get_value(u["id"], "iopix", "person")}   # optional extra fields

app.middleware("http")(auth.middleware(open_paths=("/", "/index.html"), open_prefixes=("/static/", "/api/auth/"), before=my_gate))
app.include_router(auth.router())         # /api/auth/login, accounts, logout, logout_all, me, settings, password, setup,
                                          #           sessions, app-passwords, proxy
app.include_router(auth.users_router())   # /api/users (admins of this app)

@router.post("/api/photos/tag", dependencies=[auth.access.editor_write])
def tag(request: Request):
    user = request.state.user          # {id, name, displayName, email, role, passwordless, suiteAdmin, ...} or None
```

`auth.access` gives `public`, `signed_in`, `editor`, `editor_write`, `admin`, `admin_write`, `suite_admin`; the
`_write` ones answer 403 while `read_only()` says so. The JSON shapes are those of iopix's API, so its pages and
the ionotes/iocontacts ones (which copied them) keep working.

Per-user data: `users.get_settings(uid, app)`, `users.set_settings(uid, app, changes)`, and for anything else
`users.get_value / set_value(uid, app, key, value)` (JSON values).

## Command line

```
iothing-auth init                          # the tables
iothing-auth list
iothing-auth create alex --email a@b.c --suite-admin
iothing-auth role alex ionotes editor
iothing-auth password alex
iothing-auth migrate --iopix sqlite:///…/viewer.db --ionotes mysql://notes:pw@host/notes --iocontacts mysql://… [--apply]
```

`migrate` reads each app's own user table and folds them into one: matched by email, then name; the first source
wins a password conflict (iopix, then ionotes, then ioContacts); an admin everywhere becomes a suite admin. Without
`--apply` it only prints the plan. With it, the apps' tables that point at user ids are rewritten to the shared ids
and the old user tables are renamed `*_migrated`, not dropped.

## Tests

`pytest` runs against a real MariaDB (`IOTHING_AUTH_DB_HOST`… ; it uses a database of its own, `auth_test`, and
skips without a server).
