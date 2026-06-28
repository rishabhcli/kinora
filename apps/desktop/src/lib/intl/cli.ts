// i18n CLI — lint catalogs, extract keys, and pseudo-generate, from the terminal.
//
// Run with the repo's node-strip-types convention (no build step):
//
//   node --experimental-strip-types apps/desktop/src/lib/intl/cli.ts lint
//   node --experimental-strip-types apps/desktop/src/lib/intl/cli.ts extract
//   node --experimental-strip-types apps/desktop/src/lib/intl/cli.ts pseudo --out -
//   node --experimental-strip-types apps/desktop/src/lib/intl/cli.ts coverage
//
// The argument-parsing + report-building logic is a PURE core (cli-core.ts) so it
// is unit-tested without touching the filesystem; this file is the thin I/O shell.

import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, dirname, relative, basename } from "node:path";
import { fileURLToPath } from "node:url";
import type { MessageTree } from "./types.ts";
import { lintCatalog, formatLintReport } from "./lint.ts";
import { extractKeySet, crossReference } from "./extract.ts";
import { flatten, coverage as catalogCoverage } from "./catalog.ts";
import { pseudoLocalizeCatalog } from "./pseudo.ts";
import { parseArgs, summarizeLint, type CliCommand } from "./cli-core.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const LOCALES_DIR = join(HERE, "..", "..", "i18n", "locales");
const SRC_DIR = join(HERE, "..", "..");
const SOURCE_LOCALE = "en";

function readJson(path: string): MessageTree {
  return JSON.parse(readFileSync(path, "utf8")) as MessageTree;
}

function localeFiles(): Array<{ code: string; path: string }> {
  return readdirSync(LOCALES_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => ({ code: basename(f, ".json"), path: join(LOCALES_DIR, f) }))
    .sort((a, b) => a.code.localeCompare(b.code));
}

function walkSources(dir: string, exts: string[]): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry.startsWith(".")) continue;
    const path = join(dir, entry);
    const st = statSync(path);
    if (st.isDirectory()) found.push(...walkSources(path, exts));
    else if (exts.some((e) => entry.endsWith(e)) && !entry.endsWith(".test.ts") && !entry.endsWith(".test.tsx")) {
      found.push(path);
    }
  }
  return found;
}

function cmdLint(strictExtra: boolean): number {
  const source = readJson(join(LOCALES_DIR, `${SOURCE_LOCALE}.json`));
  let errors = 0;
  for (const { code, path } of localeFiles()) {
    if (code === SOURCE_LOCALE) continue;
    const result = lintCatalog(source, readJson(path), code, { strictExtra });
    process.stdout.write(formatLintReport(result) + "\n");
    errors += result.errorCount;
  }
  const summary = summarizeLint(errors);
  process.stdout.write(summary.line + "\n");
  return summary.exitCode;
}

function cmdCoverage(): number {
  const source = readJson(join(LOCALES_DIR, `${SOURCE_LOCALE}.json`));
  const total = Object.keys(flatten(source)).length;
  process.stdout.write(`Source (${SOURCE_LOCALE}): ${total} keys\n`);
  for (const { code, path } of localeFiles()) {
    if (code === SOURCE_LOCALE) continue;
    const pct = Math.round(catalogCoverage(source, readJson(path)) * 100);
    process.stdout.write(`  ${code.padEnd(8)} ${pct}%\n`);
  }
  return 0;
}

function cmdExtract(strict: boolean): number {
  const source = readJson(join(LOCALES_DIR, `${SOURCE_LOCALE}.json`));
  const catalogKeys = Object.keys(flatten(source));
  // Skip the intl engine + i18n layer itself (its generic `t(key)` wrappers and
  // doc/example keys are not product call-sites) and the test tree.
  const sources = walkSources(SRC_DIR, [".ts", ".tsx"])
    .filter((p) => !p.includes("/lib/intl/") && !p.includes("/i18n/") && !p.includes("/test/"))
    .map((p) => readFileSync(p, "utf8"));
  const used = extractKeySet(sources);
  const report = crossReference(used, catalogKeys);
  process.stdout.write(`Used keys: ${used.size}\n`);
  if (report.undefinedKeys.length) {
    process.stdout.write(`Used-but-undefined (${report.undefinedKeys.length}):\n`);
    for (const k of report.undefinedKeys) process.stdout.write(`  ✗ ${k}\n`);
  }
  if (report.unusedKeys.length) {
    process.stdout.write(`Defined-but-unused (${report.unusedKeys.length}):\n`);
    for (const k of report.unusedKeys) process.stdout.write(`  ⚠ ${k}\n`);
  }
  // Undefined keys are real bugs but extraction is heuristic (dynamic keys, alias
  // call-sites), so only fail the run under --strict.
  return strict && report.undefinedKeys.length > 0 ? 1 : 0;
}

function cmdPseudo(out: string): number {
  const source = readJson(join(LOCALES_DIR, `${SOURCE_LOCALE}.json`));
  const pseudo = pseudoLocalizeCatalog(source as Record<string, unknown>);
  const json = JSON.stringify(pseudo, null, 2) + "\n";
  if (out === "-" || out === "") {
    process.stdout.write(json);
  } else {
    writeFileSync(out, json);
    process.stdout.write(`Wrote ${relative(process.cwd(), out)}\n`);
  }
  return 0;
}

function run(argv: string[]): number {
  const parsed = parseArgs(argv);
  if (parsed.help || !parsed.command) {
    process.stdout.write(parsed.usage + "\n");
    return parsed.command ? 0 : 1;
  }
  const command: CliCommand = parsed.command;
  switch (command) {
    case "lint":
      return cmdLint(parsed.strictExtra);
    case "coverage":
      return cmdCoverage();
    case "extract":
      return cmdExtract(parsed.strictExtra);
    case "pseudo":
      return cmdPseudo(parsed.out);
    default:
      process.stdout.write(parsed.usage + "\n");
      return 1;
  }
}

// Only execute when run directly (not when imported by a test).
const invokedDirectly =
  process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1];
if (invokedDirectly) {
  process.exit(run(process.argv.slice(2)));
}

export { run };
