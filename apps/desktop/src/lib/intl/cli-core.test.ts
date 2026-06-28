import { test } from "vitest";
import assert from "node:assert/strict";
import { parseArgs, summarizeLint, USAGE } from "./cli-core.ts";

test("parses a bare command", () => {
  const p = parseArgs(["lint"]);
  assert.equal(p.command, "lint");
  assert.equal(p.help, false);
  assert.equal(p.strictExtra, false);
});

test("--strict toggles strictExtra", () => {
  assert.equal(parseArgs(["lint", "--strict"]).strictExtra, true);
});

test("--out PATH and --out=PATH both work", () => {
  assert.equal(parseArgs(["pseudo", "--out", "x.json"]).out, "x.json");
  assert.equal(parseArgs(["pseudo", "--out=y.json"]).out, "y.json");
  assert.equal(parseArgs(["pseudo"]).out, "-"); // default stdout
});

test("-h / --help sets help and shows usage", () => {
  const p = parseArgs(["--help"]);
  assert.equal(p.help, true);
  assert.equal(p.usage, USAGE);
});

test("unknown command leaves command null", () => {
  assert.equal(parseArgs(["frobnicate"]).command, null);
});

test("first valid command wins; later positionals ignored", () => {
  assert.equal(parseArgs(["coverage", "lint"]).command, "coverage");
});

test("summarizeLint: 0 errors → pass + exit 0", () => {
  const s = summarizeLint(0);
  assert.equal(s.exitCode, 0);
  assert.match(s.line, /pass/);
});

test("summarizeLint: errors → fail + exit 1", () => {
  const s = summarizeLint(3);
  assert.equal(s.exitCode, 1);
  assert.match(s.line, /3 error/);
});
