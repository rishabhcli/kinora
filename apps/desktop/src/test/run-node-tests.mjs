// Runs the pure Node test files that intentionally use node:test or the tiny
// reading harness instead of Vitest/jsdom. This keeps `pnpm test` as one gate.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const srcRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function walk(dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) {
      found.push(...walk(path));
    } else if (entry.endsWith(".test.ts")) {
      const text = readFileSync(path, "utf8");
      if (
        text.includes('from "node:test"') ||
        text.includes("from 'node:test'") ||
        text.includes("tiny-test.mjs")
      ) {
        found.push(path);
      }
    }
  }
  return found.sort();
}

const files = walk(srcRoot);
let failed = 0;

for (const file of files) {
  process.stdout.write(`\n* ${relative(srcRoot, file)}\n`);
  const result = spawnSync(
    process.execPath,
    ["--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "--experimental-strip-types", file],
    { stdio: "inherit" },
  );
  if (result.status !== 0) {
    failed += 1;
  }
}

process.stdout.write(`\n${files.length} file(s), ${failed} failing\n`);
process.exit(failed > 0 ? 1 : 0);
