import test from "node:test";
import assert from "node:assert/strict";
import { Logger, createConsoleSink } from "../../dist-electron/core/logger.js";

function fixedClock() {
  let t = 1000;
  return () => (t += 1);
}

test("respects the minimum level", () => {
  const log = new Logger({ level: "warn", now: fixedClock() });
  log.log("info", "s", "ignored");
  log.log("warn", "s", "kept");
  const tail = log.tail();
  assert.equal(tail.length, 1);
  assert.equal(tail[0].message, "kept");
});

test("ring buffer evicts oldest beyond ringSize", () => {
  const log = new Logger({ level: "debug", ringSize: 16, now: fixedClock() });
  for (let i = 0; i < 50; i++) log.log("info", "s", `m${i}`);
  const tail = log.tail();
  assert.equal(tail.length, 16);
  assert.equal(tail[0].message, "m34");
  assert.equal(tail[15].message, "m49");
});

test("tail(limit) returns the most recent N", () => {
  const log = new Logger({ level: "debug", now: fixedClock() });
  for (let i = 0; i < 10; i++) log.log("info", "s", `m${i}`);
  assert.deepEqual(
    log.tail(3).map((e) => e.message),
    ["m7", "m8", "m9"],
  );
});

test("scoped() prefixes scope and forwards levels", () => {
  const log = new Logger({ level: "debug", now: fixedClock() });
  const s = log.scoped("net");
  s.debug("d");
  s.info("i");
  s.warn("w");
  s.error("e", { a: 1 });
  const tail = log.tail();
  assert.deepEqual(
    tail.map((e) => [e.level, e.scope]),
    [
      ["debug", "net"],
      ["info", "net"],
      ["warn", "net"],
      ["error", "net"],
    ],
  );
  assert.deepEqual(tail[3].data, { a: 1 });
});

test("redacts sensitive keys before they reach a sink", () => {
  const seen = [];
  const log = new Logger({ level: "debug", now: fixedClock(), sinks: [{ write: (e) => seen.push(e) }] });
  log.log("info", "auth", "login", { token: "secret-abc", nested: { apiKey: "k", ok: 1 } });
  const entry = seen[0];
  assert.equal(entry.data.token, "«redacted»");
  assert.equal(entry.data.nested.apiKey, "«redacted»");
  assert.equal(entry.data.nested.ok, 1);
});

test("a throwing sink never crashes logging", () => {
  const log = new Logger({
    level: "debug",
    now: fixedClock(),
    sinks: [
      {
        write() {
          throw new Error("sink down");
        },
      },
    ],
  });
  assert.doesNotThrow(() => log.log("info", "s", "still works"));
  assert.equal(log.count(), 1);
});

test("console sink routes by level", () => {
  const calls = { log: 0, warn: 0, error: 0 };
  const sink = createConsoleSink({
    log: () => calls.log++,
    warn: () => calls.warn++,
    error: () => calls.error++,
  });
  sink.write({ ts: 1, level: "info", scope: "s", message: "m" });
  sink.write({ ts: 2, level: "warn", scope: "s", message: "m" });
  sink.write({ ts: 3, level: "error", scope: "s", message: "m" });
  assert.deepEqual(calls, { log: 1, warn: 1, error: 1 });
});

test("setLevel changes the threshold at runtime", () => {
  const log = new Logger({ level: "info", now: fixedClock() });
  log.log("debug", "s", "no");
  log.setLevel("debug");
  log.log("debug", "s", "yes");
  assert.equal(log.tail().length, 1);
  assert.equal(log.tail()[0].message, "yes");
});
