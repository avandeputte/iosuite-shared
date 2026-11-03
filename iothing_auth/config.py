"""Settings from the environment (or an env file the app loaded before importing this)."""
import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DB_HOST = env("IOTHING_AUTH_DB_HOST", "127.0.0.1")
DB_PORT = int(env("IOTHING_AUTH_DB_PORT", "3306"))
DB_NAME = env("IOTHING_AUTH_DB_NAME", "auth")
DB_USER = env("IOTHING_AUTH_DB_USER", "auth")
DB_PASSWORD = env("IOTHING_AUTH_DB_PASSWORD", "auth")

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
