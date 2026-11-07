"""Updating an app itself, the same way in every ioThing app: which commit is installed, whether GitHub has a newer
version, running the app's update.sh, restarting, and the tail of the server log.

    from iosuite_shared.updates import Updater
    updater = Updater("iocalendar", version=__version__, root=ROOT)        # repo avandeputte/iocalendar, branch main
    updater.start_daily()                                                   # the daily check, while the setting is on
    app.include_router(auth.server_router(updater, log_file=LOG_FILE))      # GET/POST /api/server, GET /api/log

Each app keeps its `update.sh` next to the code; this module only runs it. The script is told where to write its
outcome and which repository and branch to follow through <PREFIX>_RESULT, <PREFIX>_REPO, <PREFIX>_BRANCH and
<PREFIX>_REF in its environment (PREFIX is the app's key in capitals: IOCALENDAR_RESULT ...). Docker installs are
updated by pulling the image, so there the check only reports.

The "check for updates daily" switch is a setting of the app in the Shared ioSuite database
(`db.get_setting(key, app=...)`); an app that keeps its settings elsewhere passes `settings=(get, set)`.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from iosuite_shared import config, db

DAILY = 24 * 3600
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v or "0")[:3])


def _env_flag_off(name: str) -> bool:
    return not config.env_flag(name, True)


class Updater:
    """One per app. `app` is its key (iocalendar), `version` the running version, `root` the folder with update.sh.
    `repo` and `branch` default to <PREFIX>_REPO / <PREFIX>_BRANCH in the environment, then avandeputte/<app> and main.
    `version_file` is the file in the repository that holds the version line (__version__ = "..."),
    relative to the repository root. UPDATE_CHECK=0 in the environment locks the daily check off."""

    def __init__(self, app: str, version: str, root, repo: str = None, branch: str = None, version_file: str = "app/__init__.py",
                 result_file=None, settings=None, env_prefix: str = None, user_agent: str = None):
        self.app, self.version, self.root = app, version, Path(root)
        self.prefix = (env_prefix or app).upper()
        self.repo = repo or os.environ.get(f"{self.prefix}_REPO") or f"avandeputte/{app}"
        self.branch = branch or os.environ.get(f"{self.prefix}_BRANCH") or "main"
        self.version_file = version_file
        self.result_file = Path(result_file) if result_file else self.root / ".update-result"
        self.user_agent = user_agent or app
        self.check_locked = _env_flag_off("UPDATE_CHECK")
        self._get, self._set = settings or (lambda k, d="": db.get_setting(k, d, app), lambda k, v: db.set_setting(k, v, app))
        self._run_lock = threading.Lock()
        self._checked: dict = {}
        self._commit = None
        self.progress: dict = {"running": False, "error": None, "log": "", "started": None, "finished": None}

    # ---- what is installed
    def in_docker(self) -> bool:
        return os.path.exists("/.dockerenv")

    def local_commit(self) -> str:
        """The installed commit (cached: it only changes through update.sh, which restarts the process)."""
        if self._commit is None:
            self._commit = self._read_commit()  # "unknown" is kept too: no git subprocess per status call
        return self._commit

    def _read_commit(self) -> str:
        if (self.root / ".git").exists():
            try:
                return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root), capture_output=True, text=True,
                                      timeout=10).stdout.strip() or "unknown"
            except (OSError, subprocess.SubprocessError):
                return "unknown"
        try:
            return (self.root / "COMMIT").read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    # ---- the daily check
    def auto_check_enabled(self) -> bool:
        if self.check_locked:
            return False
        try:
            return (self._get("update_check", "1") or "1") == "1"
        except Exception:  # noqa: BLE001  the database is away: no check
            return False

    def set_auto_check(self, on: bool) -> None:
        if self.check_locked and on:
            raise ValueError("UPDATE_CHECK=0 in the environment keeps the daily check off")
        self._set("update_check", "1" if on else "0")
        if not on:
            self.forget_check()

    def forget_check(self) -> None:
        self._checked.clear()

    def _get_url(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()

    def _remote_version(self, sha: str) -> str | None:
        """The version at that commit, so a commit that only changes the docs or the workflow is not announced as new."""
        text = self._get_url(f"https://raw.githubusercontent.com/{self.repo}/{sha}/{self.version_file}", "text/plain").decode("utf-8", "replace")
        m = VERSION_RE.search(text)
        return m.group(1) if m else None

    def check(self) -> dict:
        """{installed, version, remote, remoteDate, remoteMessage, remoteVersion, status: current | update | error, checked}"""
        mine = self.local_commit()
        out = {"installed": mine[:7], "version": self.version, "checked": int(time.time())}
        try:
            c = json.loads(self._get_url(f"https://api.github.com/repos/{self.repo}/commits/{self.branch}"))
            out.update(remote=c["sha"][:7], remoteDate=c["commit"]["committer"]["date"][:10],
                       remoteMessage=c["commit"]["message"].splitlines()[0][:120])
            if mine != "unknown" and c["sha"].startswith(mine):
                out["status"] = "current"
            else:
                try:
                    rv = self._remote_version(c["sha"])
                except Exception:  # noqa: BLE001
                    rv = None
                out["remoteVersion"] = rv
                # a newer build is one with a higher version; the same version under another commit (docs, the
                # workflow) is nothing to install, and an older one means this install is ahead of the branch
                out["status"] = "update" if rv is None or _version_tuple(rv) > _version_tuple(self.version) else "current"
        except Exception as e:  # noqa: BLE001
            out.update(status="error", error=str(e)[:160])
        self._checked.clear()
        self._checked.update(out)
        return out

    def available(self) -> dict | None:
        """What the banner needs, from the last check: {version, remote, message, date}, or None."""
        c = dict(self._checked)
        if c.get("status") != "update":
            return None
        return {"remote": c.get("remote"), "version": c.get("remoteVersion"), "message": c.get("remoteMessage"), "date": c.get("remoteDate")}

    def start_daily(self, first_after: float = 60) -> threading.Thread:
        """Ask GitHub once a day (the first time a minute after start) whether the branch has moved on, while the
        setting is on. Only the public commit is fetched; nothing about this install is sent."""
        def loop():
            time.sleep(first_after)
            while True:
                try:
                    if self.auto_check_enabled():
                        self.check()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(DAILY)
        t = threading.Thread(target=loop, name="update-check", daemon=True)
        t.start()
        return t

    # ---- running update.sh
    def last_result(self) -> dict | None:
        """The outcome of the last update.sh run, from the file it writes before the restart: {ok, text, at}."""
        try:
            text = self.result_file.read_text().strip()
        except OSError:
            return None
        if not text:
            return None
        return {"ok": text.startswith("ok:"), "text": text.split(":", 1)[-1].strip()[:300], "at": int(self.result_file.stat().st_mtime)}

    def _writable(self) -> bool:
        paths = [self.root] + [p for p in (self.root / ".venv", self.root / ".git") if p.exists()]
        return all(os.access(str(p), os.W_OK) for p in paths)

    def can_run(self) -> bool:
        return (self.root / "update.sh").exists() and (self.root / ".venv").exists() and not self.in_docker() and self._writable()

    def status(self) -> dict:
        return {"version": self.version, "installed": self.local_commit()[:7], "docker": self.in_docker(), "canRun": self.can_run(),
                "repo": self.repo, "branch": self.branch, "check": dict(self._checked) or None, "progress": dict(self.progress),
                "lastUpdate": self.last_result(), "autoCheck": self.auto_check_enabled(), "autoCheckLocked": self.check_locked}

    def run(self, ref: str = "") -> None:
        """update.sh in a child process; the service manager starts the app again when it finishes.
        ref: a commit or branch to install instead of the tip of the branch. Raises RuntimeError when it cannot run."""
        if self.in_docker():
            raise RuntimeError("In Docker, update by pulling the image: docker compose pull && docker compose up -d")
        if not self.can_run():
            raise RuntimeError("This install cannot update itself (no update.sh or .venv next to the app, or the folder is not "
                               "writable by the service). Run ./update.sh in the app folder.")
        ref = (ref or "").strip()[:80]
        if ref and not re.fullmatch(r"[0-9a-f]{7,40}|[A-Za-z0-9][A-Za-z0-9._-]*", ref):
            raise RuntimeError("ref must be a commit id or a branch name")
        with self._run_lock:
            if self.progress["running"]:
                raise RuntimeError("An update is already running")
            self.progress.update(running=True, error=None, log="", started=int(time.time()), finished=None)
        env = dict(os.environ, **{f"{self.prefix}_RESULT": str(self.result_file), f"{self.prefix}_REPO": self.repo, f"{self.prefix}_BRANCH": self.branch})
        if ref:
            env[f"{self.prefix}_REF"] = ref
        try:
            self.result_file.unlink()
        except OSError:
            pass

        def work():
            try:
                res = subprocess.run(["sh", str(self.root / "update.sh")], cwd=str(self.root), capture_output=True, text=True, timeout=1800, env=env)
                self.progress["log"] = (res.stdout + res.stderr)[-3000:]
                if res.returncode != 0:
                    tail = re.sub(r"\x1b\[[0-9;]*m", "", (res.stderr or res.stdout)).strip()
                    self.progress["error"] = tail.splitlines()[-1] if tail else f"exit {res.returncode}"
                elif os.environ.get("INVOCATION_ID") and "updated to" in (self.last_result() or {}).get("text", ""):
                    # still running after a successful update: the service could not be restarted from here (it runs
                    # as an account without sudo), so this process ends and systemd (Restart=always) starts the new version
                    self.restart(1.5)
            except Exception as e:  # noqa: BLE001
                self.progress["error"] = str(e)[:300]
            finally:
                self.progress.update(running=False, finished=int(time.time()))
                self._checked.clear()

        threading.Thread(target=work, name="self-update", daemon=True).start()

    @staticmethod
    def restart(delay: float = 0.5) -> None:
        """End the process shortly; systemd (Restart=always), launchd (KeepAlive) or Docker (restart: unless-stopped)
        starts the app again."""
        threading.Timer(delay, lambda: os._exit(0)).start()


# ---- the server log
PAGE_REQUEST = " uvicorn.access: "
LEVELS = ("any", "warning", "error", "auth")


def tail(path, lines: int = 300, contains: str = "", level: str = "any", requests: bool = True) -> dict:
    """The last `lines` lines of the log file. `contains` keeps lines with that text; `level` "warning" or "error"
    keeps only those (tracebacks count as errors), "auth" only sign-ins, failed attempts and sign-outs; `requests`
    False drops the page-request lines."""
    path = Path(path)
    lines = max(1, min(int(lines), 5000))
    if not path.exists():
        return {"file": str(path), "size_bytes": 0, "returned": 0, "lines": [], "note": "no log file yet (the server writes it from its next start)"}
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 4_000_000))
        text = f.read().decode("utf-8", "replace")
    out = text.splitlines()
    if size > 4_000_000:
        out = out[1:]  # the first line was cut in the middle
    if not requests:
        out = [l for l in out if PAGE_REQUEST not in l]
    if contains:
        c = contains.lower()
        out = [l for l in out if c in l.lower()]
    if level == "auth":
        out = [l for l in out if " iosuite_shared.auth: " in l]
    elif level == "warning":
        out = [l for l in out if " WARNING " in l or " ERROR " in l or " CRITICAL " in l or "Traceback" in l]
    elif level == "error":
        out = [l for l in out if " ERROR " in l or " CRITICAL " in l or "Traceback" in l]
    return {"file": str(path), "size_bytes": size, "returned": min(lines, len(out)), "lines": out[-lines:]}


# ---- the server log file
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_log(path, level: int = None) -> bool:
    """Logging the same way in every app: INFO to stderr (the service's journal), and when `path` is set a rotating copy
    (5 MB, 3 old files) of everything, uvicorn's request and error lines included, which Maintenance › Updates and logs
    and `tail()` read. Idempotent: a second call (the tests start the app more than once) adds nothing. False when the
    file could not be opened (the app runs without it)."""
    import logging
    import logging.handlers
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level or logging.INFO, format=LOG_FORMAT)
    elif level:
        root.setLevel(level)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if not path:
        return False
    if any(isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(Path(path).resolve()) for h in root.handlers):
        return True
    try:
        h = logging.handlers.RotatingFileHandler(path, maxBytes=5 << 20, backupCount=3, encoding="utf-8")
    except OSError as e:
        logging.getLogger("iosuite_shared.updates").warning("no server log at %s: %s", path, e)
        return False
    h.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(h)
    for name in ("uvicorn.access", "uvicorn.error"):  # uvicorn's loggers do not propagate to the root
        logging.getLogger(name).addHandler(h)
    return True
