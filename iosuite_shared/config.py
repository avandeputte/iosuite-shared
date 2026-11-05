"""Settings from the environment (or an env file the app loaded before importing this)."""
import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# The Shared ioSuite database: accounts, sessions, roles, app passwords, and the suite-wide settings and list of apps.
# IOSUITE_DB_HOST, _PORT, _NAME, _USER, _PASSWORD. Default: database and user `iosuite`.
PREFIX = "IOSUITE_DB_"
DEFAULTS = {"HOST": "127.0.0.1", "PORT": "3306", "NAME": "iosuite", "USER": "iosuite", "PASSWORD": "iosuite"}


def shared_db(key: str) -> str:
    return os.environ.get(PREFIX + key, DEFAULTS[key])


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
