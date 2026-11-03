"""The tests run against a real MariaDB (IOTHING_TEST_DB_* or the IOTHING_AUTH_DB_* settings) in a database of their
own, auth_test, which is dropped and made again for every test. Without a reachable server they are skipped."""
import os

import pytest

from iothing_auth import db


def _params():
    return {"host": os.environ.get("IOTHING_TEST_DB_HOST", os.environ.get("IOTHING_AUTH_DB_HOST", "127.0.0.1")),
            "port": int(os.environ.get("IOTHING_TEST_DB_PORT", os.environ.get("IOTHING_AUTH_DB_PORT", "3306"))),
            "user": os.environ.get("IOTHING_TEST_DB_USER", os.environ.get("IOTHING_AUTH_DB_USER", "auth")),
            "password": os.environ.get("IOTHING_TEST_DB_PASSWORD", os.environ.get("IOTHING_AUTH_DB_PASSWORD", "auth")),
            "database": os.environ.get("IOTHING_TEST_DB_NAME", "auth_test")}


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    p = _params()
    db.configure(**p)
    try:
        con = db.connect(database=None)
    except Exception as e:
        pytest.skip(f"no MariaDB to test against: {e}")
    try:
        with con.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{p['database']}`")
        con.commit()
    finally:
        con.close()
    db._ready = False
    db.settings_cache.clear()
    from iothing_auth import sessions, users
    users.invalidate()
    sessions.invalidate()
    monkeypatch.setattr("iothing_auth.passwords.SCRYPT", (2 ** 12, 8, 1))  # fast hashes for the tests
    monkeypatch.setattr("iothing_auth.config.ADMIN_USER", "")
    db.init()
    yield
