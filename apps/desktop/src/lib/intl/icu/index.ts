// ICU MessageFormat — public surface + a memoised compile→evaluate convenience.

import type { Message } from "./ast.ts";
import { parse, tryParse, ICUParseError } from "./parser.ts";
import { evaluate, evaluateParts, type EvalContext, type Part } from "./evaluate.ts";
import type { IntlArgs } from "../types.ts";

export type { Message, MessageNode } from "./ast.ts";
export type { EvalContext, Part } from "./evaluate.ts";
export { parse, tryParse, ICUParseError } from "./parser.ts";
export { evaluate, evaluateParts } from "./evaluate.ts";

// Compiled-AST cache keyed by source string. ICU parsing is pure, so an unbounded
// app-lifetime cache of compiled messages is safe and avoids re-parsing per render.
const astCache = new Map<string, Message>();

/** Parse `src` once and memoise the AST. */
export function compile(src: string): Message {
  const hit = astCache.get(src);
  if (hit) return hit;
  const ast = parse(src);
  astCache.set(src, ast);
  return ast;
}

/** One-shot: compile + evaluate an ICU message to a string. */
export function formatMessage(
  src: string,
  locale: string,
  args: IntlArgs = {},
  onMissing: EvalContext["onMissing"] = "key",
): string {
  return evaluate(compile(src), { locale, args, onMissing });
}

/** One-shot: compile + evaluate to rich-text parts (tags preserved). */
export function formatMessageToParts(
  src: string,
  locale: string,
  args: IntlArgs = {},
  onMissing: EvalContext["onMissing"] = "key",
): Part[] {
  return evaluateParts(compile(src), { locale, args, onMissing });
}

/** Clear the compiled-AST cache (test seam). */
export function _clearAstCache(): void {
  astCache.clear();
}

export { ICUParseError as ParseError };
