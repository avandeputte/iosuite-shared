"""`iosuite-shared provision`: the SQL planned from config.env and the run against a fake connection; no MariaDB."""
import pytest

from iosuite_shared import cli, install, provision


@pytest.fixture
def fresh_db():
    """Overrides conftest's autouse database fixture: nothing here needs MariaDB."""
    yield


CFG = dict(DB_HOST="db.lan", DB_PORT="3307", DB_NAME="suite", DB_USER="suite", DB_PASSWORD="s3cret",
           IOSUITE_DB_HOST="db.lan", IOSUITE_DB_PORT="3307", IOSUITE_DB_NAME="iosuite", IOSUITE_DB_USER="iosuite", IOSUITE_DB_PASSWORD="sh4red")


def test_plan_one_server():
    jobs = provision.plan_sql(CFG, also=["iocalendar"])
    assert list(jobs) == [("db.lan", "3307")]
    sql = [s for s, _ in jobs[("db.lan", "3307")]]
    assert "CREATE DATABASE IF NOT EXISTS `suite` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" in sql
    assert "CREATE DATABASE IF NOT EXISTS `iosuite` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" in sql
    assert "CREATE DATABASE IF NOT EXISTS `calendar` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" in sql
    assert ("GRANT ALL PRIVILEGES ON `calendar`.* TO `suite`@%s", ("%",)) in jobs[("db.lan", "3307")]
    assert ("ALTER USER `iosuite`@%s IDENTIFIED BY %s", ("%", "sh4red")) in jobs[("db.lan", "3307")]
    assert sql[-1] == "FLUSH PRIVILEGES"
    assert not any("localhost" in str(a) for _, a in jobs[("db.lan", "3307")])  # a remote server: '%' only


def test_plan_local_server_and_two_servers():
    cfg = dict(CFG, DB_HOST="127.0.0.1", DB_PORT="3306")
    jobs = provision.plan_sql(cfg)
    assert set(jobs) == {("127.0.0.1", "3306"), ("db.lan", "3307")}
    assert ("GRANT ALL PRIVILEGES ON `suite`.* TO `suite`@%s", ("localhost",)) in jobs[("127.0.0.1", "3306")]


def test_plan_without_an_app_database_uses_the_shared_one():
    cfg = dict(IOSUITE_DB_HOST="db.lan", IOSUITE_DB_PASSWORD="pw")
    [stmts] = provision.plan_sql(cfg, also=["ionotes"]).values()
    assert ("GRANT ALL PRIVILEGES ON `notes`.* TO `iosuite`@%s", ("%",)) in stmts
    assert not any("`suite`" in s for s, _ in stmts)


def test_bad_identifier():
    with pytest.raises(SystemExit):
        provision.plan_sql(dict(CFG, DB_NAME="suite; drop"))


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args=None):
                conn.log.append((sql, args))

        return Cur()

    def close(self):
        self.log.append(("close", None))


def test_run_executes_everything(monkeypatch):
    seen, lines = [], []
    n = provision.run("root", "rootpw", CFG, also=["iohealth"], connect=lambda h, p: FakeConn(seen), log=lines.append)
    assert n == 1 and seen[-1] == ("close", None) and seen[-2] == ("FLUSH PRIVILEGES", None)
    assert any(s.startswith("CREATE DATABASE IF NOT EXISTS `health`") for s, _ in seen)
    assert "database(s) health, iosuite, suite and user(s) iosuite, suite ready" in lines[0]


def test_run_reports_a_connection_failure():
    def connect(h, p):
        raise OSError("refused")
    with pytest.raises(SystemExit, match="could not connect to db.lan:3307 as root"):
        provision.run("root", "x", CFG, connect=connect, log=lambda *_: None)


def test_install_uses_the_admin_for_the_companions(tmp_path, monkeypatch):
    app = tmp_path / "opt" / "iosuite" / "app"
    app.mkdir(parents=True)
    (app / "config.env").write_text("".join(f"{k}={v}\n" for k, v in CFG.items()))
    calls = {}
    monkeypatch.setenv("DB_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setattr(provision, "run", lambda user, pw, cfg, also=(), **kw: calls.update(user=user, pw=pw, also=list(also)))
    install.install(["iocalendar"], app, db_admin="root", fetcher=lambda n, t, log: t.mkdir(parents=True, exist_ok=True),
                    runner=lambda e, log: 0, log=lambda *_: None)
    assert calls == dict(user="root", pw="rootpw", also=["iocalendar"])


def test_cli_provision(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.env").write_text("".join(f"{k}={v}\n" for k, v in CFG.items()))
    assert cli.main(["--config", str(tmp_path), "provision", "--admin", "root", "--also", "ionotes", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "db.lan:3307" in out and "`notes`" in out and "s3cret" not in out
    seen = {}
    monkeypatch.setenv("DB_ADMIN_PASSWORD", "rootpw")
    monkeypatch.setattr(provision, "run", lambda user, pw, cfg, also=(), **kw: seen.update(user=user, pw=pw, also=list(also)))
    assert cli.main(["--config", str(tmp_path), "provision", "--admin", "root"]) == 0
    assert seen == dict(user="root", pw="rootpw", also=[])
