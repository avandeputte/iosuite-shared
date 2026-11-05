"""Settings from the environment, which an app fills from its config.env first (`load_file`).

Every ioThing app keeps its settings in `config.env` in its folder: KEY=value lines, # comments. Variables already in
the environment (a container's, a service's) win over the file.
"""
import os
from pathlib import Path

FILE_NAME = "config.env"


def load_file(folder) -> Path:
    """Read <folder>/config.env into the environment (variables already set win). Call it before anything reads the
    settings, iosuite_shared's included: the values below are read once, when this module is imported."""
    p = Path(folder) / FILE_NAME
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k:
                    os.environ.setdefault(k, v)
    return p


def env(name: str, default: str = "") -> str:
    """The variable, stripped; `default` when it is unset or empty."""
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def env_flag(name: str, default: bool = False) -> bool:
    """An on/off switch: 1, true, yes, on (any case) are on; 0, false, no, off are off; unset or anything else: default."""
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def env_int(name: str, default: int) -> int:
    v = env(name)
    return int(v) if v else default


# The Shared ioSuite database: accounts, sessions, roles, app passwords, and the suite-wide settings and list of apps.
# IOSUITE_DB_HOST, _PORT, _NAME, _USER, _PASSWORD. By default it is on the same server as the app's own database
# (DB_HOST, DB_PORT), with database and user `iosuite`.
PREFIX = "IOSUITE_DB_"
DEFAULTS = {"HOST": env("DB_HOST", "127.0.0.1"), "PORT": env("DB_PORT", "3306"), "NAME": "iosuite", "USER": "iosuite",
            "PASSWORD": "iosuite"}


def shared_db(key: str) -> str:
    return env(PREFIX + key, DEFAULTS[key]) if key != "PASSWORD" else os.environ.get(PREFIX + key, DEFAULTS[key])


DB_HOST = shared_db("HOST")
DB_PORT = int(shared_db("PORT"))
DB_NAME = shared_db("NAME")
DB_USER = shared_db("USER")
DB_PASSWORD = shared_db("PASSWORD")

# the domain the session cookie is shared on (no leading dot): every host under it gets the same cookie
COOKIE_DOMAIN = env("COOKIE_DOMAIN").lstrip(".").lower()
COOKIE = env("COOKIE_NAME", "iothing_session")
SESSION_DAYS = env_int("SESSION_DAYS", 30)

# the first admin on an empty database (containers, installers)
ADMIN_USER = env("ADMIN_USER")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def proxy_env(name: str):
    """A proxy sign-in setting from the environment (AUTH_PROXY, CF_ACCESS_TEAM...), or None when unset."""
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else None
