"""A fresh Shared ioSuite database gets every table, with the suite's names, and records its schema version."""
from iosuite_shared import db, schema


def test_fresh_database_has_every_table():
    db.init()
    with db.tx() as con, con.cursor() as cur:
        assert {"ios_users", "ios_user_roles", "ios_user_data", "ios_sessions", "ios_app_passwords", "ios_settings",
                "ios_apps", "ios_credentials", "ios_schema_version"} <= schema.tables(cur)
        assert schema.current_version(cur, "ios") == db.SCHEMA_VERSION
        assert "uq_ios_users_email" in schema.indexes(cur, "ios_users")
        assert "idx_ios_app_passwords_user" in schema.indexes(cur, "ios_app_passwords")
        assert {"ip", "last_seen_at", "last_ip"} <= schema.columns(cur, "ios_sessions")
