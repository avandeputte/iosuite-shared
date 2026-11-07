"""Set up the edge check in Cloudflare in one go, or check that it is (README.md, steps 1, 2 and 4, and the rate limit).
Every step finds what is already there and only makes what is missing, so it can be run again at any time.

    export CLOUDFLARE_API_TOKEN=…  CLOUDFLARE_ACCOUNT_ID=…
    python3 setup.py --host calendar.example.com=iocalendar --host contacts.example.com=iocontacts \\
        [--mode monitor|enforce] [--deploy] [--rate-limit] [--app-token-file ~/.iosuite-edge-kv-token] [--dry-run]

1. the Workers KV namespace APP_PASSWORDS (made when missing)
2. wrangler.toml in this folder (gitignored): the routes <host>/dav*, the namespace, APPS and MODE
3. --deploy: `npx --yes wrangler@4 deploy`
4. --rate-limit: the zone's rule "ioSuite DAV rate limit" on /dav* of those hosts, 60 requests per 10 s per IP and
   location, block 10 s (the Free plan's limits); other rate-limit rules are left as they are
5. --app-token-file: the apps' API token "iosuite-edge-kv" (Account › Workers KV Storage › Read + Write on this account
   only), written to that file (mode 600). Never printed. An existing token of that name is left alone: Cloudflare
   shows a token's value only once, so delete it in the dashboard to have a new one made.

Then store the settings in the Shared ioSuite database, from an app's folder:

    .venv/bin/iosuite-shared --config . edge --account-id … --namespace-id … --token-file <that file>

The admin token needs: Account › Workers Scripts, Workers KV Storage: Edit, Account Settings: Read; Zone › Zone: Read,
Workers Routes, Zone WAF: Edit; User › Memberships: Read, API Tokens: Edit (the last only for --app-token-file).
Standard library only.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
HERE = pathlib.Path(__file__).resolve().parent
NAMESPACE = "APP_PASSWORDS"
RULE = "ioSuite DAV rate limit"
TOKEN_NAME = "iosuite-edge-kv"
dry = False


def call(method: str, path: str, body=None, ok_missing: bool = False):
    """One Cloudflare API call; the result, or None for a missing entrypoint with ok_missing."""
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["result"]
    except urllib.error.HTTPError as e:
        j = json.loads(e.read() or b"{}")
        if ok_missing and e.code == 404:
            return None
        sys.exit(f"{method} {path}: HTTP {e.code} {'; '.join(x.get('message', '') for x in j.get('errors', []))}")


def change(what: str) -> bool:
    print(("would " if dry else "") + what)
    return not dry


def zone_of(hosts) -> dict:
    """The one zone all hosts are in (the longest zone name each host ends with)."""
    zones = call("GET", "/zones?per_page=50")
    found = set()
    for h in hosts:
        z = max((z for z in zones if h == z["name"] or h.endswith("." + z["name"])), key=lambda z: len(z["name"]), default=None)
        if not z:
            sys.exit(f"{h}: no zone of this token's account holds it")
        found.add((z["id"], z["name"]))
    if len(found) != 1:
        sys.exit("the hosts are in different zones: " + ", ".join(n for _, n in found))
    zid, name = found.pop()
    return {"id": zid, "name": name}


def namespace(account: str) -> str:
    for ns in call("GET", f"/accounts/{account}/storage/kv/namespaces?per_page=100"):
        if ns["title"] == NAMESPACE:
            print(f"KV namespace {NAMESPACE}: {ns['id']}")
            return ns["id"]
    if not change(f"create the KV namespace {NAMESPACE}"):
        return "<new namespace id>"
    ns = call("POST", f"/accounts/{account}/storage/kv/namespaces", {"title": NAMESPACE})
    print(f"KV namespace {NAMESPACE}: {ns['id']} (new)")
    return ns["id"]


def wrangler_toml(apps: dict, zone: str, ns: str, mode: str):
    routes = "".join(f'  {{ pattern = "{h}/dav*", zone_name = "{zone}" }},\n' for h in apps)
    text = f'''name = "iosuite-edge-auth"
main = "worker.js"
compatibility_date = "2026-10-01"

# the DAV paths only: the web app, /api, /mcp, /ics and /.well-known go straight to the server as before
routes = [
{routes}]

[[kv_namespaces]]
binding = "APP_PASSWORDS"
id = "{ns}"

[vars]
APPS = '{json.dumps(apps)}'
MODE = "{mode}"                   # "enforce" once `iosuite-shared edge` shows no password missing

[observability]
enabled = true                     # the "would block" / "blocked" lines in Workers Logs
'''
    path = HERE / "wrangler.toml"
    if path.exists() and path.read_text() == text:
        print("wrangler.toml: up to date")
    elif change("write wrangler.toml" + (" (replacing the one there)" if path.exists() else "")):
        path.write_text(text)


def deploy():
    if change("deploy the Worker (npx --yes wrangler@4 deploy)"):
        subprocess.run(["npx", "--yes", "wrangler@4", "deploy"], cwd=HERE, check=True)


def rate_limit(zone: dict, hosts):
    rule = {"description": RULE, "action": "block", "enabled": True,
            "expression": "(http.host in {%s} and starts_with(http.request.uri.path, \"/dav\"))" % " ".join(f'"{h}"' for h in hosts),
            "ratelimit": {"characteristics": ["ip.src", "cf.colo.id"], "period": 10, "requests_per_period": 60,
                          "mitigation_timeout": 10}}
    base = f"/zones/{zone['id']}/rulesets/phases/http_ratelimit/entrypoint"
    current = call("GET", base, ok_missing=True)
    rules = (current or {}).get("rules", [])
    mine = next((r for r in rules if r.get("description") == RULE), None)
    keep = ("description", "action", "enabled", "expression", "ratelimit")
    if mine and {k: mine.get(k) for k in keep} == rule:
        print(f"rate limit rule: up to date ({mine['id']})")
        return
    if not change(("update" if mine else "add") + f" the rate limit rule {RULE!r} on {zone['name']}"):
        return
    others = [{k: r[k] for k in ("id", "description", "action", "enabled", "expression", "ratelimit", "action_parameters")
               if k in r} for r in rules if r is not mine]
    out = call("PUT", base, {"rules": others + [dict(rule, **({"id": mine["id"]} if mine else {}))]})
    print(f"rate limit rule: done (ruleset {out['id']}, {len(out['rules'])} rule(s))")


def app_token(account: str, path: pathlib.Path):
    if any(t["name"] == TOKEN_NAME for t in call("GET", "/user/tokens?per_page=100")):
        print(f"API token {TOKEN_NAME}: already there (its value is only shown once: delete it to have a new one made)")
        return
    groups = {g["name"]: g["id"] for g in call("GET", "/user/tokens/permission_groups")}
    need = ["Workers KV Storage Write", "Workers KV Storage Read"]
    if any(n not in groups for n in need):
        sys.exit("Cloudflare has no permission group " + ", ".join(n for n in need if n not in groups))
    if not change(f"create the API token {TOKEN_NAME} and write it to {path}"):
        return
    t = call("POST", "/user/tokens", {"name": TOKEN_NAME, "policies": [{
        "effect": "allow", "resources": {f"com.cloudflare.api.account.{account}": "*"},
        "permission_groups": [{"id": groups[n]} for n in need]}]})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t["value"] + "\n")
    os.chmod(path, 0o600)
    print(f"API token {TOKEN_NAME}: written to {path}")


def main():
    global dry
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", action="append", required=True, metavar="HOST=APP",
                   help="a public host and its app (iocalendar, iocontacts); once per host")
    p.add_argument("--mode", choices=("monitor", "enforce"), default="monitor")
    p.add_argument("--deploy", action="store_true", help="run wrangler deploy")
    p.add_argument("--rate-limit", action="store_true", help="add or update the WAF rate limit rule")
    p.add_argument("--app-token-file", type=pathlib.Path, help="create the apps' KV token and write it here")
    p.add_argument("--dry-run", action="store_true", help="only say what would change")
    a = p.parse_args()
    dry = a.dry_run
    for v in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        if not os.environ.get(v):
            sys.exit(f"set {v}")
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    apps = {}
    for h in a.host:
        host, _, app = h.partition("=")
        if not host or not app:
            sys.exit(f"--host {h}: give HOST=APP")
        apps[host.strip().lower()] = app.strip()
    zone = zone_of(apps)
    print(f"zone {zone['name']}: {zone['id']}")
    ns = namespace(account)
    wrangler_toml(apps, zone["name"], ns, a.mode)
    if a.deploy:
        deploy()
    if a.rate_limit:
        rate_limit(zone, list(apps))
    if a.app_token_file:
        app_token(account, a.app_token_file.expanduser())
    print(f"\nthen, from an app's folder: .venv/bin/iosuite-shared --config . edge --account-id {account} --namespace-id {ns}"
          + (f" --token-file {a.app_token_file}" if a.app_token_file else " --token-file <the apps' token file>"))


if __name__ == "__main__":
    main()
