"""The auth database: connections, the schema, and the suite-wide / per-app settings table.

Every call opens its own connection and closes it (PyMySQL connections are not shared between threads). Rows come
back as dicts.
"""
import json
import threading
import time
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from iothing_auth import config

SCHEMA_VERSION = 2

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (
        k VARCHAR(64) PRIMARY KEY,
        v MEDIUMTEXT
    )""",
    # one account for the whole suite. hash '' is a passwordless account: one-click sign-in on the LAN, viewer at most.
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(64) NOT NULL,
        display_name VARCHAR(128) NULL,
        email VARCHAR(191) NULL,
        hash VARCHAR(255) NOT NULL DEFAULT '',
        suite_admin TINYINT NOT NULL DEFAULT 0,
        listed TINYINT NOT NULL DEFAULT 1,
        created BIGINT NOT NULL,
        last_login BIGINT NULL,
        session_gen INT NOT NULL DEFAULT 0,
        UNIQUE KEY (name),
        UNIQUE KEY (email)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    # the role an account holds in one app; no row: the app's default_role setting (viewer unless changed)
    """CREATE TABLE IF NOT EXISTS user_roles (
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        role ENUM('none','viewer','editor','admin') NOT NULL,
        PRIMARY KEY (user_id, app),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    # per-user, per-app values (display settings, the ioPix person tag, the ioContacts "me" contact), JSON-encoded
    """CREATE TABLE IF NOT EXISTS user_data (
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        k VARCHAR(64) NOT NULL,
        v MEDIUMTEXT NOT NULL,
        PRIMARY KEY (user_id, app, k),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    # a browser or app session: the cookie carries a random token, the table its hash. Shared by every app on the
    # domain. Where it began and was last seen: the visitor's address (the real one, through Cloudflare or a trusted
    # proxy), whether it came through Cloudflare, the client (web, or the app's User-Agent product) and the full User-Agent.
    """CREATE TABLE IF NOT EXISTS sessions (
        token_hash CHAR(64) PRIMARY KEY,
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        via VARCHAR(16) NOT NULL,
        created BIGINT NOT NULL,
        expires BIGINT NOT NULL,
        renewed BIGINT NOT NULL,
        ip VARCHAR(64) NULL,
        cloudflare TINYINT NOT NULL DEFAULT 0,
        client VARCHAR(32) NOT NULL DEFAULT '',
        user_agent VARCHAR(255) NULL,
        last_seen BIGINT NULL,
        last_ip VARCHAR(64) NULL,
        INDEX (user_id),
        INDEX (expires),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    # app passwords: one per device, for one app (CardDAV, LDAP, MCP clients...); shown once, revocable
    """CREATE TABLE IF NOT EXISTS app_passwords (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        app VARCHAR(32) NOT NULL,
        label VARCHAR(128) NOT NULL,
        prefix CHAR(8) NOT NULL,
        hash VARCHAR(255) NOT NULL,
        created BIGINT NOT NULL,
        last_used BIGINT NULL,
        INDEX (prefix),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
    # suite-wide (app '') and per-app settings: proxy sign-in, require_login, default_role
    """CREATE TABLE IF NOT EXISTS settings (
        app VARCHAR(32) NOT NULL,
        k VARCHAR(64) NOT NULL,
        v MEDIUMTEXT NOT NULL,
        PRIMARY KEY (app, k)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci""",
]

# schema 2 (sessions carry where they began and were last seen)
UPGRADES = [
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ip VARCHAR(64) NULL",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cloudflare TINYINT NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS client VARCHAR(32) NOT NULL DEFAULT ''",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_agent VARCHAR(255) NULL",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen BIGINT NULL",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_ip VARCHAR(64) NULL",
]

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
                for stmt in SCHEMA:
                    cur.execute(stmt)
                for stmt in UPGRADES:  # columns added since the first schema (MariaDB: IF NOT EXISTS)
                    cur.execute(stmt)
                cur.execute("INSERT INTO meta (k, v) VALUES ('schema', %s) ON DUPLICATE KEY UPDATE v = VALUES(v)", (str(SCHEMA_VERSION),))
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
    from iothing_auth import users
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
    rows = q("SELECT app, v FROM settings WHERE k = %s AND app IN (%s, '')", (key, app))
    by_app = {r["app"]: r["v"] for r in rows}
    if app in by_app:
        v = by_app[app]
    elif "" in by_app:
        v = by_app[""]
    settings_cache[(app, key)] = (v, now)
    return v if v is not None else default


def set_setting(key: str, value, app: str = ""):
    """Store (or, with None / '', remove) a setting for the app ('' : suite-wide)."""
    if value is None or str(value) == "":
        ex("DELETE FROM settings WHERE app = %s AND k = %s", (app, key))
    else:
        ex("INSERT INTO settings (app, k, v) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE v = VALUES(v)", (app, key, str(value)))
    settings_cache.clear()


def settings_of(app: str = "") -> dict:
    """Every setting stored for exactly this app (no fallback), for Settings screens."""
    return {r["k"]: r["v"] for r in q("SELECT k, v FROM settings WHERE app = %s", (app,))}


def json_load(s, default=None):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
