"""`iosuite-shared install APP...`: put other apps of the suite on this machine, beside the one that is already here,
without asking the shared questions again.

Every app's setup.sh ends with "Also install other apps of the suite here?" (or takes `--also APP,APP`). The answer
comes here: with the calling app's config.env (`--config FOLDER`) this reads the answers that are the same for every
app and hands them to each companion's own setup.sh, which runs unattended (`--yes`):

  * the app database server: DB_HOST, DB_PORT, DB_USER and DB_PASSWORD are reused, each app gets its own database
    name (iosuite -> suite, iocalendar -> calendar, ...; the app creates it when the user may);
  * the Shared ioSuite database: IOSUITE_DB_* as they are;
  * the sign-in domain (COOKIE_DOMAIN) and, with `--admin NAME` and ADMIN_PASSWORD in the environment, the admin
    account, made an admin of every companion too (the account already exists in the shared database).

Each companion is fetched from GitHub into BASE/<app>/app (BASE is the folder above the calling app's own
<name>/app, so /opt when the app is in /opt/iosuite/app) with its commit in COMMIT, the layout the Proxmox installers
make and update.sh understands; then `sh setup.sh --yes ...` runs there and installs the packages, writes config.env,
creates the tables and (as root with systemd) the service. The passwords travel in the environment, never on a
command line. What differs per app is left to its defaults: the usual port, no public address (reached as is), the
system time zone. ioPix gets `--no-digikam` and a photos folder (`--photos DIR`, default /library/photos, created when
missing), and the AI models are left off (`--features none`) so the install stays small; both are changed later in
its Settings or by running its setup.sh again.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# name -> how to fetch and set it up. `db` is the app's default database name; `env` the prefix of the environment
# variables its setup.sh reads the secrets from (PFX_DB, PFX_SUITE_DB, PFX_ADMIN); `subdir` the folder of the
# repository that holds the app when it is not the root.
APPS = {
    "iosuite": dict(label="ioSuite", repo="avandeputte/iosuite", db="suite", port=8630, env="IOSUITE"),
    "iocalendar": dict(label="ioCalendar", repo="avandeputte/iocalendar", db="calendar", port=8620, env="IOCALENDAR"),
    "iocontacts": dict(label="ioContacts", repo="avandeputte/iocontacts", db="contacts", port=8640, env="IOCONTACTS"),
    "ionotes": dict(label="ioNotes", repo="avandeputte/ionotes", db="notes", port=8610, env="IONOTES", data=True,
                    extra=["--features", "none"]),
    "iohealth": dict(label="ioHealth", repo="avandeputte/ioHealth", db="health", port=47800, env="IOHEALTH", subdir="server"),
    "iopix": dict(label="ioPix", repo="avandeputte/iopix", db="iopix", port=8600, env="IOPIX", data=True,
                  extra=["--no-digikam", "--features", "none"], photos=True),
}
DEFAULT_PHOTOS = "/library/photos"


def read_env_file(path):
    """config.env as a dict (KEY=VALUE lines; the last one wins; no quoting, as the apps' setup.sh write it)."""
    out = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_apps(spec):
    """'iopix, ionotes' / ['iopix,ionotes'] -> ['iopix', 'ionotes']; 'all' -> every app; 'none' / '' -> []."""
    if isinstance(spec, str):
        spec = [spec]
    names = []
    for part in spec:
        for n in part.replace(";", ",").replace(" ", ",").split(","):
            n = n.strip().lower()
            if not n or n == "none":
                continue
            if n == "all":
                names.extend(a for a in APPS if a not in names)
                continue
            if n not in APPS:
                raise SystemExit(f"error: unknown app {n!r} (one of: {', '.join(APPS)}, or all)")
            if n not in names:
                names.append(n)
    return names


def dsn(host, port, name, user, password):
    return f"{host}:{port}/{name}:{user}:{password}"


def shared_answers(cfg):
    """The answers every app shares, from the calling app's config.env: the app database server (its credentials;
    the database name differs per app), the Shared ioSuite database and the cookie domain. An app without a MariaDB
    of its own (ioNotes on SQLite, ioPix set up from its web page) lends the Shared ioSuite database's server and
    credentials for the companions' databases instead."""
    s_host = cfg.get("IOSUITE_DB_HOST") or cfg.get("DB_HOST") or "127.0.0.1"
    s_port = cfg.get("IOSUITE_DB_PORT") or cfg.get("DB_PORT") or "3306"
    s_name = cfg.get("IOSUITE_DB_NAME") or "iosuite"
    s_user = cfg.get("IOSUITE_DB_USER") or "iosuite"
    s_pw = cfg.get("IOSUITE_DB_PASSWORD") or ""
    if cfg.get("DB_HOST") and cfg.get("DB_USER") and cfg.get("DB_TYPE", "mysql") in ("mysql", "mariadb"):
        a_host, a_port, a_user, a_pw = cfg["DB_HOST"], cfg.get("DB_PORT") or "3306", cfg["DB_USER"], cfg.get("DB_PASSWORD") or ""
    else:
        a_host, a_port, a_user, a_pw = s_host, s_port, s_user, s_pw
    return dict(app_db=(a_host, a_port, a_user, a_pw), suite_db=dsn(s_host, s_port, s_name, s_user, s_pw),
                cookie_domain=cfg.get("COOKIE_DOMAIN", ""))


def base_dir(config_dir):
    """/opt/iosuite/app -> /opt (the companions go to /opt/<app>/app); /srv/iosuite -> /srv."""
    d = Path(config_dir).resolve()
    return d.parent.parent if d.name == "app" else d.parent


def plan(names, cfg, base, admin=None, service=None, photos=None, from_app=None):
    """One entry per app: where it goes, the setup.sh arguments and the environment with the secrets."""
    ans = shared_answers(cfg)
    a_host, a_port, a_user, a_pw = ans["app_db"]
    out = []
    for n in names:
        if n == from_app:
            continue
        a = APPS[n]
        target = Path(base) / n / "app"
        args = ["--yes", "--cookie-domain", ans["cookie_domain"], "--public-url", ""]
        if service is True:
            args.append("--service")
        elif service is False:
            args.append("--no-service")
        args += list(a.get("extra", []))
        if a.get("data"):
            args += ["--data", str(Path(base) / n / "data")]
        if a.get("photos"):
            args += ["--photos", photos or DEFAULT_PHOTOS]
        env = {f"{a['env']}_DB": dsn(a_host, a_port, a["db"], a_user, a_pw), f"{a['env']}_SUITE_DB": ans["suite_db"]}
        if admin:
            env[f"{a['env']}_ADMIN"] = f"{admin}:{os.environ.get('ADMIN_PASSWORD', '')}"
        else:
            args.append("--no-admin")
        out.append(dict(name=n, label=a["label"], target=target, args=args, env=env, port=a["port"]))
    return out


def say(msg):
    print(f"\n\033[1;34m==>\033[0m {msg}", flush=True)


def fetch(name, target, log=print):
    """A shallow clone of the app's repository into TARGET (its `subdir` when it has one), COMMIT written, .git removed:
    the layout the Proxmox installers make, which the app's update.sh keeps up to date."""
    a = APPS[name]
    url = os.environ.get(f"{a['env']}_REPO") or f"https://github.com/{a['repo']}"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{name}-src-", dir=str(target.parent)))
    try:
        subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(tmp)], check=True)
        sha = subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        src = tmp / a["subdir"] if a.get("subdir") else tmp
        shutil.rmtree(tmp / ".git", ignore_errors=True)
        shutil.copytree(src, target, symlinks=True, dirs_exist_ok=True)
        (target / "COMMIT").write_text(sha + "\n")
        log(f"  fetched {a['label']} {sha[:7]} into {target}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_setup(entry, log=print):
    env = dict(os.environ)
    env.update(entry["env"])
    cmd = ["sh", "./setup.sh"] + entry["args"]
    log("  sh ./setup.sh " + " ".join(a if a else "''" for a in entry["args"]))
    return subprocess.run(cmd, cwd=str(entry["target"]), env=env).returncode


def install(names, config_dir, base=None, admin=None, service=None, photos=None, from_app=None, fetcher=fetch, runner=run_setup, log=print):
    """Install every app in NAMES; returns the names that failed (empty when all went well)."""
    cfg = read_env_file(Path(config_dir) / "config.env") if config_dir else {}
    base = Path(base) if base else base_dir(config_dir or os.getcwd())
    entries = plan(names, cfg, base, admin=admin, service=service, photos=photos, from_app=from_app)
    if not entries:
        log("nothing to install")
        return []
    if any(APPS[e["name"]].get("photos") for e in entries):
        p = Path(photos or DEFAULT_PHOTOS)
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            log(f"  made the photos folder {p}: mount or copy your photos there (ioPix's Settings can add more folders)")
    failed = []
    for e in entries:
        say(f"Installing {e['label']} into {e['target']}")
        if (e["target"] / "config.env").exists():
            log(f"  {e['label']} is already set up in {e['target']}: left as it is (its own setup.sh or update.sh changes it)")
            continue
        try:
            fetcher(e["name"], e["target"], log=log)
        except Exception as exc:  # noqa: BLE001 - one failing app must not stop the others
            log(f"  could not fetch {e['label']}: {exc}")
            failed.append(e["name"])
            continue
        rc = runner(e, log=log)
        if rc != 0:
            log(f"  {e['label']}'s setup.sh failed (exit {rc}); fix the cause and run `sh {e['target']}/setup.sh` yourself")
            failed.append(e["name"])
        else:
            log(f"  {e['label']} runs on port {e['port']}")
    return failed


def add_parser(sub):
    p = sub.add_parser("install", help="install other apps of the suite here, sharing the answers of the app in --config")
    p.add_argument("apps", nargs="+", help="iosuite, iopix, ionotes, iocalendar, iocontacts, iohealth (comma-separated or several), or all")
    p.add_argument("--into", default=None, help="the folder the apps go under, each as <folder>/<app>/app (default: beside the calling app, e.g. /opt)")
    p.add_argument("--admin", default=None, help="make this account an admin of every app too (its password from ADMIN_PASSWORD)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--service", dest="service", action="store_true", default=None, help="install and start each app's systemd service")
    g.add_argument("--no-service", dest="service", action="store_false", help="only install; start each with its run.sh")
    p.add_argument("--photos", default=None, help=f"ioPix: the folder with the photos (default {DEFAULT_PHOTOS})")
    p.add_argument("--from", dest="from_app", default=None, help="the app that is calling (never reinstalled)")
    return p


def main(a):
    names = parse_apps(a.apps)
    failed = install(names, a.config, base=a.into, admin=a.admin, service=a.service, photos=a.photos, from_app=a.from_app)
    if failed:
        print(f"\nerror: not installed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0
