import test from "node:test";
import assert from "node:assert/strict";
import { DeepLinkQueue } from "../../dist-electron/core/deep-link-queue.js";

const link = (action) => ({ action, segments: [], params: {}, href: `kinora://${action}` });

test("links offered before ready are queued, not delivered", () => {
  const delivered = [];
  const q = new DeepLinkQueue((l) => delivered.push(l.action));
  q.offer(link("book"));
  q.offer(link("library"));
  assert.equal(delivered.length, 0);
  assert.equal(q.size, 2);
  assert.equal(q.isReady, false);
});

test("markReady flushes queued links in FIFO order", () => {
  const delivered = [];
  const q = new DeepLinkQueue((l) => delivered.push(l.action));
  q.offer(link("book"));
  q.offer(link("settings"));
  q.markReady();
  assert.deepEqual(delivered, ["book", "settings"]);
  assert.equal(q.size, 0);
  assert.equal(q.isReady, true);
});

test("links offered after ready are delivered immediately", () => {
  const delivered = [];
  const q = new DeepLinkQueue((l) => delivered.push(l.action));
  q.markReady();
  q.offer(link("diagnostics"));
  assert.deepEqual(delivered, ["diagnostics"]);
});

test("reset clears pending and the ready flag", () => {
  const delivered = [];
  const q = new DeepLinkQueue((l) => delivered.push(l.action));
  q.offer(link("book"));
  q.reset();
  assert.equal(q.size, 0);
  assert.equal(q.isReady, false);
  q.markReady();
  assert.equal(delivered.length, 0);
});
