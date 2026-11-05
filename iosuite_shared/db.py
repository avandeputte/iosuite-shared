"""The Shared ioSuite database: connections, the schema, and the suite-wide / per-app settings table.

Every call opens its own connection and closes it (PyMySQL connections are not shared between threads). Rows come
back as dicts.
"""
import json
import logging
import threading
import time
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from iosuite_shared import config, schema

log = logging.getLogger("iosuite_shared.db")

SCHEMA_VERSION = 4  # 4: the suite naming convention (ios_ tables, *_at times, name/value, named indexes)
PREFIX = "ios"
OPTIONS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

# Every table is ios_<name>, so the shared tables can sit in the same database as the apps' own (each app has its
# own three-letter prefix). Times are seconds since 1970 (UTC).
SCHEMA = [
    # one account for the whole suite. hash '' is a passwordless account: one-click sign-in on the LAN, viewer at most.
    """CREATE TABLE IF NOT EXISTS ios_users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        display_name VARCHAR(128) NULL,
        email VARCHAR(191) NULL,
        hash VARCHAR(255) NOT NULL DEFAULT '',
        suite_admin TINYINT NOT NULL DEFAULT 0,
        listed TINYINT NOT NULL DEFAULT 1,
        created_at BIGINT NOT NULL,
        last_login_at BIGINT NULL,
        session_gen INT NOT NULL DEFAULT 0,
        UNIQUE KEY uq_ios_users_name (name),
        UNIQUE KEY uq_ios_users_email (email)
    )""",
    # the role an account holds in one app; no row: the app's default_role setting (viewer unless changed)
    """CREATE TABLE IF NOT EXISTS ios_user_roles (
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        role ENUM('none','viewer','editor','admin') NOT NULL,
        PRIMARY KEY (user_id, app),
        FOREIGN KEY (user_id) REFERENCES ios_users(id) ON DELETE CASCADE
    )""",
    # per-user, per-app values (display settings, the ioPix person tag, the ioContacts "me" contact), JSON-encoded
    """CREATE TABLE IF NOT EXISTS ios_user_data (
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        name VARCHAR(64) NOT NULL,
        value MEDIUMTEXT NOT NULL,
        PRIMARY KEY (user_id, app, name),
        FOREIGN KEY (user_id) REFERENCES ios_users(id) ON DELETE CASCADE
    )""",
    # a browser or app session: the cookie carries a random token, the table its hash. Shared by every app on the
    # domain. Where it began and was last seen: the visitor's address (the real one, through Cloudflare or a trusted
    # proxy), whether it came through Cloudflare, the client (web, or the app's User-Agent product) and the full User-Agent.
    """CREATE TABLE IF NOT EXISTS ios_sessions (
        token_hash CHAR(64) PRIMARY KEY,
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        via VARCHAR(16) NOT NULL,
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        renewed_at BIGINT NOT NULL,
        ip VARCHAR(64) NULL,
        cloudflare TINYINT NOT NULL DEFAULT 0,
        client VARCHAR(32) NOT NULL DEFAULT '',
        user_agent VARCHAR(255) NULL,
        last_seen_at BIGINT NULL,
        last_ip VARCHAR(64) NULL,
        INDEX idx_ios_sessions_user (user_id),
        INDEX idx_ios_sessions_expires (expires_at),
        FOREIGN KEY (user_id) REFERENCES ios_users(id) ON DELETE CASCADE
    )""",
    # app passwords: one per device, for one app (CardDAV, LDAP, MCP clients...); shown once, revocable
    """CREATE TABLE IF NOT EXISTS ios_app_passwords (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        label VARCHAR(128) NOT NULL,
        prefix CHAR(8) NOT NULL,
        hash VARCHAR(255) NOT NULL,
        created_at BIGINT NOT NULL,
        last_used_at BIGINT NULL,
        INDEX idx_ios_app_passwords_prefix (prefix),
        INDEX idx_ios_app_passwords_user (user_id),
        FOREIGN KEY (user_id) REFERENCES ios_users(id) ON DELETE CASCADE
    )""",
    # suite-wide (app '') and per-app settings: proxy sign-in, require_login, default_role
    """CREATE TABLE IF NOT EXISTS ios_settings (
        app VARCHAR(32) NOT NULL,
        name VARCHAR(64) NOT NULL,
        value MEDIUMTEXT NOT NULL,
        PRIMARY KEY (app, name)
    )""",
    # the suite's apps, each written by the app itself when it starts (iosuite_shared.suite): what it is called, its
    # version, and where it is (the address its configuration names, else the best one it was reached at), so the start
    # page and the others link to it without anybody typing an address
    """CREATE TABLE IF NOT EXISTS ios_apps (
        app VARCHAR(32) PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        description VARCHAR(255) NULL,
        version VARCHAR(32) NULL,
        color VARCHAR(16) NULL,
        icon VARCHAR(512) NULL,
        configured_url VARCHAR(512) NULL,
        seen_url VARCHAR(512) NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        last_seen_at BIGINT NULL
    )""",
]

# 0.8.0: the names from before the suite naming convention, renamed once at start (every row kept). Remove once every
# server has run 0.8.0.
RENAME_TABLES = {"users": "ios_users", "user_roles": "ios_user_roles", "user_data": "ios_user_data", "sessions": "ios_sessions",
                 "app_passwords": "ios_app_passwords", "settings": "ios_settings", "suite_apps": "ios_apps"}
RENAME_COLUMNS = {
    "ios_users": {"created": "created_at", "last_login": "last_login_at"},
    "ios_user_data": {"k": "name", "v": "value"},
    "ios_sessions": {"created": "created_at", "expires": "expires_at", "renewed": "renewed_at", "last_seen": "last_seen_at"},
    "ios_app_passwords": {"created": "created_at", "last_used": "last_used_at"},
    "ios_settings": {"k": "name", "v": "value"},
    "ios_apps": {"registered": "created_at", "updated": "updated_at", "last_seen": "last_seen_at"},
}
RENAME_INDEXES = {
    "ios_users": {"name": "uq_ios_users_name", "email": "uq_ios_users_email"},
    "ios_sessions": {"user_id": "idx_ios_sessions_user", "expires": "idx_ios_sessions_expires"},
    "ios_app_passwords": {"prefix": "idx_ios_app_passwords_prefix", "user_id": "idx_ios_app_passwords_user"},
}
# columns added since a table was first made (MariaDB: IF NOT EXISTS); schema 2 gave sessions where they began
UPGRADES = [
    "ALTER TABLE ios_sessions ADD COLUMN IF NOT EXISTS ip VARCHAR(64) NULL",
    "ALTER TABLE ios_sessions ADD COLUMN IF NOT EXISTS cloudflare TINYINT NOT NULL DEFAULT 0",
    "ALTER TABLE ios_sessions ADD COLUMN IF NOT EXISTS client VARCHAR(32) NOT NULL DEFAULT ''",
    "ALTER TABLE ios_sessions ADD COLUMN IF NOT EXISTS user_agent VARCHAR(255) NULL",
    "ALTER TABLE ios_sessions ADD COLUMN IF NOT EXISTS last_seen_at BIGINT NULL",
    "ALTER TABLE ios_sessions ADD COLUMN IF NOT EXISTS last_ip VARCHAR(64) NULL",
]


def _rename_to_convention(cur):
    """Schema 4, before the tables are made: the old names become the convention's."""
    done = schema.rename(cur, RENAME_TABLES, RENAME_COLUMNS)
    if done:
        log.info("shared database: renamed to the suite naming convention: %s", ", ".join(done))


def _finish_convention(cur):
    """Schema 4, once every table exists: the meta table's schema row is replaced by ios_schema_version and its other
    rows (the record of `iosuite-shared migrate` runs) move to ios_settings under app '_migrate'; the old unnamed keys
    get the convention's names; the tables that took the server's default character set get utf8mb4."""
    if "meta" in schema.tables(cur):
        cur.execute("INSERT IGNORE INTO ios_settings (app, name, value) "
                    "SELECT '_migrate', k, COALESCE(v, '') FROM meta WHERE k LIKE 'migrated:%'")
        cur.execute("DROP TABLE meta")
    schema.rename(cur, None, None, RENAME_INDEXES)
    for t in ("ios_user_roles", "ios_sessions"):
        cur.execute("SELECT table_collation AS c FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s", (t,))
        r = cur.fetchone()
        if r and not (r["c"] or "").startswith("utf8mb4"):
            cur.execute(f"ALTER TABLE {t} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")


_overrides: dict = {}  # the tests point the library at another database


def configure(**values):
    """Override host, port, database, user, password (the tests; an app that keeps its own configuration)."""
    global _ready
    _overrides.update({k: v for k, v in values.items() if v is not None})
    settings_cache.clear()
    _ready = False  # another database: its tables are checked again


def params() -> dict:
    p = {"host": config.DB_HOST, "port": config.DB_PORT, "database": config.DB_NAME, "user": config.DB_USER, "password": config.DB_PASSWORD}
    p.update(_overrides)
    return p


def connect(database: str | None = ...):
    p = params()
    return pymysql.connect(host=p["host"], port=int(p["port"]), user=p["user"], password=p["password"],
                           database=p["database"] if database is ... else database, charset="utf8mb4",
                           cursorclass=DictCursor, autocommit=False, connect_timeout=8, read_timeout=60, write_timeout=60)


@contextmanager
def tx():
    """A connection whose work is committed at the end of the block (rolled back on an exception)."""
    con = connect()
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def q(sql: str, args=None) -> list:
    with tx() as con:
        with con.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()


def one(sql: str, args=None):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args=None) -> int:
    """Run one statement; returns lastrowid (inserts) or the affected row count."""
    with tx() as con:
        with con.cursor() as cur:
            cur.execute(sql, args)
            return cur.lastrowid or cur.rowcount


_ready = False
error = None  # why the database could not be opened at start (None once it could)
_init_lock = threading.Lock()
_retry_at = 0.0


def init():
    """Create the database if the account may, then every table (idempotent)."""
    global _ready
    with _init_lock:
        if _ready:
            return
        p = params()
        try:
            con = connect(database=None)
            try:
                with con.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{p['database']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                con.commit()
            finally:
                con.close()
        except pymysql.MySQLError:
            pass  # no right to create databases: it has to exist already
        with tx() as con:
            with con.cursor() as cur:
                _rename_to_convention(cur)
                for stmt in SCHEMA:
                    cur.execute(stmt + OPTIONS)
                for stmt in UPGRADES:
                    cur.execute(stmt)
                _finish_convention(cur)
                schema.ensure_version_table(cur, PREFIX)
                schema.record_version(cur, PREFIX, SCHEMA_VERSION)
        _ready = True


def ready() -> bool:
    return _ready


def available() -> bool:
    """Whether the database can be used; when the start failed, another try at most every 10 seconds."""
    global _retry_at
    if _ready:
        return True
    if time.time() < _retry_at:
        return False
    _retry_at = time.time() + 10
    from iosuite_shared import users
    return users.init()


# ---- settings: suite-wide (app '') with a per-app override
settings_cache: dict = {}  # (app, key) -> (value, at)
CACHE_S = 5.0


def get_setting(key: str, default: str = "", app: str = "") -> str:
    """The setting for this app, else the suite-wide one, else default. Cached a few seconds: asked on every request."""
    now = time.time()
    hit = settings_cache.get((app, key))
    if hit and now - hit[1] < CACHE_S:
        return hit[0] if hit[0] is not None else default
    v = None
    rows = q("SELECT app, value FROM ios_settings WHERE name = %s AND app IN (%s, '')", (key, app))
    by_app = {r["app"]: r["value"] for r in rows}
    if app in by_app:
        v = by_app[app]
    elif "" in by_app:
        v = by_app[""]
    settings_cache[(app, key)] = (v, now)
    return v if v is not None else default


def set_setting(key: str, value, app: str = ""):
    """Store (or, with None / '', remove) a setting for the app ('' : suite-wide)."""
    if value is None or str(value) == "":
        ex("DELETE FROM ios_settings WHERE app = %s AND name = %s", (app, key))
    else:
        ex("INSERT INTO ios_settings (app, name, value) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)", (app, key, str(value)))
    settings_cache.clear()


def settings_of(app: str = "") -> dict:
    """Every setting stored for exactly this app (no fallback), for Settings screens."""
    return {r["name"]: r["value"] for r in q("SELECT name, value FROM ios_settings WHERE app = %s", (app,))}


def json_load(s, default=None):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
