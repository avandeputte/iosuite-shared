# iosuite-shared

The suite's shared library: the Python package `iosuite-shared` (module `iosuite_shared`).

The Shared ioSuite database for the ioThing apps (ioPix, ioNotes, ioContacts): one account, one password, one session for
the whole suite. Signed in at `photo.iothing.net`, a person is signed in at `notes.iothing.net` too.

## What is shared, what is per app

| Shared (one row per person)            | Per app                                                   |
|----------------------------------------|-----------------------------------------------------------|
| name, display name, email, password    | role: `none`, `viewer`, `editor` or `admin`               |
| sessions (the `iothing_session` cookie): where each began and was last seen (address, through Cloudflare or not, web or which app, User-Agent) | settings (display preferences) and other per-user data    |
| suite admin flag, sign-in list flag    | app passwords (for CardDAV, LDAP, MCP clients…)           |
| proxy sign-in (Cloudflare Access…)     | optional overrides of the proxy settings, `require_login`, `default_role` |

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
```

The names every app understands the same way:

```
DB_HOST=db.example.lan  DB_PORT=3306  DB_NAME=…  DB_USER=…  DB_PASSWORD=…   # the app's own database
IOSUITE_DB_HOST=…  IOSUITE_DB_PORT=…            # the Shared ioSuite database; default: DB_HOST and DB_PORT
IOSUITE_DB_NAME=iosuite  IOSUITE_DB_USER=iosuite  IOSUITE_DB_PASSWORD=…
PUBLIC_URL=https://notes.example.com             # the address people and the other apps reach the app at
COOKIE_DOMAIN=example.com                        # the session cookie is shared by every host under it; a LAN address gets its own
HOST=0.0.0.0  PORT=8610                          # where the server listens (each app has its own port, below)
ADMIN_USER=…  ADMIN_PASSWORD=…                   # the first admin, on an empty Shared ioSuite database
TZ=Europe/Brussels  LOG_FILE=…  UPDATE_CHECK=0  MCP=1  MCP_ADMIN_TOKEN=…
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
`CF_ACCESS_AUD`) can come from the environment of each app (wins) or be saved once, suite-wide, from any app's
Settings (`POST /api/auth/proxy`). One Cloudflare Access application covering `*.iothing.net` is the simple setup:
one AUD, one sign-in for every app.

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
app.include_router(auth.server_router(updater, log_file=LOG_FILE))   # /api/server, /api/log (below)

@router.post("/api/photos/tag", dependencies=[auth.access.editor_write])
def tag(request: Request):
    user = request.state.user          # {id, name, displayName, email, role, passwordless, suiteAdmin, ...} or None
```

`auth.access` gives `public`, `signed_in`, `editor`, `editor_write`, `admin`, `admin_write`, `suite_admin`; the
`_write` ones answer 403 while `read_only()` says so. The JSON shapes are those of iopix's API, so its pages and
the ionotes/iocontacts ones (which copied them) keep working.

Per-user data: `users.get_settings(uid, app)`, `users.set_settings(uid, app, changes)`, and for anything else
`users.get_value / set_value(uid, app, key, value)` (JSON values).

## Updates, restart and the server log

Every app updates itself the same way, from `iosuite_shared.updates`: the installed commit, a daily check of GitHub
for a newer version, the app's own `update.sh`, a restart, the MCP admin token, and the tail of the server log.
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
| `POST /api/server {action: token / regenerate_token}` | the MCP admin token |
| `POST /api/server {action: restart}` | ends the process; systemd, launchd or Docker starts it again |
| `POST /api/server {autoCheck: bool, ...}` | the daily-check switch; other keys go to `apply(body)` |
| `GET /api/log?lines&level&contains&requests` | the tail of `log_file` (`level`: any, warning, error, auth) |
| `GET /api/log/download` | the whole file |

`update.sh` gets `<APP>_RESULT`, `<APP>_REPO`, `<APP>_BRANCH` and `<APP>_REF` in its environment (`IOCALENDAR_RESULT`…)
and writes `ok: …` or `error: …` to the result file before the restart. `<APP>_REPO` and `<APP>_BRANCH` in the app's
environment change what is followed; `UPDATE_CHECK=0` locks the daily check off; `MCP_ADMIN_TOKEN` fixes the token.
`updater.available()` gives the banner what it needs after a check found a newer version; `updater.token_ok(given)`
checks a token an MCP client sent. The daily-check switch and the token are settings of the app in the Shared ioSuite
database; pass `settings=(get, set)` to keep them elsewhere.

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

```python
from iosuite_shared import suite
suite.apps()            # [{app, name, description, version, color, icon, url, configuredUrl, seenUrl, lastSeen, ...}]
suite.url_of("iopix")   # "https://photo.iothing.net" or None
```

Over HTTP: `GET /api/auth/suite` (signed in) lists them; `DELETE /api/auth/suite/{app}` (suite admins) forgets an
uninstalled one (it comes back if it starts again).

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

`iosuite_shared.schema` has what the apps need for it: `rename(cur, tables, columns, indexes)` renames what still has
an old name (RENAME TABLE / COLUMN / INDEX: instant, every row kept; nothing on a database already up to date), and
`ensure_version_table` / `current_version` / `record_version` keep `<prefix>_schema_version`. 0.8.0 renamed this
library's own tables (`users` -> `ios_users`, `suite_apps` -> `ios_apps`, `k`/`v` -> `name`/`value`, `created` ->
`created_at`...) at the first start; every app on a server has to run a version built on 0.8.0 from then on.

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
iosuite-shared migrate --iopix sqlite:///…/viewer.db --ionotes mysql://notes:pw@host/notes --iocontacts mysql://… [--apply]
```

`migrate` reads each app's own user table and folds them into one: matched by email, then name; the first source
wins a password conflict (iopix, then ionotes, then ioContacts); an admin everywhere becomes a suite admin. Without
`--apply` it only prints the plan. With it, the apps' tables that point at user ids are rewritten to the shared ids
and the old user tables are renamed `*_migrated`, not dropped.

## Tests

`pytest tests/test_updates.py` runs without a database. The rest of `pytest` needs a MariaDB of its own: a Docker container it starts itself (`iosuite-test-mariadb`, kept for the next
run), or the server named by `IOSUITE_TEST_DB=host:port:user:password` (a user that may create and drop databases).
It uses a database `iosuite_shared_test` there, dropped and made again per test, and never a live Shared ioSuite database;
without a test server it is skipped. The apps' suites use the same helper (`iosuite_shared.testing`).
