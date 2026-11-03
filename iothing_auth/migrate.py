"""Fold the apps' own user tables into the shared database.

    iothing-auth migrate --iopix sqlite:///.../viewer.db --ionotes mysql://notes:pw@host/notes --iocontacts mysql://... [--apply]

Without --apply nothing is written: the report says which accounts would be made or merged and where the sources
disagree. Accounts are matched across apps by email, then by name (case-insensitive). When two apps give the same
person different passwords, the first source wins, in this order: iopix, ionotes, ioContacts. Each app's role becomes
the account's role in that app; an admin in every app given becomes a suite admin. Per-app settings, the iopix person
tag, the ioContacts "me" contact and app passwords move along. With --apply the apps' own tables that point at a
user id (iopix viewer_albums.owner_id, ioContacts addressbooks.owner_id, calendar_tokens...) are rewritten to the
shared ids, and the old user tables are renamed (user_accounts_migrated, users_migrated), not dropped.
"""
import json
import sqlite3
import time
from urllib.parse import unquote, urlsplit

from iothing_auth import db, users

ORDER = ("iopix", "ionotes", "iocontacts")
# the apps' tables with a user id column, remapped with --apply
REMAP = {
    "iopix": [("viewer_albums", "owner_id")],
    "ionotes": [],
    "iocontacts": [("addressbooks", "owner_id"), ("calendar_tokens", "user_id"), ("not_duplicates", "user_id"), ("merges", "user_id"),
                   ("suggestions", "by_user_id"), ("suggestions", "decided_by")],
}


class Source:
    """One app's database: sqlite:///path or mysql://user:pw@host[:port]/db."""

    def __init__(self, app: str, url: str):
        self.app, self.url = app, url
        u = urlsplit(url)
        self.kind = "sqlite" if u.scheme.startswith("sqlite") else "mysql"
        if self.kind == "sqlite":
            self.path = unquote(u.path) if u.netloc == "" else unquote(u.netloc + u.path)
            self.con = sqlite3.connect(self.path)
            self.con.row_factory = sqlite3.Row
        else:
            import pymysql
            from pymysql.cursors import DictCursor
            self.con = pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""), password=unquote(u.password or ""),
                                       database=u.path.lstrip("/"), charset="utf8mb4", cursorclass=DictCursor, autocommit=False)

    def q(self, sql: str, args=()) -> list:
        if self.kind == "sqlite":
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]
        with self.con.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), args)
            return cur.fetchall()

    def ex(self, sql: str, args=()):
        if self.kind == "sqlite":
            self.con.execute(sql, args)
        else:
            with self.con.cursor() as cur:
                cur.execute(sql.replace("?", "%s"), args)

    def has_table(self, name: str) -> bool:
        if self.kind == "sqlite":
            return bool(self.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))
        return bool(self.q("SHOW TABLES LIKE ?", (name,)))

    def accounts(self) -> list:
        """Every account as {name, email, display_name, hash, role, settings, data{}, app_passwords[]}."""
        out = []
        if self.app in ("iopix", "ionotes"):
            if not self.has_table("user_accounts"):
                return []
            for r in self.q("SELECT * FROM user_accounts ORDER BY id"):
                data = {}
                if self.app == "iopix" and r.get("person"):
                    data["person"] = int(r["person"])
                out.append({"id": r["id"], "name": r["name"], "email": (r.get("email") or "").strip().lower(), "display_name": "",
                            "hash": r.get("hash") or "", "role": r.get("role") or "viewer", "listed": bool(r.get("listed", 1)),
                            "created": r.get("created"), "last_login": r.get("last_login"),
                            "settings": db.json_load(r.get("settings") or "{}", {}) or {}, "data": data, "app_passwords": []})
        elif self.app == "iocontacts":
            if not self.has_table("users"):
                return []
            for r in self.q("SELECT * FROM users ORDER BY id"):
                data = {}
                if r.get("me_contact_id"):
                    data["me_contact_id"] = int(r["me_contact_id"])
                pws = [{"label": p["label"], "prefix": p["prefix"], "hash": p["pw_hash"], "created": p.get("created"), "last_used": p.get("last_used")}
                       for p in self.q("SELECT * FROM app_passwords WHERE user_id = ?", (r["id"],))] if self.has_table("app_passwords") else []
                out.append({"id": r["id"], "name": r["name"], "email": (r.get("email") or "").strip().lower(), "display_name": r.get("display_name") or "",
                            "hash": r.get("pw_hash") or "", "role": r.get("role") or "editor", "listed": True,
                            "created": r.get("created"), "last_login": None, "settings": {}, "data": data, "app_passwords": pws})
        return out

    def remap(self, idmap: dict):
        """Rewrite the user id columns to the shared ids (two passes: old ids and new ids may overlap)."""
        for table, col in REMAP.get(self.app, []):
            if not self.has_table(table):
                continue
            for old, new in idmap.items():
                self.ex(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (-int(new), int(old)))
            self.ex(f"UPDATE {table} SET {col} = -{col} WHERE {col} < 0")

    def retire(self):
        old = "user_accounts" if self.app in ("iopix", "ionotes") else "users"
        if self.has_table(old) and not self.has_table(old + "_migrated"):
            self.ex(f"ALTER TABLE {old} RENAME TO {old}_migrated")
        if self.app == "iocontacts" and self.has_table("app_passwords") and not self.has_table("app_passwords_migrated"):
            self.ex("ALTER TABLE app_passwords RENAME TO app_passwords_migrated")

    def commit(self):
        self.con.commit()


def _ts(v):
    """A created / last_login value as epoch seconds (sqlite ints, MariaDB datetimes)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(v.timestamp())
    except AttributeError:
        return None


def plan(sources: dict) -> dict:
    """Match the sources' accounts into shared accounts. Returns {accounts: [...], conflicts: [...]}."""
    merged = []  # {name, email, display_name, hash, hash_from, suite_admin, roles{app}, settings{app}, data{app}, app_passwords[app], sources{app: old id}}
    conflicts = []

    def find(a):
        for m in merged:
            if a["email"] and m["email"] == a["email"]:
                return m
        for m in merged:
            if m["name"].lower() == a["name"].lower():
                return m
        return None

    for app in ORDER:
        src = sources.get(app)
        if not src:
            continue
        for a in src.accounts():
            m = find(a)
            if m is None:
                m = {"name": a["name"], "email": a["email"], "display_name": a["display_name"], "hash": a["hash"], "hash_from": app if a["hash"] else None,
                     "listed": a["listed"], "created": _ts(a["created"]), "last_login": _ts(a["last_login"]),
                     "roles": {}, "settings": {}, "data": {}, "app_passwords": {}, "sources": {}}
                merged.append(m)
            else:
                if a["hash"] and m["hash"] and a["hash"] != m["hash"]:
                    conflicts.append(f"{m['name']}: {app} has another password than {m['hash_from']}; {m['hash_from']}'s is kept")
                elif a["hash"] and not m["hash"]:
                    m["hash"], m["hash_from"] = a["hash"], app
                if a["email"] and not m["email"]:
                    m["email"] = a["email"]
                elif a["email"] and m["email"] != a["email"]:
                    conflicts.append(f"{m['name']}: {app} has email {a['email']}, kept {m['email']}")
                if a["display_name"] and not m["display_name"]:
                    m["display_name"] = a["display_name"]
                if a["name"].lower() != m["name"].lower():
                    conflicts.append(f"{m['name']}: matched by email with {app}'s account {a['name']}; the name {m['name']} is kept")
                m["last_login"] = max(filter(None, [m["last_login"], _ts(a["last_login"])]), default=None)
            m["roles"][app] = a["role"] if a["role"] in users.ROLES else "viewer"
            if a["settings"]:
                m["settings"][app] = a["settings"]
            if a["data"]:
                m["data"][app] = a["data"]
            if a["app_passwords"]:
                m["app_passwords"][app] = a["app_passwords"]
            m["sources"][app] = a["id"]
    apps = [a for a in ORDER if a in sources]
    for m in merged:
        m["suite_admin"] = bool(apps) and all(m["roles"].get(a) == "admin" for a in apps)
        if not m["hash"] and any(r != "viewer" for r in m["roles"].values()):
            conflicts.append(f"{m['name']}: no password but a role above viewer ({m['roles']}): lowered to viewer")
            m["roles"] = {a: "viewer" for a in m["roles"]}
            m["suite_admin"] = False
    # two merged accounts could still clash with what the shared database already holds
    for m in merged:
        ex = users.find(m["name"]) or (users.find(m["email"]) if m["email"] else None)
        m["existing"] = ex["id"] if ex else None
    return {"accounts": merged, "conflicts": conflicts}


def apply(sources: dict, p: dict) -> dict:
    """Write the plan: {app: {old id: new id}}."""
    idmaps = {app: {} for app in sources}
    now = int(time.time())
    for m in p["accounts"]:
        if m["existing"]:
            uid = m["existing"]
            if m["hash"] and not db.one("SELECT hash FROM users WHERE id = %s", (uid,))["hash"]:
                db.ex("UPDATE users SET hash = %s WHERE id = %s", (m["hash"], uid))
        else:
            uid = db.ex("INSERT INTO users (name, display_name, email, hash, suite_admin, listed, created, last_login) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (m["name"], m["display_name"] or None, m["email"] or None, m["hash"], 1 if m["suite_admin"] else 0, 1 if m["listed"] else 0,
                         m["created"] or now, m["last_login"]))
        for app, role in m["roles"].items():
            users.set_role(uid, app, role)
        for app, s in m["settings"].items():
            users.set_value(uid, app, users.SETTINGS_KEY, s)
        for app, d in m["data"].items():
            for k, v in d.items():
                users.set_value(uid, app, k, v)
        for app, pws in m["app_passwords"].items():
            for pw in pws:
                db.ex("INSERT INTO app_passwords (user_id, app, label, prefix, hash, created, last_used) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (uid, app, pw["label"], pw["prefix"], pw["hash"], _ts(pw["created"]) or now, _ts(pw["last_used"])))
        for app, old in m["sources"].items():
            idmaps[app][old] = uid
    for app, src in sources.items():
        src.remap(idmaps[app])
        src.retire()
        src.commit()
        db.ex("INSERT INTO meta (k, v) VALUES (%s, %s) ON DUPLICATE KEY UPDATE v = VALUES(v)", (f"migrated:{app}", json.dumps({"at": now, "ids": idmaps[app]})))
    users.invalidate()
    return idmaps


def run(a) -> int:
    sources = {app: Source(app, url) for app, url in (("iopix", a.iopix), ("ionotes", a.ionotes), ("iocontacts", a.iocontacts)) if url}
    if not sources:
        print("nothing to migrate: give --iopix, --ionotes and/or --iocontacts")
        return 2
    users.init()
    p = plan(sources)
    print(f"{len(p['accounts'])} shared account(s) from {', '.join(sources)}:")
    for m in p["accounts"]:
        how = f"merge into existing #{m['existing']}" if m["existing"] else "new"
        print(f"  {m['name']:24} {m['email'] or '-':30} {'suite-admin' if m['suite_admin'] else '':12} password from {m['hash_from'] or 'nobody (one-click)':10}  "
              f"roles {m['roles']}  [{how}; sources {m['sources']}]")
    if p["conflicts"]:
        print("conflicts (resolved as said):")
        for c in p["conflicts"]:
            print("  -", c)
    if a.report:
        with open(a.report, "w") as f:
            json.dump({k: v for k, v in p.items()}, f, indent=2, default=str)
        print(f"report written to {a.report}")
    if not a.apply:
        print("dry run: nothing written (add --apply)")
        return 0
    idmaps = apply(sources, p)
    print("applied; old id -> shared id:", json.dumps(idmaps))
    return 0
