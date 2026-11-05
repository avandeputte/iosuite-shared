import argparse
import json
import sqlite3

from iosuite_shared import migrate, users
from iosuite_shared.passwords import hash_password


def _iopix_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE user_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, hash TEXT NOT NULL, role TEXT, settings TEXT DEFAULT '{}',
            created INTEGER, last_login INTEGER, person INTEGER, email TEXT, session_gen INTEGER DEFAULT 0, listed INTEGER DEFAULT 1);
        CREATE TABLE viewer_albums (id INTEGER PRIMARY KEY, name TEXT, owner_id INTEGER);
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO app_settings VALUES ('require_login', '1'), ('auth_proxy', 'cloudflare'), ('cf_access_team', 'x.cloudflareaccess.com'), ('cf_access_aud', 'aud1');
    """)
    con.execute("INSERT INTO user_accounts (name, hash, role, settings, created, email, person) VALUES ('alex', ?, 'admin', '{\"theme\":\"dark\"}', 1000, 'alex@example.com', 7)", (hash_password("alexpass1"),))
    con.execute("INSERT INTO user_accounts (name, hash, role, created) VALUES ('kid', '', 'viewer', 1001)")
    con.execute("INSERT INTO user_accounts (name, hash, role, created) VALUES ('bob', ?, 'editor', 1002)", (hash_password("bobpass12"),))
    con.execute("INSERT INTO viewer_albums (id, name, owner_id) VALUES (1, 'mine', 1), (2, 'bobs', 3), (3, 'shared', NULL)")
    con.commit(); con.close()


def _ionotes_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE user_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, hash TEXT NOT NULL, role TEXT, settings TEXT DEFAULT '{}',
            created INTEGER, last_login INTEGER, email TEXT, session_gen INTEGER DEFAULT 0, listed INTEGER DEFAULT 1);
    """)
    # the same person as iopix's alex (by email), another name and password; bob an admin here
    con.execute("INSERT INTO user_accounts (name, hash, role, created, email) VALUES ('Alexandre', ?, 'admin', 2000, 'ALEX@example.com')", (hash_password("other1234"),))
    con.execute("INSERT INTO user_accounts (name, hash, role, created) VALUES ('bob', ?, 'admin', 2001)", (hash_password("bobpass12"),))
    con.commit(); con.close()


def test_migrate_dry_run_then_apply(tmp_path, capsys):
    pix, notes = tmp_path / "viewer.db", tmp_path / "notes.db"
    _iopix_db(pix); _ionotes_db(notes)
    args = argparse.Namespace(iopix=f"sqlite:///{pix}", ionotes=f"sqlite:///{notes}", iocontacts=None, apply=False, report=str(tmp_path / "r.json"))
    assert migrate.run(args) == 0
    assert not users.any_users()
    rep = json.loads((tmp_path / "r.json").read_text())
    assert len(rep["accounts"]) == 3
    assert any("another password" in c for c in rep["conflicts"])
    args.apply = True
    assert migrate.run(args) == 0
    alex = users.find("alex")
    assert alex["suiteAdmin"] and alex["email"] == "alex@example.com"
    assert users.authenticate("alex", "alexpass1") and not users.authenticate("alex", "other1234")  # iopix's password won
    assert users.roles_of(alex["id"]) == {"iopix": "admin", "ionotes": "admin"}
    assert users.get_settings(alex["id"], "iopix") == {"theme": "dark"} and users.get_value(alex["id"], "iopix", "person") == 7
    bob = users.find("bob")
    assert not bob["suiteAdmin"] and users.roles_of(bob["id"]) == {"iopix": "editor", "ionotes": "admin"}
    kid = users.find("kid")
    assert kid["passwordless"] and users.roles_of(kid["id"]) == {"iopix": "viewer"}
    con = sqlite3.connect(pix)
    assert con.execute("SELECT owner_id FROM viewer_albums WHERE id = 1").fetchone()[0] == alex["id"]
    assert con.execute("SELECT owner_id FROM viewer_albums WHERE id = 2").fetchone()[0] == bob["id"]
    assert con.execute("SELECT owner_id FROM viewer_albums WHERE id = 3").fetchone()[0] is None
    assert con.execute("SELECT count(*) FROM user_accounts_migrated").fetchone()[0] == 3
    from iosuite_shared import db as authdb
    assert users.require_login("iopix") and not users.require_login("ionotes")  # require_login moved per app
    assert authdb.settings_of("") == {"auth_proxy": "cloudflare", "cf_access_team": "x.cloudflareaccess.com", "cf_access_aud": "aud1"}  # the proxy, suite-wide
