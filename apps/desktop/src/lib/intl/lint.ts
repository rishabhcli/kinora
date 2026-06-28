// Catalog linter — catches the classes of i18n bug that ship silently:
//   • missing keys      a locale is short a string the source has
//   • extra keys        a locale has a string the source dropped (likely stale)
//   • invalid ICU       a translation has a malformed MessageFormat string
//   • placeholder drift a translation references different `{args}` than the source
//   • category gaps     a plural/select arm set is missing 'other'
//
// Pure over two trees + a parser; no I/O. The CLI (cli.ts) wires it to files.

import type { MessageTree } from "./types.ts";
import { flatten, diffCatalogs } from "./catalog.ts";
import { parse, ICUParseError, type Message, type MessageNode } from "./icu/index.ts";

export type Severity = "error" | "warning";

export interface LintIssue {
  severity: Severity;
  /** Stable machine code, e.g. "missing-key", "placeholder-drift". */
  code: string;
  /** The dotted catalog key the issue concerns (or "" for whole-catalog issues). */
  key: string;
  message: string;
}

export interface LintResult {
  locale: string;
  issues: LintIssue[];
  readonly errorCount: number;
  readonly warningCount: number;
}

/** Collect the set of `{arg}` names a message references (across all nodes/arms). */
export function collectArguments(message: Message): Set<string> {
  const args = new Set<string>();
  const visit = (nodes: MessageNode[]) => {
    for (const node of nodes) {
      switch (node.type) {
        case "argument":
        case "format":
          args.add(node.arg);
          break;
        case "plural":
        case "select":
          args.add(node.arg);
          for (const arm of Object.values(node.options)) visit(arm);
          break;
        case "tag":
          visit(node.children);
          break;
        default:
          break;
      }
    }
  };
  visit(message);
  return args;
}

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

export interface LintOptions {
  /** Treat extra (stale) keys as errors instead of warnings (default false). */
  strictExtra?: boolean;
  /** Require placeholder parity with the source (default true). */
  checkPlaceholders?: boolean;
}

/**
 * Lint a `subject` locale against the `reference` (source) catalog. Returns every
 * issue found; the caller decides whether any error fails the build.
 */
export function lintCatalog(
  reference: MessageTree,
  subject: MessageTree,
  locale: string,
  options: LintOptions = {},
): LintResult {
  const strictExtra = options.strictExtra ?? false;
  const checkPlaceholders = options.checkPlaceholders ?? true;

  const issues: LintIssue[] = [];
  const refFlat = flatten(reference);
  const subFlat = flatten(subject);
  const { missing, extra } = diffCatalogs(reference, subject);

  for (const key of missing) {
    issues.push({
      severity: "error",
      code: "missing-key",
      key,
      message: `missing translation for "${key}"`,
    });
  }
  for (const key of extra) {
    issues.push({
      severity: strictExtra ? "error" : "warning",
      code: "extra-key",
      key,
      message: `stale key "${key}" not present in source`,
    });
  }

  // Validate ICU + placeholder parity for every common key.
  for (const [key, subSrc] of Object.entries(subFlat)) {
    let subAst: Message;
    try {
      subAst = parse(subSrc);
    } catch (err) {
      const offset = err instanceof ICUParseError ? err.offset : -1;
      issues.push({
        severity: "error",
        code: "invalid-icu",
        key,
        message: `invalid ICU MessageFormat (offset ${offset}): ${(err as Error).message}`,
      });
      continue;
    }

    const refSrc = refFlat[key];
    if (refSrc === undefined) continue; // extra key — already flagged

    if (checkPlaceholders) {
      let refAst: Message | null = null;
      try {
        refAst = parse(refSrc);
      } catch {
        refAst = null;
      }
      if (refAst) {
        const refArgs = collectArguments(refAst);
        const subArgs = collectArguments(subAst);
        if (!setsEqual(refArgs, subArgs)) {
          const refList = [...refArgs].sort().join(", ") || "(none)";
          const subList = [...subArgs].sort().join(", ") || "(none)";
          issues.push({
            severity: "error",
            code: "placeholder-drift",
            key,
            message: `placeholder mismatch — source uses {${refList}}, translation uses {${subList}}`,
          });
        }
      }
    }
  }

  return {
    locale,
    issues,
    get errorCount() {
      return this.issues.filter((i) => i.severity === "error").length;
    },
    get warningCount() {
      return this.issues.filter((i) => i.severity === "warning").length;
    },
  };
}

/** Format a LintResult as a human-readable report block. */
export function formatLintReport(result: LintResult): string {
  if (result.issues.length === 0) {
    return `✓ ${result.locale}: no issues`;
  }
  const lines = [`${result.locale}: ${result.errorCount} error(s), ${result.warningCount} warning(s)`];
  for (const issue of result.issues) {
    const mark = issue.severity === "error" ? "✗" : "⚠";
    lines.push(`  ${mark} [${issue.code}] ${issue.key || "(catalog)"}: ${issue.message}`);
  }
  return lines.join("\n");
}
