"""The tests run against a MariaDB of their own (iothing_auth.testing: a Docker container, or the server named by
IOTHING_TEST_DB=host:port:user:password), in a database `iothing_auth_test` dropped and made again for every test.
They never touch a real `auth` database; without a test server they are skipped."""
import pytest

from iothing_auth import db, testing

DB = "iothing_auth_test"


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    if testing.server() is None:
        pytest.skip(f"{testing.server.reason}: the tests need a MariaDB (Docker, or IOTHING_TEST_DB=host:port:user:password)")
    db.configure(**testing.fresh_database(DB))
    db._ready = False
    db.settings_cache.clear()
    from iothing_auth import sessions, suite, users
    users.invalidate()
    suite.invalidate()
    sessions.invalidate()
    monkeypatch.setattr("iothing_auth.passwords.SCRYPT", (2 ** 12, 8, 1))  # fast hashes for the tests
    monkeypatch.setattr("iothing_auth.config.ADMIN_USER", "")
    db.init()
    yield
