"""Settings from the environment (or an env file the app loaded before importing this)."""
import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# The Shared ioSuite database: accounts, sessions, roles, app passwords, and the suite-wide settings and list of apps.
# IOSUITE_SHARED_DB_HOST, _PORT, _NAME, _USER, _PASSWORD; the names it had before 0.4 (IOTHING_AUTH_DB_*, when it was
# "the auth database") still work, so an existing install keeps its database. Default: database and user `iosuite`.
SHARED_PREFIX, LEGACY_PREFIX = "IOSUITE_SHARED_DB_", "IOTHING_AUTH_DB_"
DEFAULTS = {"HOST": "127.0.0.1", "PORT": "3306", "NAME": "iosuite", "USER": "iosuite", "PASSWORD": "iosuite"}
LEGACY_DEFAULTS = {"NAME": "auth", "USER": "auth", "PASSWORD": "auth"}  # what an install that set nothing used before 0.4


def shared_db(key: str) -> str:
    v = os.environ.get(SHARED_PREFIX + key)
    if v is None:
        v = os.environ.get(LEGACY_PREFIX + key)
    return v if v is not None else DEFAULTS[key]


# whether anything names the database's name or account: if nothing does, an install from before 0.4 may be on the old
# defaults (auth/auth), which db.init() falls back to when the new ones are refused
DB_EXPLICIT = any(os.environ.get(p + k) is not None for p in (SHARED_PREFIX, LEGACY_PREFIX) for k in ("NAME", "USER", "PASSWORD"))

DB_HOST = shared_db("HOST")
DB_PORT = int(shared_db("PORT"))
DB_NAME = shared_db("NAME")
DB_USER = shared_db("USER")
DB_PASSWORD = shared_db("PASSWORD")

# the domain the session cookie is shared on (no leading dot): every host under it gets the same cookie
COOKIE_DOMAIN = env("IOTHING_COOKIE_DOMAIN", "").strip().lstrip(".").lower()
COOKIE = env("IOTHING_COOKIE", "iothing_session")
SESSION_DAYS = int(env("IOTHING_SESSION_DAYS", "30"))

# the first admin on an empty database (containers, installers)
ADMIN_USER = env("ADMIN_USER")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")


def proxy_env(name: str):
    """A proxy sign-in setting from the environment (AUTH_PROXY, CF_ACCESS_TEAM...), or None when unset."""
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else None
