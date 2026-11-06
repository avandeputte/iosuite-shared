"""iosuite-shared: init | check | list | create | admin | role | password | migrate (see migrate.py) | install (see install.py) | provision (see provision.py).

Every app's setup.sh makes its first admin with `iosuite-shared admin NAME --app APP` (the password from ADMIN_PASSWORD,
else asked for) and checks the Shared ioSuite database with `iosuite-shared check`; run them with the app's config.env
loaded (`--config FOLDER` reads FOLDER/config.env first).
"""
import argparse
import getpass
import os
import sys


def main(argv=None):
    p = argparse.ArgumentParser(prog="iosuite-shared", description="The shared ioThing user database")
    sub = p.add_subparsers(dest="cmd", required=True)
    p.add_argument("--config", default=None, help="a folder whose config.env is read first (an app's folder)")
    sub.add_parser("init", help="create the database tables")
    sub.add_parser("check", help="connect to the Shared ioSuite database and count the accounts")
    ad = sub.add_parser("admin", help="make NAME an admin of APP (created when missing; the first account is a suite admin)")
    ad.add_argument("name"); ad.add_argument("--app", required=True)
    sub.add_parser("list", help="every account and its roles")
    c = sub.add_parser("create", help="a new account (suite admin with --suite-admin)")
    c.add_argument("name"); c.add_argument("--email", default=""); c.add_argument("--password", default=None); c.add_argument("--suite-admin", action="store_true")
    c.add_argument("--app", default=None); c.add_argument("--role", default=None)
    r = sub.add_parser("role", help="set an account's role in an app")
    r.add_argument("name"); r.add_argument("app"); r.add_argument("role", choices=("none", "viewer", "editor", "admin"))
    pw = sub.add_parser("password", help="set an account's password")
    pw.add_argument("name"); pw.add_argument("--password", default=None)
    m = sub.add_parser("migrate", help="fold the apps' own user tables into the shared database (dry run unless --apply)")
    m.add_argument("--iopix", help="iopix's viewer database: sqlite:///path/viewer.db or mysql://user:pw@host/db")
    m.add_argument("--ionotes", help="ionotes' database: mysql://user:pw@host/db or sqlite:///path")
    m.add_argument("--iocontacts", help="ioContacts' database: mysql://user:pw@host/db")
    m.add_argument("--apply", action="store_true", help="write the accounts (and remap the apps' own user ids)")
    m.add_argument("--report", default=None, help="write the report (JSON) here")
    m.add_argument("--keep-old", action="store_true", help="leave the apps' own user tables in place (old versions keep working); `retire` renames them later")
    rt = sub.add_parser("retire", help="rename the apps' own user tables to *_migrated (after migrate --keep-old)")
    rt.add_argument("--iopix"); rt.add_argument("--ionotes"); rt.add_argument("--iocontacts")
    from iosuite_shared import install, provision
    install.add_parser(sub)
    provision.add_parser(sub)
    a = p.parse_args(argv)
    if a.config:
        from iosuite_shared import config
        config.load_file(a.config)

    if a.cmd == "install":  # no database needed here: the companions' setup.sh check theirs
        return install.main(a)
    if a.cmd == "provision":  # connects as the administrator, not as the app
        return provision.main(a)

    from iosuite_shared import users
    if a.cmd == "migrate":
        from iosuite_shared import migrate
        return migrate.run(a)
    if a.cmd == "retire":
        from iosuite_shared import migrate
        return migrate.retire(a)
    users.init()
    if a.cmd == "init":
        print("ok")
    elif a.cmd == "check":
        from iosuite_shared import config
        print(f"ok: {config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}, {len(users.list_users())} account(s)")
    elif a.cmd == "admin":
        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass(f"Password for {a.name}: ")
        u = users.find(a.name)
        try:
            if u:
                users.update(u["id"], password=password or None, role="admin", app=a.app)
                print(f"ok: {a.name} is an admin of {a.app}")
            else:
                first = not users.any_users()
                users.create(a.name, password, "admin", a.app, suite_admin=first)
                print(f"ok: created {a.name}" + (" (suite admin)" if first else ""))
        except ValueError as e:
            sys.exit(f"error: {e}")
    elif a.cmd == "list":
        for u in users.list_users():
            roles = ", ".join(f"{k}={v}" for k, v in users.roles_of(u["id"]).items()) or "-"
            print(f"{u['id']:4} {u['name']:24} {u['email'] or '':30} {'suite-admin' if u['suiteAdmin'] else '':12} {'(no password)' if u['passwordless'] else '':14} {roles}")
    elif a.cmd == "create":
        password = a.password if a.password is not None else getpass.getpass("Password (empty: one-click viewer): ")
        print(users.create(a.name, password, a.role, a.app, email=a.email, suite_admin=a.suite_admin))
    elif a.cmd == "role":
        u = users.find(a.name)
        if not u:
            sys.exit(f"no account {a.name}")
        users.set_role(u["id"], a.app, a.role)
        print(users.roles_of(u["id"]))
    elif a.cmd == "password":
        u = users.find(a.name)
        if not u:
            sys.exit(f"no account {a.name}")
        users.update(u["id"], password=a.password if a.password is not None else getpass.getpass("New password: "))
        print("ok")


if __name__ == "__main__":
    main()
