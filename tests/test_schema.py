"""0.8.0: a Shared ioSuite database from before the naming convention is renamed at start, every row kept."""
from iosuite_shared import db, schema, sessions, suite, users

OLD = [  # the 0.7.0 schema (meta, unnamed keys, created/expires/k/v...)
    "CREATE TABLE meta (k VARCHAR(64) PRIMARY KEY, v MEDIUMTEXT)",
    "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64) NOT NULL, display_name VARCHAR(128) NULL, "
    "email VARCHAR(191) NULL, hash VARCHAR(255) NOT NULL DEFAULT '', suite_admin TINYINT NOT NULL DEFAULT 0, listed TINYINT NOT NULL DEFAULT 1, "
    "created BIGINT NOT NULL, last_login BIGINT NULL, session_gen INT NOT NULL DEFAULT 0, UNIQUE KEY (name), UNIQUE KEY (email)) CHARACTER SET utf8mb4",
    "CREATE TABLE user_roles (user_id INT NOT NULL, app VARCHAR(32) NOT NULL, role ENUM('none','viewer','editor','admin') NOT NULL, "
    "PRIMARY KEY (user_id, app), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE) CHARACTER SET latin1",
    "CREATE TABLE user_data (user_id INT NOT NULL, app VARCHAR(32) NOT NULL, k VARCHAR(64) NOT NULL, v MEDIUMTEXT NOT NULL, "
    "PRIMARY KEY (user_id, app, k), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE) CHARACTER SET utf8mb4",
    "CREATE TABLE sessions (token_hash CHAR(64) PRIMARY KEY, user_id INT NOT NULL, app VARCHAR(32) NOT NULL, via VARCHAR(16) NOT NULL, "
    "created BIGINT NOT NULL, expires BIGINT NOT NULL, renewed BIGINT NOT NULL, ip VARCHAR(64) NULL, cloudflare TINYINT NOT NULL DEFAULT 0, "
    "client VARCHAR(32) NOT NULL DEFAULT '', user_agent VARCHAR(255) NULL, last_seen BIGINT NULL, last_ip VARCHAR(64) NULL, "
    "INDEX (user_id), INDEX (expires), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE) CHARACTER SET latin1",
    "CREATE TABLE app_passwords (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT NOT NULL, app VARCHAR(32) NOT NULL, label VARCHAR(128) NOT NULL, "
    "prefix CHAR(8) NOT NULL, hash VARCHAR(255) NOT NULL, created BIGINT NOT NULL, last_used BIGINT NULL, INDEX (prefix), "
    "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE) CHARACTER SET utf8mb4",
    "CREATE TABLE settings (app VARCHAR(32) NOT NULL, k VARCHAR(64) NOT NULL, v MEDIUMTEXT NOT NULL, PRIMARY KEY (app, k)) CHARACTER SET utf8mb4",
    "CREATE TABLE suite_apps (app VARCHAR(32) PRIMARY KEY, name VARCHAR(64) NOT NULL, description VARCHAR(255) NULL, version VARCHAR(32) NULL, "
    "color VARCHAR(16) NULL, icon VARCHAR(512) NULL, configured_url VARCHAR(512) NULL, seen_url VARCHAR(512) NULL, registered BIGINT NOT NULL, "
    "updated BIGINT NOT NULL, last_seen BIGINT NULL) CHARACTER SET utf8mb4",
]
ROWS = [
    "INSERT INTO meta VALUES ('schema', '3'), ('migrated:ionotes', '{\"at\": 5}')",
    "INSERT INTO users (id, name, email, hash, suite_admin, created, last_login) VALUES (1, 'alex', 'alex@example.com', '', 1, 100, 200)",
    "INSERT INTO user_roles VALUES (1, 'iopix', 'admin')",
    "INSERT INTO user_data VALUES (1, 'iopix', 'person', '42')",
    "INSERT INTO sessions (token_hash, user_id, app, via, created, expires, renewed, last_seen) VALUES "
    "(SHA2('tok', 256), 1, 'iopix', 'password', 100, 9999999999, 100, 150)",
    "INSERT INTO app_passwords (user_id, app, label, prefix, hash, created, last_used) VALUES (1, 'iocontacts', 'phone', 'abcd1234', 'x', 100, 300)",
    "INSERT INTO settings VALUES ('', 'require_login', '1')",
    "INSERT INTO suite_apps (app, name, registered, updated, last_seen) VALUES ('iopix', 'ioPix', 100, 110, 120)",
]


def _old_database():
    with db.tx() as con, con.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for t in schema.tables(cur):
            cur.execute(f"DROP TABLE `{t}`")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        for stmt in OLD + ROWS:
            cur.execute(stmt)
    db._ready = False
    db.settings_cache.clear()
    users.invalidate()
    suite.invalidate()
    sessions.invalidate()


def test_old_database_renamed_with_every_row():
    _old_database()
    db.init()
    db.init()  # twice: nothing left to do
    with db.tx() as con, con.cursor() as cur:
        assert schema.tables(cur) == {"ios_users", "ios_user_roles", "ios_user_data", "ios_sessions", "ios_app_passwords",
                                      "ios_settings", "ios_apps", "ios_credentials", "ios_schema_version"}
        assert schema.current_version(cur, "ios") == db.SCHEMA_VERSION
        assert "uq_ios_users_email" in schema.indexes(cur, "ios_users")
        assert {"idx_ios_sessions_user", "idx_ios_sessions_expires"} <= schema.indexes(cur, "ios_sessions")
        cur.execute("SELECT table_collation AS c FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'ios_sessions'")
        assert cur.fetchone()["c"].startswith("utf8mb4")
    u = users.find("alex")
    assert u["created"] == 100 and u["last_login"] == 200 and u["suiteAdmin"]
    assert users.roles_of(1) == {"iopix": "admin"} and users.get_value(1, "iopix", "person") == 42
    s = sessions.list_sessions(1)
    assert len(s) == 1 and s[0]["created"] == 100 and s[0]["lastSeen"] == 150
    assert db.get_setting("require_login") == "1"
    assert suite.get("iopix")["registered"] == 100 and suite.get("iopix")["lastSeen"] == 120
    assert db.one("SELECT value FROM ios_settings WHERE app = '_migrate' AND name = 'migrated:ionotes'")["value"] == '{"at": 5}'
    assert db.one("SELECT last_used_at FROM ios_app_passwords")["last_used_at"] == 300
    db.ex("DELETE FROM ios_users WHERE id = 1")
    assert db.one("SELECT count(*) AS n FROM ios_sessions")["n"] == 0


def test_fresh_database_is_already_named():
    with db.tx() as con, con.cursor() as cur:
        assert schema.rename(cur, db.RENAME_TABLES, db.RENAME_COLUMNS, db.RENAME_INDEXES) == []
        assert "idx_ios_app_passwords_user" in schema.indexes(cur, "ios_app_passwords")
