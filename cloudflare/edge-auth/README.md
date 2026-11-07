# Edge check of app passwords (Cloudflare Worker)

Optional. When ioCalendar and ioContacts are reached through Cloudflare, this Worker sits on their DAV paths
(`/dav*`) and turns away a request whose app password is wrong before it reaches your server. Someone guessing
passwords against CalDAV or CardDAV then never touches the server, and a WAF rate limit on the same paths stops them
trying fast.

Cloudflare Access can't do this itself: Apple Calendar, Apple Contacts, DAVx5 and Thunderbird send an HTTP Basic
header, which Access doesn't read. That is why `/dav*` has a Bypass policy in Access, and why this Worker is the
check on those paths instead.

## How it works

- Each app publishes a **SHA-256 of every app password** to a Workers KV namespace, keyed `<app>:<prefix>`
  (`iocalendar:3fa91c02`). It does that when the password is made, and on its first use after each start of the app.
  Deleting the password, or the account, removes the key.
- An app password is 144 random bits, so its SHA-256 can't be turned back into it. **Account passwords are never sent
  to Cloudflare.**
- The Worker reads the Basic header, finds the key from the password's 8 hex, compares the hash, and lets the request
  through only on a match. No credentials get the usual 401 sign-in prompt from the Worker itself.
- The server still checks every password itself (scrypt), and so still enforces the CalDAV/CardDAV-only scope. The
  Worker only guards `/dav*`, where every app password of that app is valid, so scope doesn't change its answer. A
  stale key only lets a request reach the server, never in.
- Revoking a password takes up to about a minute to reach every Cloudflare location (KV caching). The server refuses
  it straight away.
- `/.well-known/caldav`, `/.well-known/carddav`, `/ics/*`, `/mcp`, `/api` and the web app are not touched.

What stops working behind the Worker in `enforce` mode: signing a DAV client in with your **account** password. Use
an app password (Settings › Apps and devices), which the Apple profile download already does.

## Set it up

1. **KV namespace.** In the Cloudflare dashboard go to Storage & Databases › KV › Create, name it `APP_PASSWORDS`, and
   copy its id. Or run `npx wrangler kv namespace create APP_PASSWORDS`.
2. **API token** for the apps. Go to My Profile › API Tokens › Create Token › Custom token, with the permission
   *Account › Workers KV Storage › Edit* on your account only. Nothing else is needed.
3. **The apps.** Add to `config.env` of ioCalendar **and** ioContacts, then restart them:

   ```
   CF_KV_ACCOUNT_ID=…        # dashboard › Account home › the account's ⋯ › Copy account ID
   CF_KV_NAMESPACE_ID=…      # from step 1
   CF_KV_API_TOKEN=…         # from step 2
   ```

4. **The Worker.** Copy `wrangler.toml.example` to `wrangler.toml`, put in your two hosts, the zone and the namespace
   id, and run `npx wrangler deploy` in this folder. Without wrangler, create a Worker in the dashboard, paste
   `worker.js`, bind the KV namespace as `APP_PASSWORDS`, add the variables `APPS` and `MODE`, and add the routes
   `calendar.example.com/dav*` and `contacts.example.com/dav*`.
5. **Monitor first.** `MODE = "monitor"` checks and logs (Workers › iosuite-edge-auth › Logs: `"would block"`) but lets
   everything through. Leave it like that until each of your devices has synced once, so passwords made before step 3
   get published. Then check from an app's folder:

   ```
   .venv/bin/iosuite-shared --config . edge          # keys at the edge vs app passwords; "not at the edge yet" lines
   .venv/bin/iosuite-shared --config . edge --apply  # also remove keys whose password is gone
   ```

   A password still listed as missing belongs to a device that hasn't synced. Open it, or revoke the password and
   make a new one.
6. **Enforce.** Set `MODE = "enforce"` and deploy again.

## Rate limit (recommended)

Under Security › WAF › Rate limiting rules › Create rule:

- **If** `(http.host in {"calendar.example.com" "contacts.example.com"} and starts_with(http.request.uri.path, "/dav"))`
- **Count** by IP. **Rate:** 60 requests per 10 seconds on the Free plan (its one rule allows a 10-second period). On
  a paid plan, use 300 per minute.
- **Then** Block for 10 seconds (Free) or 10 minutes.

A phone syncing a calendar sends a burst of tens of requests, so keep the limit well above that. With
the Worker enforcing, failed attempts cost nothing at the server anyway; the rule just stops the Worker being hammered
too. To narrow it to failures only, Pro and above can match on `http.response.code eq 401`.

Optionally, add a custom WAF rule that blocks `/dav*` from outside the countries you live in and travel to.

## Costs

The Workers Free plan includes 100,000 requests and 100,000 KV reads a day, and 1,000 KV writes. A family's phones and
computers syncing every 15 minutes stay far below that. KV reads are cached for 60 seconds at each location.

## Test

```
node --test cloudflare/edge-auth/worker.test.mjs
```
