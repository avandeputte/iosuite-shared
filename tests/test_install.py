"""`iosuite-shared install`: the plan built from a calling app's config.env, without GitHub or a database."""
import pytest

from iosuite_shared import cli, install


@pytest.fixture
def fresh_db():
    """Overrides conftest's autouse database fixture: nothing here needs MariaDB."""
    yield


CFG = """DB_HOST=db.lan
DB_PORT=3307
DB_NAME=suite
DB_USER=suite
DB_PASSWORD=s3cret
IOSUITE_DB_HOST=db.lan
IOSUITE_DB_PORT=3307
IOSUITE_DB_NAME=iosuite
IOSUITE_DB_USER=iosuite
IOSUITE_DB_PASSWORD=sh4red
COOKIE_DOMAIN=example.com
PORT=8630
"""


def test_parse_apps():
    assert install.parse_apps("iopix, ionotes") == ["iopix", "ionotes"]
    assert install.parse_apps(["iocalendar,iocontacts", "iocalendar"]) == ["iocalendar", "iocontacts"]
    assert install.parse_apps("none") == []
    assert install.parse_apps("all") == list(install.APPS)
    with pytest.raises(SystemExit):
        install.parse_apps("iofoo")


def test_plan_shares_the_answers(tmp_path):
    app = tmp_path / "opt" / "iosuite" / "app"
    app.mkdir(parents=True)
    (app / "config.env").write_text(CFG)
    cfg = install.read_env_file(app / "config.env")
    base = install.base_dir(app)
    assert base == tmp_path / "opt"
    entries = install.plan(["iocalendar", "iopix", "iosuite"], cfg, base, admin="alex", service=True, from_app="iosuite")
    assert [e["name"] for e in entries] == ["iocalendar", "iopix"]  # the caller is never reinstalled
    cal = entries[0]
    assert cal["target"] == tmp_path / "opt" / "iocalendar" / "app"
    # the same server, user and password; the app's own database name; the Shared ioSuite database as it is
    assert cal["env"]["IOCALENDAR_DB"] == "db.lan:3307/calendar:suite:s3cret"
    assert cal["env"]["IOCALENDAR_SUITE_DB"] == "db.lan:3307/iosuite:iosuite:sh4red"
    assert cal["env"]["IOCALENDAR_ADMIN"].startswith("alex:")
    assert cal["args"][:5] == ["--yes", "--cookie-domain", "example.com", "--public-url", ""]
    assert "--service" in cal["args"] and "--no-admin" not in cal["args"]
    pix = entries[1]
    assert pix["env"]["IOPIX_DB"] == "db.lan:3307/iopix:suite:s3cret"
    assert "--no-digikam" in pix["args"] and "--photos" in pix["args"]
    assert pix["args"][pix["args"].index("--data") + 1] == str(tmp_path / "opt" / "iopix" / "data")


def test_plan_without_an_app_database_lends_the_shared_one(tmp_path):
    cfg = install.read_env_file(tmp_path / "missing")  # ionotes on SQLite: no DB_* at all
    cfg.update(IOSUITE_DB_HOST="db.lan", IOSUITE_DB_PASSWORD="pw")
    [e] = install.plan(["iohealth"], cfg, tmp_path, service=False)
    assert e["env"]["IOHEALTH_DB"] == "db.lan:3306/health:iosuite:pw"
    assert e["env"]["IOHEALTH_SUITE_DB"] == "db.lan:3306/iosuite:iosuite:pw"
    assert "--no-service" in e["args"] and "--no-admin" in e["args"]


def test_install_runs_each_setup_and_skips_what_is_there(tmp_path, monkeypatch):
    app = tmp_path / "opt" / "iosuite" / "app"
    app.mkdir(parents=True)
    (app / "config.env").write_text(CFG)
    done = tmp_path / "opt" / "iocontacts" / "app"
    done.mkdir(parents=True)
    (done / "config.env").write_text("PORT=8640\n")
    fetched, ran, lines = [], [], []

    def fetcher(name, target, log):
        fetched.append(name)
        target.mkdir(parents=True, exist_ok=True)

    def runner(entry, log):
        ran.append(entry["name"])
        return 1 if entry["name"] == "iohealth" else 0

    failed = install.install(["iocontacts", "iocalendar", "iohealth"], app, photos=str(tmp_path / "photos"),
                             fetcher=fetcher, runner=runner, log=lines.append)
    assert fetched == ["iocalendar", "iohealth"] and ran == ["iocalendar", "iohealth"]
    assert failed == ["iohealth"]
    assert any("already set up" in l for l in lines)


def test_cli_has_install(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(install, "install", lambda names, config_dir, **kw: calls.update(names=names, config_dir=config_dir, **kw) or [])
    assert cli.main(["--config", str(tmp_path), "install", "iopix,ionotes", "--admin", "alex", "--no-service", "--from", "iosuite"]) == 0
    assert calls["names"] == ["iopix", "ionotes"] and calls["admin"] == "alex" and calls["service"] is False and calls["from_app"] == "iosuite"


def test_local_mariadb_gets_the_databases(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(install.shutil, "which", lambda n: "/usr/bin/mariadb")

    class R:
        returncode, stderr = 0, ""

    monkeypatch.setattr(install.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, sql=kw["input"]) or R())
    cfg = dict(DB_HOST="127.0.0.1", DB_USER="iopix", DB_PASSWORD="x")
    entries = install.plan(["iocalendar", "ionotes"], cfg, tmp_path)
    install.prepare_local_databases(entries, cfg, log=lambda *_: None)
    assert seen["cmd"] == ["mariadb"] and "`calendar`" in seen["sql"] and "`notes`" in seen["sql"] and "'iopix'@'localhost'" in seen["sql"]
    seen.clear()
    install.prepare_local_databases(entries, dict(DB_HOST="db.lan", DB_USER="suite"), log=lambda *_: None)
    assert not seen  # a remote server is left to its administrator
