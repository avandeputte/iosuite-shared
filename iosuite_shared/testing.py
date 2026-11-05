"""The MariaDB the test suites run against: never a real server unless asked.

The library's own tests and the apps' suites need a MariaDB for the Shared ioSuite database. Two ways, the same as iopix:

- IOSUITE_TEST_DB=host:port:user:password names one (CI uses a MariaDB service this way). The user must be allowed
  to CREATE and DROP databases: every run starts from empty databases of its own.
- Otherwise a container is started with Docker, `iosuite-test-mariadb` on a free local port, and left running for
  the next run (`docker rm -f iosuite-test-mariadb` throws it away). Without Docker and without IOSUITE_TEST_DB the
  suite is skipped with a message that says so.

Nothing here ever touches the suite's live Shared ioSuite database: the test databases are made on that test server and
dropped at the start of every session.
"""
import os
import socket
import subprocess
import time

CONTAINER = "iosuite-test-mariadb"
IMAGE = "mariadb:11"
ROOT_PASSWORD = "iosuite-test"
_server = None


def server() -> dict:
    """{host, port, user, password} of a reachable MariaDB, or None (with the reason in `server.reason`)."""
    global _server
    if _server is not None:
        return _server or None
    given = os.environ.get("IOSUITE_TEST_DB", "")
    if given:
        host, port, user, password = (given.split(":", 3) + ["", "", ""])[:4]
        c = {"host": host, "port": int(port or 3306), "user": user or "root", "password": password}
    else:
        c = _docker()
    if c and _wait(c):
        _server = c
        return c
    _server = {}
    return None


server.reason = ""


def _docker():
    try:
        out = subprocess.run(["docker", "port", CONTAINER, "3306/tcp"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        server.reason = f"no Docker ({e}) and no IOSUITE_TEST_DB"
        return None
    if out.returncode != 0:  # not running: start it (a stopped one of the same name is removed first)
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, timeout=30)
        run = subprocess.run(["docker", "run", "-d", "--name", CONTAINER, "-p", "127.0.0.1:0:3306",
                              "-e", f"MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}", IMAGE], capture_output=True, text=True, timeout=600)
        if run.returncode != 0:
            server.reason = "docker run failed: " + run.stderr.strip()[-300:]
            return None
        out = subprocess.run(["docker", "port", CONTAINER, "3306/tcp"], capture_output=True, text=True, timeout=20)
    port = int(out.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
    return {"host": "127.0.0.1", "port": port, "user": "root", "password": ROOT_PASSWORD}


def _wait(c: dict, seconds: float = 90) -> bool:
    import pymysql
    end = time.time() + seconds
    while time.time() < end:
        try:
            con = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"], connect_timeout=3)
            con.close()
            return True
        except (pymysql.err.Error, OSError, socket.error) as e:
            server.reason = f"MariaDB at {c['host']}:{c['port']} does not answer: {e}"
            time.sleep(0.5)
    return False


def fresh_database(name: str) -> dict:
    """Drop and create `name` on the test server; the connection settings for it."""
    import pymysql
    c = server()
    con = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"], connect_timeout=5)
    try:
        with con.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        con.commit()
    finally:
        con.close()
    return {"host": c["host"], "port": c["port"], "database": name, "user": c["user"], "password": c["password"]}


def auth_env(name: str) -> dict:
    """A fresh Shared ioSuite database for a test session, as the IOSUITE_DB_* variables an app process reads."""
    s = fresh_database(name)
    out = {}
    for k, v in s.items():
        key = "NAME" if k == "database" else k.upper()
        out["IOSUITE_DB_" + key] = str(v)
    return out
