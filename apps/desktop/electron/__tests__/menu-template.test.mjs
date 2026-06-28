import test from "node:test";
import assert from "node:assert/strict";
import {
  ACCELERATORS,
  GLOBAL_SHORTCUTS,
  findDuplicateAccelerators,
  buildMenuTemplate,
  collectLabels,
} from "../../dist-electron/core/menu-template.js";

const noopCallbacks = () => ({
  addBook() {},
  menuAction() {},
  openExternal() {},
  checkForUpdates() {},
});

test("no accelerator is bound twice", () => {
  assert.deepEqual(findDuplicateAccelerators(), []);
  assert.deepEqual(findDuplicateAccelerators(ACCELERATORS), []);
});

test("findDuplicateAccelerators detects a clash (case-insensitive)", () => {
  const dupes = findDuplicateAccelerators([
    { accelerator: "CmdOrCtrl+O", action: "add-book" },
    { accelerator: "cmdorctrl+o", action: "open-settings" },
  ]);
  assert.equal(dupes.length, 1);
});

test("global shortcuts are flagged", () => {
  assert.ok(GLOBAL_SHORTCUTS.length >= 1);
  for (const g of GLOBAL_SHORTCUTS) assert.equal(g.global, true);
});

test("mac template includes the app menu; non-mac does not", () => {
  const mac = buildMenuTemplate("darwin", noopCallbacks());
  assert.equal(mac[0].label, "Kinora");
  const win = buildMenuTemplate("win32", noopCallbacks());
  assert.notEqual(win[0].label, "Kinora");
  assert.equal(win[0].label, "File");
});

test("template exposes the expected top-level menus", () => {
  const labels = buildMenuTemplate("darwin", noopCallbacks())
    .map((m) => m.label)
    .filter(Boolean);
  assert.ok(labels.includes("File"));
  assert.ok(labels.includes("Edit"));
  assert.ok(labels.includes("View"));
});

test("Add Book / Diagnostics labels are present", () => {
  const labels = collectLabels(buildMenuTemplate("win32", noopCallbacks()));
  assert.ok(labels.includes("Add Book…"));
  assert.ok(labels.includes("Diagnostics…"));
  assert.ok(labels.includes("New Window"));
});

test("menu clicks invoke the wired callbacks", () => {
  const hits = [];
  const cb = {
    addBook: () => hits.push("addBook"),
    menuAction: (a) => hits.push(`menu:${a}`),
    openExternal: (u) => hits.push(`ext:${u}`),
    checkForUpdates: () => hits.push("update"),
  };
  const tpl = buildMenuTemplate("darwin", cb);
  // Find and click "Add Book…" deep in the File menu.
  const file = tpl.find((m) => m.label === "File");
  const addBook = file.submenu.find((i) => i.label === "Add Book…");
  addBook.click();
  // Click the GitHub external link in Help.
  const help = tpl.find((m) => m.role === "help");
  const gh = help.submenu.find((i) => i.label === "Kinora on GitHub");
  gh.click();
  assert.ok(hits.includes("addBook"));
  assert.ok(hits.some((h) => h.startsWith("ext:https://github.com")));
});
