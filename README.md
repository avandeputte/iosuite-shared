# iosuite-shared

The suite's shared library: the Python package `iosuite-shared` (module `iosuite_shared`).

The Shared ioSuite database for the ioThing apps (ioPix, ioNotes, ioContacts): one account, one password, one session for
the whole suite. Signed in at `photo.example.com`, a person is signed in at `notes.example.com` too.

## What is shared, what is per app

| Shared (one row per person)            | Per app                                                   |
|----------------------------------------|-----------------------------------------------------------|
| name, display name, email, password    | role: `none`, `viewer`, `editor` or `admin`               |
| sessions (the `iothing_session` cookie): where each began and was last seen (address, through Cloudflare or not, web or which app, User-Agent) | settings (display preferences) and other per-user data    |
| suite admin flag, sign-in list flag    | app passwords (for CardDAV, LDAP, MCP clients…)           |
| proxy sign-in (Cloudflare Access…)     | `require_login`, `default_role` |

A **suite admin** is admin in every app and manages accounts. An app's **admin** manages that app's settings and
the accounts' roles there. An account without a role row in an app holds the app's `default_role` (`viewer` unless
changed; set it to `none` to make an app invitation-only). A **passwordless** account signs in with one click on the
LAN and is a viewer at most, everywhere.

## Install

```
pip install git+https://github.com/avandeputte/iosuite-shared.git
```

## Settings: config.env

Every app keeps its settings in `config.env` in its own folder (KEY=value lines, `#` comments; variables already in
the environment, such as a container's, win). The app reads it first, before anything else looks at a setting:

```python
from iosuite_shared import config as shared_config
shared_config.load_file(APP_DIR)            # APP_DIR/config.env into os.environ
globals().update(shared_config.standard("iocalendar", APP_DIR))   # APP_DIR, DATA_DIR, STATIC_DIR, LOG_FILE, HOST, PORT, PUBLIC_URL,
                                                                  # BASE_URL, FORWARDED_ALLOW_IPS, TLS_*_FILE, MCP_FORCED, ADMIN_USER/PASSWORD
DB = shared_config.own_db("calendar")       # the app's own database: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD (no default password)
```

`DATA_DIR` (`APP_DIR/data` by default) is where an app writes on its own; the server log is `LOG_FILE`, `DATA_DIR/<app>.log`
by default (`LOG_FILE=` with nothing after it: none).

The names every app understands the same way:

```
DB_HOST=db.example.lan  DB_PORT=3306  DB_NAME=…  DB_USER=…  DB_PASSWORD=…   # the app's own database
IOSUITE_DB_HOST=…  IOSUITE_DB_PORT=…            # the Shared ioSuite database; default: DB_HOST and DB_PORT
IOSUITE_DB_NAME=iosuite  IOSUITE_DB_USER=iosuite  IOSUITE_DB_PASSWORD=…
PUBLIC_URL=https://notes.example.com             # the address people and the other apps reach the app at
COOKIE_DOMAIN=example.com                        # the session cookie is shared by every host under it; a LAN address gets its own
HOST=0.0.0.0  PORT=8610                          # where the server listens (each app has its own port, below)
INTERNAL_URL=http://192.168.1.151:8610          # where the app is inside the network; default: the host's LAN address and PORT
ADMIN_USER=…  ADMIN_PASSWORD=…                   # the first admin, on an empty Shared ioSuite database
TZ=Europe/Brussels  LOG_FILE=…  UPDATE_CHECK=0  MCP=1
```

`COOKIE_NAME` (default `iothing_session`) and `SESSION_DAYS` (30) rarely need changing. On/off switches take
1/0, true/false, yes/no or on/off (`config.env_flag`).

Default ports, different for every app so that all of them can run on one host (`suite.PORTS`):

| ioPix | ioNotes | ioCalendar | ioSuite | ioContacts | ioHealth |
|-------|---------|------------|---------|------------|----------|
| 8600  | 8610    | 8620       | 8630    | 8640 (LDAP 3890) | 47800 |

The database is the **Shared ioSuite database**: accounts, sessions, roles and app passwords, and the suite-wide
settings and list of apps. Its default name and account are `iosuite`; `IOSUITE_DB_*` are the only variables read.

Proxy sign-in (`AUTH_PROXY`, `AUTH_PROXY_HEADER`, `AUTH_PROXY_TRUSTED`, `AUTH_PROXY_CREATE`, `CF_ACCESS_TEAM`,
`CF_ACCESS_AUD`) is kept once for the whole suite in the Shared ioSuite database and read only from there; it is changed
in the start page's Settings › Suite (`/api/suite-admin/proxy`) or any app's Settings (`POST /api/auth/proxy`).
`config.env` and the environment are never read for them. A proxy that names the user in a header (every preset but
Cloudflare) needs its address in `AUTH_PROXY_TRUSTED`: the header is believed from there only, and with several apps on
one machine "this machine" would let any local process sign in as anyone. For Cloudflare the token is verified, so the
default (127.0.0.1 and ::1, where cloudflared runs) stands.
`CF_ACCESS_AUD` takes one AUD tag or several (comma-separated): one Cloudflare Access application covering
`*.example.com` is the simple setup (one AUD), an application per host works too (one AUD each).

## In a FastAPI app

```python
from iosuite_shared.fastapi import Auth
from iosuite_shared import users

auth = Auth("iopix", read_only=lambda: appstate.read_only())
auth.decorate = lambda u: {**u, "person": users.get_value(u["id"], "iopix", "person")}   # optional extra fields

app.middleware("http")(auth.middleware(open_paths=("/", "/index.html"), open_prefixes=("/static/", "/api/auth/"), before=my_gate))
app.include_router(auth.router())         # /api/auth/login, accounts, logout, logout_all, me, settings, password, setup,
                                          #           sessions (list, DELETE one), app-passwords, proxy, suite
app.include_router(auth.users_router())   # /api/users (admins of this app), /api/users/{id}/sessions (list, DELETE all or ?session=)
app.include_router(auth.status_router(updater, extra=lambda request: {...}))   # GET /api/status (below)
app.include_router(auth.server_router(updater, log_file=LOG_FILE))   # /api/server, /api/log (below)
auth.install(app, limit_for=lambda path, ctype: 4 << 20, forwarded_allow_ips=config.FORWARDED_ALLOW_IPS)   # 400 for bad input, the headers, the body cap, the forwarded headers (below)

@router.post("/api/photos/tag", dependencies=[auth.access.editor_write])
def tag(request: Request):
    user = request.state.user          # {id, name, displayName, email, role, passwordless, suiteAdmin, ...} or None
```

`auth.access` gives `public`, `signed_in`, `editor`, `editor_write`, `admin`, `admin_write`, `suite_admin`; the
`_write` ones answer 403 while `read_only()` says so. A rule of the app's own gets a marker of its own,
`auth.access.custom("uploader", lambda user, request: ..., "Uploads need an editor")`, so the route says what it needs
instead of carrying `public` and checking inside. The marker is the check: a route does not test the role again in
its body. The JSON shapes are those of iopix's API, so its pages and the ionotes/iocontacts ones (which copied them)
keep working: `PATCH /api/users/{id}` changes an account, `DELETE /api/users/{id}` removes it;
`POST /api/auth/password {current, password}` changes the caller's own password (never removes it: that is an
admin's decision on the users route).

Wrong passwords are counted on every path: the sign-in route, HTTP Basic and bearer tokens (`Auth._authorization`)
and, through `users.attempt_wait / attempt_failed / attempt_succeeded`, an app's own server such as ioContacts' LDAP.
After `users.LOCK_AFTER` failures for a (visitor, name) pair the next attempt waits (429 with `Retry-After`, before any
hash is computed); the name alone and the address alone have higher limits. A wrong password repeated within a few
seconds (a client with a stale one) is answered from memory without a hash.

The code is in three modules: `iosuite_shared.fastapi` (`Auth`: who is asking, the middleware, `install`),
`iosuite_shared.access` (the markers, the JSON body, the body cap) and `iosuite_shared.routers` (the routers);
`from iosuite_shared.fastapi import Auth, public_user, json_body, BodyCap` keeps working.

The Shared ioSuite database is reached through a small connection pool (`db.Pool`); an app uses the same class for
its own database: `pool = db.Pool(connect)` and `with pool.tx() as con:`.

Per-user data: `users.get_settings(uid, app)`, `users.set_settings(uid, app, changes)`, and for anything else
`users.get_value / set_value(uid, app, key, value)` (JSON values).

## Suite administration

`app.include_router(auth.admin_router())` serves everything in the Shared ioSuite database to **suite admins** at
`/api/suite-admin` (module `iosuite_shared.admin`). Its screen ships here too, so every app shows the same one
(Settings › Suite, suite admins only): `GET /api/suite-admin/ui.js` is one ES module with its own styles that uses the
app's colour variables when it has them.

```js
const ui = await import(new URL('api/suite-admin/ui.js', document.baseURI).href);
ui.mount(el, { sub: 'people', toast, onSub: (s) => history.replaceState(null, '', `#/settings/suite/${s}`) });
// parts: people, roles, passwords, keys, apps, signin
```

The routes:

| Route | What |
|-------|------|
| `GET /api/suite-admin` | `{apps, users, roles, defaultRoles, me}`: every app the database knows (the registered ones, plus keys only seen in roles, settings or app passwords: `registered: false`) with `defaultRole`, `requireLogin` and counts; every account with `roles` (explicit, per app), `effective` (the role it holds in each app), `appPasswords`, `sessions`, `lastSeen` |
| `POST /api/suite-admin/users` | a new account: `{name, password ('': one-click viewer), email, displayName, suiteAdmin, listed, roles: {app: role}}` |
| `PATCH /api/suite-admin/users/{id}` | any of those; `roles: {app: null}` removes the row, so the app's default role applies |
| `DELETE /api/suite-admin/users/{id}` | the account, everywhere |
| `GET`/`DELETE /api/suite-admin/users/{id}/sessions` | its sessions; end all, or one with `?session=` |
| `GET /api/suite-admin/app-passwords?user=&app=` | every app password (never the secret) |
| `POST /api/suite-admin/app-passwords` | `{userId, app, label, scope}`: a new one for that account, shown once |
| `PATCH /api/suite-admin/app-passwords/{id}` | `{scope}`: `''` everything, `dav` the app's sync protocols only |
| `DELETE /api/suite-admin/app-passwords/{id}` | revoke one |
| `PATCH /api/suite-admin/apps/{app}` | `{defaultRole: none/viewer/editor, requireLogin}` |
| `DELETE /api/suite-admin/apps/{app}` | off the list (it registers again if it starts); `?data=1` also drops its roles, settings, app passwords and keys |
| `GET /api/suite-admin/keys?app=` | every app's keys and tokens (`iosuite_shared.credentials`), never the value: `{app ('' : the whole suite), appName, name, label, hint, set, env, envSet, updated, updatedBy}` |
| `PUT /api/suite-admin/keys/{app}/{name}` | `{value}`: replace it (`''` clears it); `_suite` for the whole suite's keys |
| `DELETE /api/suite-admin/keys/{app}/{name}` | clear it |

The usual rules hold: a one-click account is a viewer at most and has no app passwords, the last suite admin stays, and
the signed-in admin cannot delete their own account, drop their own suite admin or remove their own password here.

## The status, the error answers, the headers and the body cap

`GET /api/status` answers everyone, sign-in required or not, with the same shape in every app, so the start page, the
containers' health checks and the apps' shells read it alike:

```
{ok, app, name, version, usersExist, requireLogin, signedIn, isAdmin, viaProxy, user: {id, name, displayName, email, role, suiteAdmin, passwordless} | null,
 commit (signed in), update (admins, while the daily check is on: {version, remote, message, date, canRun, docker} | null), ...extra(request)}
```

`auth.install(app)` adds what every app's FastAPI gets the same way: a `ValueError` or malformed JSON out of a route
answers `400 {"detail": "Bad request: ..."}` (a `TypeError` is a bug and stays a 500 with its traceback;
`not_found=(MyNotFound,)` names exceptions that mean 404; `error_key="error"` adds a second key beside `detail`);
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin` on everything, `Cache-Control: no-store` on
`/api/` and `no-cache` elsewhere unless the route set its own; a body cap: a request larger than
`limit_for(path, content_type)` bytes (`body_limit`, 4 MB, by default) is refused with 413 by its Content-Length
before it is read, and as it arrives when it comes chunked or longer than it said; and the forwarded headers
(`proxyauth.Forwarded`): the address the connection really came from is kept for proxy sign-in and the throttles, and
`X-Forwarded-For` / `X-Forwarded-Proto` are believed from `forwarded_allow_ips` (the app's `FORWARDED_ALLOW_IPS`).
The apps start uvicorn with `proxy_headers=False`, so this is the one place the headers are read.

## MCP

Every app's MCP server is the same server over streamable HTTP at `/mcp`, on the app's own port, and an assistant
signs in like any other client: HTTP Basic with the user name and an app password, or a bearer app password
(Settings › Apps and devices). Every tool acts as that account. The switch is the app's `mcp_enabled` setting (off
until an admin turns it on under Settings › Server; `MCP=1` in the environment keeps it on).

```python
from iosuite_shared import mcp as shared_mcp
server = shared_mcp.make_server("iocalendar", "ioCalendar", __version__, instructions="...")
mcp_on = lambda: shared_mcp.enabled(db.get_setting, config.MCP_FORCED)
app.middleware("http")(auth.middleware(..., open_prefixes=("/mcp",), before=auth.mcp_gate(mcp_on, on_user=shared_mcp.current_user.set)))
shared_mcp.mount(app, server)                              # the /mcp routes, ahead of the app's own
async with shared_mcp.running(server): yield               # in the lifespan

@server.tool(annotations=shared_mcp.READ_ONLY)             # READ_ONLY, WRITES, ADDS, DELETES, UPDATES
def overview():
    u = shared_mcp.who()                                   # or need_writer(), need_admin(); clamp(n, lo, hi) for page sizes
```

`auth.mcp_gate` answers 404 while the switch is off ("The MCP server is switched off (Settings › Server).") and 401 when
nobody is signed in; `shared_mcp.set_enabled(db.set_setting, on, forced)` saves the switch from `POST /api/server`.

## Updates, restart and the server log

Every app updates itself the same way, from `iosuite_shared.updates`: the installed commit, a daily check of GitHub
for a newer version, the app's own `update.sh`, a restart, and the tail of the server log.
`Auth.server_router` serves it all at the same addresses in every app (admins only), so the Maintenance ›
Updates and logs page is the same page everywhere.

```python
from iosuite_shared.updates import Updater

updater = Updater("iocalendar", version=__version__, root=ROOT)   # repo avandeputte/iocalendar, branch main, update.sh in ROOT
updater.start_daily()                                             # checks once a day while the setting is on
app.include_router(auth.server_router(updater, log_file=LOG_FILE,
    info=lambda: {"mcpEnabled": mcp_enabled()},                   # extra fields of GET /api/server
    apply=save_switches))                                         # the app's own switches in the POST body (raise ValueError to refuse)
```

| Route | What |
|---|---|
| `GET /api/server` | `{version, installed, docker, canRun, repo, branch, check, progress, lastUpdate, autoCheck, autoCheckLocked}` + `info()` |
| `POST /api/server {action: check}` | asks GitHub; the answer is the check `{status: current / update / error, remoteVersion, ...}` |
| `POST /api/server {action: update, ref?}` | starts `update.sh` (409 when it cannot run here: Docker, no .venv, not writable) |
| `POST /api/server {action: restart}` | ends the process; systemd, launchd or Docker starts it again |
| `POST /api/server {autoCheck: bool, ...}` | the daily-check switch; other keys go to `apply(body)` |
| `GET /api/log?lines&level&contains&requests` | the tail of `log_file` (`level`: any, warning, error, auth) |
| `GET /api/log/download` | the whole file |

`update.sh` gets `<APP>_RESULT`, `<APP>_REPO`, `<APP>_BRANCH` and `<APP>_REF` in its environment (`IOCALENDAR_RESULT`…)
and writes `ok: …` or `error: …` to the result file before the restart. `<APP>_REPO` and `<APP>_BRANCH` in the app's
environment change what is followed; `UPDATE_CHECK=0` locks the daily check off.
`updater.available()` gives the banner what it needs after a check found a newer version.

Every app's `update.sh` is the same script, rendered from `templates/update.sh`; only the block "the app" differs
(name, label, repository, the `server` subfolder for ioHealth, the files an installed copy gets, optional package
groups). Render it from a checkout of this repository:

    python3 templates/render-update.py <app dir> iopix ioPix avandeputte/iopix "" "app static pyproject.toml ..." [groups-file] [after-file]

A groups file holds one `  has <group> <module>` line per optional extra (`  has ocr rapidocr`); the after file is shell
that runs once the new code is in place. Change the template here, then render it into every app.

`updates.setup_log(LOG_FILE)` sets logging up the same way in every app: INFO to stderr, and a rotating copy of
everything (5 MB, 3 old files, uvicorn's lines included) in `LOG_FILE`, which `tail()` and `GET /api/log` read. The daily-check switch is a setting of the app in the Shared ioSuite
database; pass `settings=(get, set)` to keep it elsewhere.

## The suite's apps

Each app installs on its own, so the Shared ioSuite database also keeps the list of the suite's apps (table `ios_apps`), which
every app writes itself: no address to type into the start page or into another app.

```python
auth = Auth("iocalendar", app_label="ioCalendar", version=__version__, public_url=config.PUBLIC_URL,
            description="Calendar", color="#963522")
```

Every app serves its web app's files under `/static/` and its mark at `/static/icons/mark.svg` (`suite.ICON`,
`static/icons/mark.svg` in the repo), so the icon is not something an app passes.

At its first request (`auth.resolve()`, which `auth.middleware()` calls, and so does an app's own middleware) the app registers (name, version, description, colour, icon, and its `PUBLIC_URL` when it has
one), and the middleware keeps the best address it is reached at: a host under `COOKIE_DOMAIN` over HTTPS beats
a LAN name, which beats an IP address; loopback and test hosts are never kept. `url` is the configured address, else
that one. `register=False` keeps an app out.

Each app also records where it is inside the network (`internal_url`, `internalUrl`): its `INTERNAL_URL`, else
`suite.local_url()`, made from the host's LAN address and `PORT` (`HOST` when it names an address; none when the
server listens on loopback only, or in a Docker container, where `INTERNAL_URL` has to say it). It is written at every
start, for reference only: nothing links to it, and `url` stays as above.

```python
from iosuite_shared import suite
suite.apps()            # [{app, name, description, version, color, icon, url, configuredUrl, seenUrl, internalUrl, lastSeen, ...}]
suite.url_of("iopix")   # "https://photo.example.com" or None
```

Over HTTP: `GET /api/auth/suite` (signed in) lists them; `DELETE /api/auth/suite/{app}` (suite admins) forgets an
uninstalled one (it comes back if it starts again).

## Tests

`iosuite_shared.testing` gives every app's suite its MariaDB (`server()`, `fresh_database(name)`, `auth_env(name)`:
Docker, or `IOSUITE_TEST_DB=host:port:user:password`), `clean_shared_tables(auth)` to empty the shared tables between
tests, and two checks every app runs: `check_conventions(root, app, version, config)` (the suite's port, one version,
`config.env`) and `check_access_declared(app, access.declared)` (every API route carries an access marker).

## Database naming convention

Every database in the suite follows the same rules, so the apps read alike and can all share one database:

| What | Rule |
|---|---|
| Tables | `<prefix>_<plural noun>`, snake_case. Prefixes: `ios_` (this library), `prt_` (ioSuite start page), `nts_` (ioNotes), `pix_` (ioPix), `cal_` (ioCalendar), `con_` (ioContacts), `hlt_` (ioHealth) |
| Child tables | `<prefix>_<parent singular>_<children>`: `con_contact_emails`, `pix_album_photos` |
| Keys | `id` (a natural key the devices sync by keeps its name: `uuid`); references `<table singular>_id` |
| Accounts | `user_id` (the row belongs to an account), `owner_id` (NULL: shared with everyone), `created_by` / `decided_by` (who did it) |
| Times | `<event>_at`: `created_at`, `updated_at`, `last_seen_at`; dates without a time `date` / `<event>_date` |
| Booleans | a bare adjective or `has_<thing>` (`deleted`, `shared`, `has_image`), TINYINT NOT NULL DEFAULT 0 |
| Order | `position` |
| Key/value tables | columns `name`, `value` (never `key`, a reserved word) |
| Per app | `<prefix>_settings (name, value, updated_at)` and `<prefix>_schema_version (version, applied_at)` |
| Indexes | `idx_<table>_<what>`, unique `uq_<table>_<what>` |
| Tables | InnoDB, utf8mb4, utf8mb4_unicode_ci |

Names a standard already gives are kept where a column holds that standard's field: iCalendar (`dtstart`, `rrule`),
vCard (`fn`), HealthKit (`start_date`), digiKam (ioPix's copy of the library).

`iosuite_shared.schema` has what the apps need for it: `tables` / `columns` / `indexes` say what a database has, and
`ensure_version_table` / `current_version` / `record_version` keep `<prefix>_schema_version`.

## App password scopes

Every app password has a **scope** (`ios_app_passwords.scope`): `''` (the default) reaches everything the account may;
`'dav'` reaches the app's sync protocols only, so a password lifted from a phone cannot open the REST API, MCP or the
web app. `Auth.scope_paths` says which path prefixes each scope may reach (by default `dav`: `/dav`,
`/.well-known/caldav`, `/.well-known/carddav`); the middleware answers 403 anywhere else. An app without DAV sets
`auth.scope_paths = {}`. A server outside FastAPI (ioContacts' LDAP) calls `apppasswords.authenticate_scoped(name,
password, app)`, which returns `(user, scope)`, and checks the scope itself.

- `apppasswords.create(user_id, app, label, scope="")`, `list_for(...)` items carry `scope`
- `POST /api/auth/app-passwords {label, scope}`; `GET` lists them with `scope`, and `scopes` (what the app offers)
- `apppasswords.set_scope(app, prefix, scope)`: for an app moving a record of its own into the column

`'read'` (`apppasswords.READ`) may only read: GET and HEAD, in every app the same, anywhere but `/mcp`, `/dav` and
`/.well-known/` (no writes, no MCP, no sync). It is what the apps' own service accounts get (see App links below).

## Keys and tokens

Every credential an app keeps for something outside the suite (the start page's Home Assistant token and AccuWeather
key, ioPix's Google Maps keys) lives in `ios_credentials`, one row per key the app declares, so Settings › Suite ›
Keys and tokens shows and changes them all. Values never leave the server: the panel says whether one is set.

```python
from iosuite_shared import credentials
credentials.declare("iosuite", [{"name": "ha_token", "label": "Home Assistant token", "hint": "...", "env": "HA_TOKEN"}])  # at start
token = os.environ.get("HA_TOKEN") or credentials.get("iosuite", "ha_token")
credentials.put("iosuite", "ha_token", value, by=user["name"])  # '' clears it
```

An `env` variable that is set still wins inside the app; the panel shows that (`envSet`).

## App links

One app reading from another needs no setup (`iosuite_shared.links`): the other app is found in the suite's list
(called on localhost when it runs on the same machine, then at its internal address when it recorded one, then at
its public one, each tried when the one before does not answer;
`links.url()` is the public one, for links people click), and the reader signs in as its own **service account**
(`users.service_account(app)`: `<app>-app`, on no sign-in screen, no password anyone knows, viewer in the other app)
with an app password of the other app with scope `read`. Both are made on first use; the password is kept in
`ios_credentials` (hidden from the keys list), and made again when the other app answers 401 (revoked on the panel).

```python
from iosuite_shared import links
if links.available("iocontacts"):
    data = links.json("iocalendar", "iocontacts", "/api/contacts?q=ann")   # or links.get(...): the urllib response
links.url("iocontacts")                                                    # for links people click
```

Behind Cloudflare Access, the whole suite's keys `cf_access_client_id` / `cf_access_client_secret` (a Cloudflare
Access service token, set on Settings › Suite › Keys and tokens) go with every request. Service accounts are not in
`users.list_users()` (pass `services=True`), do not count for the first-run setup, and cannot sign in with a password.

## Edge check of app passwords (Cloudflare Worker, optional)

Behind Cloudflare, a Worker on `/dav*` can turn a wrong app password away before it reaches the server. The apps keep
a Workers KV namespace up to date with a SHA-256 of each app password (never an account password), when it is made,
on its first use after each start, and when it or its account is deleted. It is off unless all three are set. They
are the whole suite's keys in the Shared ioSuite database (Settings › Suite › Keys and tokens), so every app picks
them up within seconds, without a restart; a variable in an app's config.env wins:

```
iosuite-shared edge --account-id … --namespace-id … --token-file token.txt   # token: Account › Workers KV Storage › Edit
CF_KV_ACCOUNT_ID=…  CF_KV_NAMESPACE_ID=…  CF_KV_API_TOKEN=…                  # or per app, in config.env
```

The Worker, its `wrangler.toml`, the rollout (monitor, then enforce) and a WAF rate-limit rule are in
[cloudflare/edge-auth/](cloudflare/edge-auth/README.md).

## Command line

```
iosuite-shared init                          # the tables
iosuite-shared check                         # reach the database, count the accounts
ADMIN_PASSWORD=… iosuite-shared admin alex --app iocalendar   # an admin of the app (made when missing; the first account is a suite admin)
iosuite-shared --config /opt/iocalendar/app check             # the app's config.env first (what setup.sh does)
iosuite-shared list
iosuite-shared create alex --email a@b.c --suite-admin
iosuite-shared role alex ionotes editor
iosuite-shared password alex
iosuite-shared edge [--apply]                # the edge check's KV namespace vs the app passwords (below)
```

### Several apps in one machine or container: `install`

```
ADMIN_PASSWORD=… iosuite-shared --config /opt/iosuite/app install iocalendar iocontacts --admin alex --service --from iosuite
iosuite-shared --config /opt/iopix/app install all --no-service
```

Every app's `setup.sh` ends with "Also install other apps of the suite here?" (flag `--also iopix,ionotes`; `--also all`;
the `IO<APP>_ALSO` environment variable in the Proxmox installers). The answer comes here, so the questions every app
shares are asked once: `install` reads the calling app's config.env and hands each companion the same **app database
server** (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`; each app gets its own database name, `calendar`, `contacts`,
`notes`, `health`, `iopix`, `suite`, created when the user may, so the user needs rights on those names too), the same
**Shared ioSuite database** (`IOSUITE_DB_*`), the same sign-in domain (`COOKIE_DOMAIN`) and, with `--admin`, the same
admin account. Each companion is fetched from GitHub into `<base>/<app>/app` (`/opt/iocalendar/app` beside
`/opt/iosuite/app`, with its commit in `COMMIT`, the layout the Proxmox installers make), and its own `setup.sh --yes`
installs the packages, writes config.env, creates the tables and the systemd service. The passwords travel in the
environment (`IOCALENDAR_DB`, `IOCALENDAR_SUITE_DB`, `IOCALENDAR_ADMIN`, ...), never on a command line. What differs
per app keeps its default: the usual port, no public address, the machine's time zone; ioPix gets `--no-digikam`,
`--features none` and the photos folder of `--photos` (default `/library/photos`). An app already set up in its folder
is left alone. Afterwards each app updates itself as always (its `update.sh`, or Maintenance › Updates in the web app).

### A MariaDB administrator makes the databases and users: `provision`

```
DB_ADMIN_PASSWORD=… iosuite-shared --config /opt/iosuite/app provision --admin root [--also iocalendar,ionotes] [--dry-run]
DB_ADMIN_PASSWORD=… iosuite-shared --config /opt/iosuite/app install iocalendar --db-admin root ...
```

Every app's `setup.sh` asks, right after the database questions, for a MariaDB / MySQL administrator (flag
`--db-admin USER:PASSWORD`, or `IO<APP>_DB_ADMIN`; empty: the databases and users exist already). With one, `provision`
creates what config.env names: the app's own database and user (`DB_*`), the Shared ioSuite database and user
(`IOSUITE_DB_*`) and, with `--also`, the companions' databases for the same app user. Nothing is dropped: databases and
users are created when missing, the users' passwords are set to the ones answered, and each user gets all privileges
on its own databases only; users are made for `'%'` (and `'localhost'` when the server is this machine). The
administrator's password travels in `DB_ADMIN_PASSWORD`, never on a command line, and is not written anywhere.

## Tests

`pytest tests/test_updates.py` runs without a database. The rest of `pytest` needs a MariaDB of its own: a Docker container it starts itself (`iosuite-test-mariadb`, kept for the next
run), or the server named by `IOSUITE_TEST_DB=host:port:user:password` (a user that may create and drop databases).
It uses a database `iosuite_shared_test` there, dropped and made again per test, and never a live Shared ioSuite database;
without a test server it is skipped. The apps' suites use the same helper (`iosuite_shared.testing`).
