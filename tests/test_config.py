"""config.env, the on/off switches and the Shared ioSuite database's default server: no database needed."""
import importlib

import pytest

from iosuite_shared import config


@pytest.fixture(autouse=True)
def fresh_db():  # overrides conftest's: nothing here touches a database
    yield


def test_load_file_fills_only_what_is_unset(tmp_path, monkeypatch):
    (tmp_path / "config.env").write_text('# a comment\nA_ONE=1\nA_TWO = "two words"\nA_SET=from-file\n\nnot a line\n')
    monkeypatch.delenv("A_ONE", raising=False)
    monkeypatch.delenv("A_TWO", raising=False)
    monkeypatch.setenv("A_SET", "from-env")
    assert config.load_file(tmp_path) == tmp_path / "config.env"
    import os
    assert os.environ["A_ONE"] == "1" and os.environ["A_TWO"] == "two words" and os.environ["A_SET"] == "from-env"
    monkeypatch.delenv("A_ONE"); monkeypatch.delenv("A_TWO")


def test_load_file_refreshes_the_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("COOKIE_DOMAIN", raising=False)
    monkeypatch.delenv("IOSUITE_DB_HOST", raising=False)
    (tmp_path / "config.env").write_text("COOKIE_DOMAIN=example.org\nDB_HOST=db.example.lan\n")
    try:
        config.load_file(tmp_path)
        assert config.COOKIE_DOMAIN == "example.org" and config.DB_HOST == "db.example.lan"
    finally:
        monkeypatch.delenv("COOKIE_DOMAIN", raising=False)
        monkeypatch.delenv("DB_HOST", raising=False)
        monkeypatch.undo()
        config._read()


def test_load_file_without_a_file(tmp_path):
    assert not config.load_file(tmp_path).exists()


@pytest.mark.parametrize("v,want", [("1", True), ("on", True), ("Yes", True), ("TRUE", True), ("0", False), ("off", False),
                                    ("no", False), ("", None), ("maybe", None)])
def test_env_flag(monkeypatch, v, want):
    monkeypatch.setenv("A_FLAG", v)
    assert config.env_flag("A_FLAG", True) is (True if want is None else want)
    assert config.env_flag("A_FLAG", False) is (False if want is None else want)


def test_shared_database_defaults_to_the_apps_server(monkeypatch):
    for k in ("IOSUITE_DB_HOST", "IOSUITE_DB_PORT", "COOKIE_DOMAIN", "COOKIE_NAME", "SESSION_DAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_HOST", "db.example.lan")
    monkeypatch.setenv("DB_PORT", "3307")
    monkeypatch.setenv("COOKIE_DOMAIN", ".Example.com")
    try:
        c = importlib.reload(config)
        assert (c.DB_HOST, c.DB_PORT, c.DB_NAME, c.DB_USER) == ("db.example.lan", 3307, "iosuite", "iosuite")
        assert (c.COOKIE_DOMAIN, c.COOKIE, c.SESSION_DAYS) == ("example.com", "iothing_session", 30)
        monkeypatch.setenv("IOSUITE_DB_HOST", "suite.example.lan")
        assert importlib.reload(config).DB_HOST == "suite.example.lan"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_every_app_has_its_own_port():
    from iosuite_shared import suite
    assert len(set(suite.PORTS.values())) == len(suite.PORTS) == 6
