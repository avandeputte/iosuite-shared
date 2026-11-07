// node --test cloudflare/edge-auth/worker.test.mjs  (Node 18+: crypto.subtle, Response and atob are built in)
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker from "./worker.js";

const PW = "ical-3fa91c02-Xx_9aBcDeFgHiJkLmNoPqRsTu";
const kv = new Map([["iocalendar:3fa91c02", JSON.stringify({ h: createHash("sha256").update(PW).digest("hex") })]]);
const env = (mode) => ({
  MODE: mode, APPS: '{"calendar.example.com": "iocalendar"}',
  APP_PASSWORDS: { get: async (k) => (kv.has(k) ? JSON.parse(kv.get(k)) : null) },
});
let forwarded = 0;
globalThis.fetch = async () => { forwarded++; return new Response("origin", { status: 207 }); };
const req = (path, pw, host = "calendar.example.com") => new Request(`https://${host}${path}`, {
  method: "PROPFIND", headers: pw === undefined ? {} : { Authorization: "Basic " + btoa(`alex:${pw}`) } });

test("a known app password goes on", async () => {
  forwarded = 0;
  assert.equal((await worker.fetch(req("/dav/calendars/", PW), env("enforce"))).status, 207);
  assert.equal(forwarded, 1);
});

test("enforce: wrong, unknown, account passwords and no credentials stop at the edge", async () => {
  forwarded = 0;
  for (const pw of [PW + "x", "ical-00000000-whatever", "hunter2", undefined]) {
    const r = await worker.fetch(req("/dav/calendars/", pw), env("enforce"));
    assert.equal(r.status, 401);
    assert.match(r.headers.get("WWW-Authenticate"), /realm="ioCalendar"/);
  }
  assert.equal((await worker.fetch(req("/dav/", PW, "other.example.com"), env("enforce"))).status, 401);
  assert.equal(forwarded, 0);
});

test("monitor lets failures through but answers a request with no credentials", async () => {
  forwarded = 0;
  assert.equal((await worker.fetch(req("/dav/", "hunter2"), env("monitor"))).status, 207);
  assert.equal((await worker.fetch(req("/dav/", undefined), env(undefined))).status, 401);
  assert.equal(forwarded, 1);
});

test("paths outside /dav are not checked", async () => {
  forwarded = 0;
  assert.equal((await worker.fetch(req("/davx", undefined), env("enforce"))).status, 207);
  assert.equal((await worker.fetch(req("/api/events", undefined), env("enforce"))).status, 207);
  assert.equal(forwarded, 2);
});
