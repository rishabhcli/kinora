import test from "node:test";
import assert from "node:assert/strict";
import { IpcRouter, v } from "../../dist-electron/core/ipc-router.js";
import { INVOKE_CHANNELS } from "../../dist-electron/shared/ipc-contract.js";

const ctx = { senderId: 1, senderUrl: "file:///app/index.html" };

test("dispatch routes a valid call to its handler", async () => {
  const router = new IpcRouter();
  router.handle("kinora:pick-book", () => "/books/a.pdf");
  const res = await router.dispatch("kinora:pick-book", undefined, ctx);
  assert.deepEqual(res, { ok: true, value: "/books/a.pdf" });
});

test("dispatch rejects unknown channels", async () => {
  const router = new IpcRouter();
  const res = await router.dispatch("kinora:evil", {}, ctx);
  assert.equal(res.ok, false);
  assert.equal(res.error.code, "unknown-channel");
});

test("dispatch rejects forbidden origins", async () => {
  const router = new IpcRouter({ allowedOriginPrefixes: ["file://"] });
  router.handle("kinora:pick-book", () => null);
  const res = await router.dispatch("kinora:pick-book", undefined, {
    senderId: 2,
    senderUrl: "http://evil.example/",
  });
  assert.equal(res.ok, false);
  assert.equal(res.error.code, "forbidden-origin");
});

test("dispatch rejects invalid payloads via validator", async () => {
  const router = new IpcRouter();
  router.handle("kinora:notify", () => undefined, v.notify);
  const bad = await router.dispatch("kinora:notify", { title: 1 }, ctx);
  assert.equal(bad.ok, false);
  assert.equal(bad.error.code, "invalid-payload");
  const good = await router.dispatch("kinora:notify", { title: "T", body: "B" }, ctx);
  assert.equal(good.ok, true);
});

test("dispatch wraps handler throws as structured error (never rejects)", async () => {
  const router = new IpcRouter();
  router.handle("kinora:diagnostics", () => {
    throw new Error("boom");
  });
  const res = await router.dispatch("kinora:diagnostics", undefined, ctx);
  assert.equal(res.ok, false);
  assert.equal(res.error.code, "handler-error");
  assert.equal(res.error.message, "boom");
});

test("dispatch reports no-handler for an allowlisted-but-unregistered channel", async () => {
  const router = new IpcRouter();
  const res = await router.dispatch("kinora:system:state", undefined, ctx);
  assert.equal(res.ok, false);
  assert.equal(res.error.code, "no-handler");
});

test("handle rejects duplicate registration and non-allowlisted names", () => {
  const router = new IpcRouter();
  router.handle("kinora:pick-book", () => null);
  assert.throws(() => router.handle("kinora:pick-book", () => null), /duplicate/);
  assert.throws(() => router.handle("kinora:nope", () => null), /not an allowlisted/);
});

test("isComplete / missing track coverage of the allowlist", () => {
  const router = new IpcRouter();
  for (const ch of INVOKE_CHANNELS) router.handle(ch, () => undefined);
  assert.equal(router.isComplete(INVOKE_CHANNELS), true);
  assert.deepEqual(router.missing(INVOKE_CHANNELS), []);
});

test("missing lists unregistered channels", () => {
  const router = new IpcRouter();
  router.handle("kinora:pick-book", () => null);
  const missing = router.missing(INVOKE_CHANNELS);
  assert.ok(missing.includes("kinora:notify"));
  assert.ok(!missing.includes("kinora:pick-book"));
});

test("validators accept/reject expected shapes", () => {
  assert.equal(v.void(undefined), true);
  assert.equal(v.void(null), true);
  assert.equal(v.void(5), false);
  assert.equal(v.tokenSet({ token: null }), true);
  assert.equal(v.tokenSet({ token: "abc" }), true);
  assert.equal(v.tokenSet({ token: 5 }), false);
  assert.equal(v.prefsGet({ key: "x" }), true);
  assert.equal(v.prefsGet({}), false);
  assert.equal(v.prefsSet({ key: "x", value: 1 }), true);
  assert.equal(v.prefsSet({ key: "x" }), false);
  assert.equal(v.logsTail(undefined), true);
  assert.equal(v.logsTail({ limit: 10 }), true);
  assert.equal(v.logsTail({ limit: "x" }), false);
  assert.equal(v.windowOpen({ route: "/x" }), true);
  assert.equal(v.windowOpen({ route: 1 }), false);
  assert.equal(v.openExternal({ url: "https://x" }), true);
  assert.equal(v.openExternal({}), false);
});

test("isOriginAllowed honours default + custom prefixes", () => {
  const router = new IpcRouter();
  assert.equal(router.isOriginAllowed("http://localhost:5173/x"), true);
  assert.equal(router.isOriginAllowed("file:///app"), true);
  assert.equal(router.isOriginAllowed("http://evil/"), false);
  assert.equal(router.isOriginAllowed(""), false);
});
