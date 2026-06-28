import test from "node:test";
import assert from "node:assert/strict";
import { ConfigStore } from "../../dist-electron/core/config-store.js";

/** An in-memory AtomicFile for tests. */
function memFile(initial = null) {
  let content = initial;
  const quarantined = [];
  return {
    file: {
      readText: () => content,
      writeText: (t) => {
        content = t;
      },
      quarantine: (t) => quarantined.push(t),
    },
    get content() {
      return content;
    },
    set content(v) {
      content = v;
    },
    quarantined,
  };
}

test("returns defaults when the file is empty", () => {
  const m = memFile(null);
  const store = new ConfigStore({ file: m.file, defaults: { theme: "dark", count: 0 } });
  assert.equal(store.get("theme"), "dark");
  assert.equal(store.get("count"), 0);
});

test("set persists an envelope and is read back", () => {
  const m = memFile(null);
  const store = new ConfigStore({ file: m.file, defaults: { theme: "dark" }, version: 2 });
  store.set("theme", "light");
  const persisted = JSON.parse(m.content);
  assert.equal(persisted.__kinora, true);
  assert.equal(persisted.version, 2);
  assert.equal(persisted.data.theme, "light");

  const reloaded = new ConfigStore({ file: m.file, defaults: { theme: "dark" }, version: 2 });
  assert.equal(reloaded.get("theme"), "light");
});

test("merge writes multiple keys and skips no-op writes", () => {
  const m = memFile(null);
  const store = new ConfigStore({ file: m.file, defaults: { a: 1, b: 2 } });
  store.merge({ a: 10, b: 20 });
  assert.equal(store.get("a"), 10);
  assert.equal(store.get("b"), 20);
  const before = m.content;
  store.merge({ a: 10 }); // no change
  assert.equal(m.content, before);
});

test("delete removes a key", () => {
  const m = memFile(null);
  const store = new ConfigStore({ file: m.file, defaults: { a: 1 } });
  store.set("a", 5);
  store.delete("a");
  // After delete, defaults re-supply the key on reload.
  const reloaded = new ConfigStore({ file: m.file, defaults: { a: 1 } });
  assert.equal(reloaded.get("a"), 1);
});

test("corrupt JSON is quarantined and resets to defaults", () => {
  const m = memFile("{ not json");
  const store = new ConfigStore({ file: m.file, defaults: { a: 1 } });
  assert.equal(store.get("a"), 1);
  assert.equal(m.quarantined.length, 1);
});

test("newly-added default keys appear after an upgrade", () => {
  const m = memFile(JSON.stringify({ __kinora: true, version: 1, data: { a: 1 } }));
  const store = new ConfigStore({ file: m.file, defaults: { a: 1, b: 99 } });
  assert.equal(store.get("a"), 1);
  assert.equal(store.get("b"), 99);
});

test("migrate runs on a version mismatch", () => {
  const m = memFile(JSON.stringify({ __kinora: true, version: 1, data: { old: "x" } }));
  const store = new ConfigStore({
    file: m.file,
    defaults: { migrated: false },
    version: 2,
    migrate: (data, from) => {
      assert.equal(from, 1);
      assert.equal(data.old, "x");
      return { migrated: true };
    },
  });
  assert.equal(store.get("migrated"), true);
});

test("validate can reject and reset", () => {
  const m = memFile(JSON.stringify({ __kinora: true, version: 1, data: { evil: true } }));
  const store = new ConfigStore({
    file: m.file,
    defaults: { ok: true },
    validate: (data) => (data && data.evil ? null : data),
  });
  assert.equal(store.get("ok"), true);
});

test("reset() restores defaults and persists", () => {
  const m = memFile(null);
  const store = new ConfigStore({ file: m.file, defaults: { a: 1 } });
  store.set("a", 9);
  store.reset();
  assert.equal(store.get("a"), 1);
  assert.ok(m.content.includes('"a": 1'));
});

test("a raw (non-enveloped) JSON blob is still loaded", () => {
  const m = memFile(JSON.stringify({ a: 7 }));
  const store = new ConfigStore({ file: m.file, defaults: { a: 1 } });
  assert.equal(store.get("a"), 7);
});
