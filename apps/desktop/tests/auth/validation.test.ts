// Run: node --test apps/desktop/tests/auth   (Node 26 strips .ts types natively)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  validateEmail,
  validatePassword,
  passwordStrength,
} from "../../src/components/auth/validation.ts";

test("validateEmail: empty is a friendly prompt, not a scold", () => {
  assert.equal(validateEmail(""), "Enter your email address.");
  assert.equal(validateEmail("   "), "Enter your email address.");
});

test("validateEmail: rejects malformed addresses", () => {
  assert.equal(validateEmail("notanemail"), "Enter a valid email address.");
  assert.equal(validateEmail("a@b"), "Enter a valid email address.");
  assert.equal(validateEmail("a@b."), "Enter a valid email address.");
  assert.equal(validateEmail("@b.com"), "Enter a valid email address.");
});

test("validateEmail: accepts real addresses (trimmed) → null", () => {
  assert.equal(validateEmail("demo@kinora.local"), null);
  assert.equal(validateEmail("  reader@example.com  "), null);
  assert.equal(validateEmail("a.b+tag@sub.domain.io"), null);
});

test("validatePassword: empty is prompted in both modes", () => {
  assert.equal(validatePassword("", "login"), "Enter your password.");
  assert.equal(validatePassword("", "register"), "Enter your password.");
});

test("validatePassword: login does not nag a short existing password", () => {
  assert.equal(validatePassword("x", "login"), null);
  assert.equal(validatePassword("demo-password-123", "login"), null);
});

test("validatePassword: register enforces a minimum length", () => {
  assert.equal(validatePassword("short", "register"), "Use at least 8 characters.");
  assert.equal(validatePassword("12345678", "register"), null);
});

test("passwordStrength: empty → score 0, no label", () => {
  assert.deepEqual(passwordStrength(""), { score: 0, label: "" });
});

test("passwordStrength: grows with length and character variety", () => {
  assert.ok(passwordStrength("aaaa").score <= 1, "trivial short → weak");
  const strong = passwordStrength("Tr0ub4dour&3xtra");
  assert.equal(strong.score, 4);
  assert.equal(strong.label, "Strong");
  // monotonic-ish: adding variety never lowers the score
  assert.ok(passwordStrength("abcdefgh").score <= passwordStrength("Abcdefg1!").score);
});

test("passwordStrength: labels map to the documented scale", () => {
  for (const s of [0, 1, 2, 3, 4]) {
    const label = passwordStrength.labelFor(s);
    assert.equal(typeof label, "string");
  }
  assert.equal(passwordStrength.labelFor(4), "Strong");
  assert.equal(passwordStrength.labelFor(2), "Fair");
});
