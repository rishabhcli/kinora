import test from "node:test";
import assert from "node:assert/strict";
import {
  isPlausibleToken,
  makeEnvelope,
  isEnvelope,
  parseEnvelope,
  obfuscate,
  deobfuscate,
} from "../../dist-electron/core/token-codec.js";

test("isPlausibleToken accepts realistic tokens, rejects junk", () => {
  assert.equal(isPlausibleToken("eyJhbGciOiJIUzI1NiJ9.payload.sig"), true);
  assert.equal(isPlausibleToken("a".repeat(64)), true);
  assert.equal(isPlausibleToken("short"), false); // <8
  assert.equal(isPlausibleToken("has space inside"), false);
  assert.equal(isPlausibleToken("a".repeat(9000)), false); // >8192
  assert.equal(isPlausibleToken(123), false);
  assert.equal(isPlausibleToken(""), false);
});

test("makeEnvelope / isEnvelope round-trip", () => {
  const env = makeEnvelope("cGF5bG9hZA==", "enc", 42);
  assert.deepEqual(env, { v: 1, mode: "enc", payload: "cGF5bG9hZA==", ts: 42 });
  assert.equal(isEnvelope(env), true);
  assert.equal(isEnvelope({ v: 2, mode: "enc", payload: "x" }), false);
  assert.equal(isEnvelope({ v: 1, mode: "weird", payload: "x" }), false);
  assert.equal(isEnvelope(null), false);
});

test("parseEnvelope tolerates bad input", () => {
  assert.equal(parseEnvelope(null), null);
  assert.equal(parseEnvelope(""), null);
  assert.equal(parseEnvelope("{not json"), null);
  assert.equal(parseEnvelope(JSON.stringify({ nope: true })), null);
  const ok = parseEnvelope(JSON.stringify(makeEnvelope("x", "plain", 1)));
  assert.equal(ok.mode, "plain");
});

test("obfuscate/deobfuscate is reversible base64 (NOT encryption)", () => {
  const token = "my-bearer-token-12345";
  const o = obfuscate(token);
  assert.notEqual(o, token);
  assert.equal(deobfuscate(o), token);
  assert.equal(Buffer.from(o, "base64").toString("utf8"), token);
});
