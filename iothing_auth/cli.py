"""iothing-auth: init | list | create | role | password | migrate (see migrate.py)."""
import argparse
import getpass
import sys


def main(argv=None):
    p = argparse.ArgumentParser(prog="iothing-auth", description="The shared ioThing user database")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database tables")
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
    a = p.parse_args(argv)

    from iothing_auth import users
    if a.cmd == "migrate":
        from iothing_auth import migrate
        return migrate.run(a)
    if a.cmd == "retire":
        from iothing_auth import migrate
        return migrate.retire(a)
    users.init()
    if a.cmd == "init":
        print("ok")
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
