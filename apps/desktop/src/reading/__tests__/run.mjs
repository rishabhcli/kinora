// Runs every `*.test.ts` in this folder, one node subprocess each (Node strips
// the TS types — no test framework, see tiny-test.mjs). Cross-platform (no bash).
//   pnpm --filter @kinora/desktop test:reading
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here)
  .filter((f) => f.endsWith(".test.ts"))
  .sort();

let failed = 0;
for (const f of files) {
  process.stdout.write(`\n• ${f}\n`);
  const r = spawnSync(
    process.execPath,
    ["--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "--experimental-strip-types", join(here, f)],
    { stdio: "inherit" },
  );
  if (r.status !== 0) failed += 1;
}
process.stdout.write(`\n${files.length} file(s), ${failed} failing\n`);
process.exit(failed > 0 ? 1 : 0);
