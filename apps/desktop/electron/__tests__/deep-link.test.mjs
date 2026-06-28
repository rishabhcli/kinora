import test from "node:test";
import assert from "node:assert/strict";
import {
  isDeepLink,
  parseDeepLink,
  findDeepLinkInArgv,
  deepLinkToRoute,
} from "../../dist-electron/core/deep-link.js";

test("isDeepLink recognises kinora scheme (case-insensitive)", () => {
  assert.equal(isDeepLink("kinora://book/1"), true);
  assert.equal(isDeepLink("KINORA://book/1"), true);
  assert.equal(isDeepLink("  kinora://x  "), true);
  assert.equal(isDeepLink("https://kinora/x"), false);
  assert.equal(isDeepLink("kinorax://x"), false);
  assert.equal(isDeepLink(42), false);
  assert.equal(isDeepLink(null), false);
});

test("parseDeepLink extracts action, segments, params", () => {
  const link = parseDeepLink("kinora://book/abc-123/page/4?from=share&autoplay=1");
  assert.ok(link);
  assert.equal(link.action, "book");
  assert.deepEqual(link.segments, ["abc-123", "page", "4"]);
  assert.deepEqual(link.params, { from: "share", autoplay: "1" });
  assert.equal(link.href, "kinora://book/abc-123/page/4?from=share&autoplay=1");
});

test("parseDeepLink decodes percent-encoded segments + params", () => {
  const link = parseDeepLink("kinora://open/My%20Book?title=A%26B");
  assert.ok(link);
  assert.equal(link.action, "open");
  assert.deepEqual(link.segments, ["My Book"]);
  assert.equal(link.params.title, "A&B");
});

test("parseDeepLink lowercases the action host", () => {
  const link = parseDeepLink("kinora://BOOK/1");
  assert.equal(link?.action, "book");
});

test("parseDeepLink handles empty host by promoting first path segment", () => {
  const link = parseDeepLink("kinora:///library");
  assert.equal(link?.action, "library");
  assert.deepEqual(link?.segments, []);
});

test("parseDeepLink returns null for non-kinora / malformed input", () => {
  assert.equal(parseDeepLink("https://example.com"), null);
  assert.equal(parseDeepLink("not a url"), null);
  assert.equal(parseDeepLink(""), null);
  assert.equal(parseDeepLink(undefined), null);
});

test("findDeepLinkInArgv returns first valid link", () => {
  const argv = ["/path/electron", "/path/main.js", "--flag", "kinora://book/9"];
  const link = findDeepLinkInArgv(argv);
  assert.equal(link?.action, "book");
  assert.deepEqual(link?.segments, ["9"]);
  assert.equal(findDeepLinkInArgv(["a", "b"]), null);
});

test("deepLinkToRoute maps known actions", () => {
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://book/42")), "/book/42");
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://open?id=7")), "/book/7");
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://open")), "/library");
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://library")), "/library");
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://settings")), "/settings");
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://auth/callback?token=x")), "/auth/callback");
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://diagnostics")), "/diagnostics");
});

test("deepLinkToRoute returns null for unknown action", () => {
  assert.equal(deepLinkToRoute(parseDeepLink("kinora://wat/1")), null);
});

test("deepLinkToRoute encodes ids with special chars", () => {
  const route = deepLinkToRoute(parseDeepLink("kinora://book/a b"));
  assert.equal(route, "/book/a%20b");
});
