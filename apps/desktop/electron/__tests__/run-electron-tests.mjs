// Runs the main-process unit tests WITHOUT launching Electron.
//
// The tests exercise the pure cores (deep-link parsing, IPC routing/validation,
// window-state math, the structured logger, the config store, update policy,
// token codec, menu template, app-config, monitors/protocol helpers). None of
// them import `electron`, so they run in plain Node.
//
// They import the COMPILED output from `dist-electron/` (not the .ts source),
// because the source uses `.js` import specifiers for the CommonJS build — so
// we build electron first, then run the test files against the emitted JS.
// This also means the tests cover exactly what ships.
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const desktopRoot = dirname(dirname(here)); // apps/desktop

// 1) Build the electron CJS so the tests can import the compiled modules.
const build = spawnSync("node", ["node_modules/typescript/bin/tsc", "-p", "electron/tsconfig.json"], {
  cwd: desktopRoot,
  stdio: "inherit",
});
if (build.status !== 0) {
  process.stdout.write("\nelectron tsc build failed — aborting electron tests\n");
  process.exit(build.status ?? 1);
}

// 2) Run every *.test.mjs in this directory via node:test.
const files = readdirSync(here)
  .filter((f) => f.endsWith(".test.mjs"))
  .sort()
  .map((f) => join(here, f));

let failed = 0;
for (const file of files) {
  process.stdout.write(`\n* electron/__tests__/${file.split("/").pop()}\n`);
  const result = spawnSync(process.execPath, ["--test", file], { stdio: "inherit", cwd: desktopRoot });
  if (result.status !== 0) failed += 1;
}

process.stdout.write(`\n[electron] ${files.length} file(s), ${failed} failing\n`);
process.exit(failed > 0 ? 1 : 0);
