"""Settings from the environment, which an app fills from its config.env first (`load_file`).

Every ioThing app keeps its settings in `config.env` in its folder: KEY=value lines, # comments. Variables already in
the environment (a container's, a service's) win over the file.
"""
import os
from pathlib import Path

FILE_NAME = "config.env"


def load_file(folder) -> Path:
    """Read <folder>/config.env into the environment (variables already set win), and the settings below again. Call
    it first, before the app or iosuite_shared's other modules read a setting."""
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
    _read()
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


def shared_db(key: str) -> str:
    defaults = {"HOST": env("DB_HOST", "127.0.0.1"), "PORT": env("DB_PORT", "3306"), "NAME": "iosuite", "USER": "iosuite",
                "PASSWORD": "iosuite"}
    return env(PREFIX + key, defaults[key]) if key != "PASSWORD" else os.environ.get(PREFIX + key, defaults[key])


def proxy_env(name: str):
    """A proxy sign-in setting from the environment (AUTH_PROXY, CF_ACCESS_TEAM...), or None when unset."""
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else None


def _read():
    """The settings, from the environment as it is now."""
    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, COOKIE_DOMAIN, COOKIE, SESSION_DAYS, ADMIN_USER, ADMIN_PASSWORD
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


_read()


# ---- the settings every app reads the same way
def standard(app: str, app_dir, log_name: str = None) -> dict:
    """The settings every app of the suite has, read after load_file(app_dir), for the app's config module
    (`globals().update(shared.standard("iocalendar", APP_DIR))`):

      APP_DIR               the app's folder (config.env, update.sh, .venv live there)
      DATA_DIR              DATA_DIR, else APP_DIR/data: what the app writes on its own (made when missing)
      STATIC_DIR            APP_DIR/static: the web app's files, served under /static/
      LOG_FILE              LOG_FILE, else DATA_DIR/<app>.log; LOG_FILE= with nothing after it: None (no file)
      HOST, PORT            where the server listens: 0.0.0.0 and the app's own default port (suite.PORTS)
      PUBLIC_URL            the address people and the other apps reach the app at; empty: the one it is reached at
      BASE_URL              PUBLIC_URL, else http://localhost:PORT (the links in MCP results)
      FORWARDED_ALLOW_IPS   the addresses that may tell the server the visitor's address and scheme (X-Forwarded-*): * by default
      TLS_CERT_FILE, TLS_KEY_FILE   HTTPS served directly instead of behind a reverse proxy: both files
      MCP_FORCED            MCP=1: the MCP endpoint is on whatever the setting says
      ADMIN_USER, ADMIN_PASSWORD    the first admin account, created on a first start with no accounts (containers, installers)

    IOSUITE_DB_* (this module), <APP>_REPO, <APP>_BRANCH, UPDATE_CHECK and MCP_ADMIN_TOKEN (iosuite_shared.updates) are
    read where they are used."""
    from iosuite_shared import suite
    app_dir = Path(app_dir).resolve()
    data_dir = Path(env("DATA_DIR", str(app_dir / "data"))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    port = env_int("PORT", suite.PORTS[app])
    public_url = env("PUBLIC_URL").rstrip("/")
    log_file = Path(env("LOG_FILE", str(data_dir / f"{log_name or app}.log"))).expanduser().resolve() if os.environ.get("LOG_FILE", "x") else None
    return {"APP_DIR": app_dir, "DATA_DIR": data_dir, "STATIC_DIR": app_dir / "static", "LOG_FILE": log_file,
            "HOST": env("HOST", "0.0.0.0"), "PORT": port, "PUBLIC_URL": public_url, "BASE_URL": public_url or f"http://localhost:{port}",
            "FORWARDED_ALLOW_IPS": env("FORWARDED_ALLOW_IPS", "*"), "TLS_CERT_FILE": env("TLS_CERT_FILE"), "TLS_KEY_FILE": env("TLS_KEY_FILE"),
            "MCP_FORCED": env_flag("MCP"), "ADMIN_USER": ADMIN_USER, "ADMIN_PASSWORD": ADMIN_PASSWORD}


def own_db(default_name: str, default_user: str = None) -> dict:
    """The app's own MariaDB (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD) as {host, port, database, user, password}.
    No default password: setup.sh writes one into config.env, a container gets it from its environment."""
    return {"host": env("DB_HOST", "127.0.0.1"), "port": env_int("DB_PORT", 3306), "database": env("DB_NAME", default_name),
            "user": env("DB_USER", default_user or default_name), "password": os.environ.get("DB_PASSWORD", "")}
