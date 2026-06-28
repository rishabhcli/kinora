// Pure argument-parsing + reporting helpers for the i18n CLI. No filesystem, no
// process — just string/struct transforms, so it is fully node-testable.

export type CliCommand = "lint" | "extract" | "coverage" | "pseudo";

const COMMANDS: readonly CliCommand[] = ["lint", "extract", "coverage", "pseudo"];

export interface ParsedArgs {
  command: CliCommand | null;
  help: boolean;
  /** lint: treat stale/extra keys as errors. */
  strictExtra: boolean;
  /** pseudo: output path ("-" = stdout). */
  out: string;
  usage: string;
}

export const USAGE = [
  "kinora-i18n — message-catalog tooling",
  "",
  "Usage: cli.ts <command> [options]",
  "",
  "Commands:",
  "  lint        validate every locale against the en source",
  "  coverage    print per-locale translation coverage",
  "  extract     find used-but-undefined and defined-but-unused keys",
  "  pseudo      emit a pseudo-localized copy of the source catalog",
  "",
  "Options:",
  "  --strict    (lint) treat extra/stale keys as errors",
  "  --out PATH  (pseudo) write to PATH instead of stdout ('-' = stdout)",
  "  -h, --help  show this help",
].join("\n");

/** Parse argv (already sliced past `node script`). Pure. */
export function parseArgs(argv: readonly string[]): ParsedArgs {
  let command: CliCommand | null = null;
  let help = false;
  let strictExtra = false;
  let out = "-";

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "-h" || arg === "--help") {
      help = true;
    } else if (arg === "--strict") {
      strictExtra = true;
    } else if (arg === "--out") {
      out = argv[i + 1] ?? "-";
      i++;
    } else if (arg.startsWith("--out=")) {
      out = arg.slice("--out=".length);
    } else if (!command && (COMMANDS as readonly string[]).includes(arg)) {
      command = arg as CliCommand;
    }
  }

  return { command, help, strictExtra, out, usage: USAGE };
}

/** Build the final lint summary line + exit code from an error count. */
export function summarizeLint(errorCount: number): { line: string; exitCode: number } {
  if (errorCount === 0) {
    return { line: "✓ all catalogs pass", exitCode: 0 };
  }
  return {
    line: `✗ ${errorCount} error(s) across catalogs`,
    exitCode: 1,
  };
}
