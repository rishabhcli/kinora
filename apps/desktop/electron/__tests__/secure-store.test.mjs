import test from "node:test";
import assert from "node:assert/strict";
import { SecureStore } from "../../dist-electron/services/secure-store.js";

const silentLog = { debug() {}, info() {}, warn() {}, error() {} };

function memFile() {
  let content = null;
  return {
    file: {
      readText: () => content,
      writeText: (t) => {
        content = t;
      },
    },
    raw: () => content,
  };
}

/** A fake safeStorage that reverses bytes — proves encrypt path is exercised. */
function fakeStorage(available = true) {
  return {
    isEncryptionAvailable: () => available,
    encryptString: (plain) => Buffer.from(`ENC(${plain})`, "utf8"),
    decryptString: (buf) => {
      const s = buf.toString("utf8");
      const m = /^ENC\((.*)\)$/s.exec(s);
      if (!m) throw new Error("bad ciphertext");
      return m[1];
    },
  };
}

test("encrypts, persists an enc envelope, and reads back", () => {
  const m = memFile();
  const store = new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(true) });
  const token = "bearer-abc-123456";
  assert.equal(store.setToken(token), true);
  const persisted = JSON.parse(m.raw());
  assert.equal(persisted.mode, "enc");
  assert.ok(!m.raw().includes(token), "raw token must not appear in plaintext");
  assert.equal(store.getToken(), token);
});

test("refuses implausible tokens", () => {
  const m = memFile();
  const store = new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(true) });
  assert.equal(store.setToken("short"), false);
  assert.equal(m.raw(), null);
});

test("falls back to a marked plain envelope when encryption is unavailable", () => {
  const m = memFile();
  const store = new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(false) });
  const token = "bearer-no-keyring-1";
  assert.equal(store.setToken(token), true);
  const persisted = JSON.parse(m.raw());
  assert.equal(persisted.mode, "plain");
  // Still readable (obfuscated, not plaintext).
  assert.ok(!m.raw().includes(token));
  assert.equal(store.getToken(), token);
});

test("returns null when an enc envelope exists but encryption is now unavailable", () => {
  const m = memFile();
  // Write with encryption available...
  new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(true) }).setToken("bearer-token-xyz");
  // ...then read with encryption unavailable.
  const reader = new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(false) });
  assert.equal(reader.getToken(), null);
});

test("clear wipes the token", () => {
  const m = memFile();
  const store = new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(true) });
  store.setToken("bearer-to-clear-1");
  store.clear();
  assert.equal(store.getToken(), null);
});

test("posture reports encryption availability", () => {
  const m = memFile();
  assert.equal(
    new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(true) }).posture().encryptionAvailable,
    true,
  );
  assert.equal(
    new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(false) }).posture().encryptionAvailable,
    false,
  );
});

test("decrypt failure returns null rather than throwing", () => {
  const m = memFile();
  m.file.writeText(JSON.stringify({ v: 1, mode: "enc", payload: "bm90LWVuYw==", ts: 1 }));
  const store = new SecureStore({ file: m.file, log: silentLog, storage: fakeStorage(true) });
  assert.equal(store.getToken(), null);
});
