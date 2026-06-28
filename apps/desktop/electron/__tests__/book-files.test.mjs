import test from "node:test";
import assert from "node:assert/strict";
import { isBook, findBookInArgv } from "../../dist-electron/core/book-files.js";

test("isBook accepts pdf/epub (case-insensitive), rejects others", () => {
  assert.equal(isBook("/x/a.pdf"), true);
  assert.equal(isBook("/x/a.PDF"), true);
  assert.equal(isBook("/x/a.epub"), true);
  assert.equal(isBook("/x/a.txt"), false);
  assert.equal(isBook("/x/a"), false);
  assert.equal(isBook(123), false);
});

test("findBookInArgv returns the first book path", () => {
  assert.equal(findBookInArgv(["electron", "main.js", "--x", "/books/b.epub"]), "/books/b.epub");
  assert.equal(findBookInArgv(["electron", "main.js"]), null);
});
