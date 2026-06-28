// ICU AST → string evaluator (and a rich-text "parts" evaluator for React).
//
// The string evaluator covers the full subset: argument interpolation, typed
// formats (number/date/time/currency/percent/unit), plural/selectordinal with
// `offset:` and `#`, and select. The parts evaluator additionally turns TagNodes
// into structured parts the React layer maps to elements (e.g. <b>…</b>).

import type { Message, MessageNode } from "./ast.ts";
import type { IntlArgs, IntlValue } from "../types.ts";
import { pluralCategory } from "../plural.ts";
import {
  formatNumber,
  formatDate,
  formatTime,
  formatCurrency,
  formatPercent,
  formatUnit,
} from "../format.ts";

export interface EvalContext {
  locale: string;
  args: IntlArgs;
  /**
   * When a referenced argument is missing, what to render. "key" → render the
   * argument name in braces (useful in dev); "empty" → "". Default "key".
   */
  onMissing?: "key" | "empty";
}

/** Stringify an ICU argument value defensively. */
function stringifyValue(v: IntlValue): string {
  if (v === null || v === undefined) return "";
  if (v instanceof Date) return v.toISOString();
  return String(v);
}

function toNumber(v: IntlValue): number {
  if (typeof v === "number") return v;
  if (typeof v === "bigint") return Number(v);
  if (typeof v === "boolean") return v ? 1 : 0;
  const n = Number(v);
  return Number.isNaN(n) ? 0 : n;
}

function toDate(v: IntlValue): Date {
  if (v instanceof Date) return v;
  if (typeof v === "number") return new Date(v);
  return new Date(String(v));
}

function missing(arg: string, ctx: EvalContext): string {
  return ctx.onMissing === "empty" ? "" : `{${arg}}`;
}

/** Parse a `number`/`date`/`time` style token into Intl options. */
function numberStyleOptions(style: string | undefined): Intl.NumberFormatOptions | undefined {
  if (!style) return undefined;
  if (style === "integer") return { maximumFractionDigits: 0 };
  if (style === "percent") return { style: "percent" };
  if (style === "compact") return { notation: "compact" };
  // `::` skeleton — minimal support for the common ones.
  if (style.startsWith("::")) {
    const skel = style.slice(2).trim();
    const opts: Intl.NumberFormatOptions = {};
    for (const token of skel.split(/\s+/)) {
      if (token === "compact-short") {
        opts.notation = "compact";
        opts.compactDisplay = "short";
      } else if (token === "compact-long") {
        opts.notation = "compact";
        opts.compactDisplay = "long";
      } else if (token === "percent") {
        opts.style = "percent";
      } else if (token.startsWith("currency/")) {
        opts.style = "currency";
        opts.currency = token.slice("currency/".length);
      } else if (/^\.0+$/.test(token)) {
        opts.minimumFractionDigits = token.length - 1;
        opts.maximumFractionDigits = token.length - 1;
      }
    }
    return opts;
  }
  return undefined;
}

type DateStyleToken = "full" | "long" | "medium" | "short";
function isDateStyle(s: string): s is DateStyleToken {
  return s === "full" || s === "long" || s === "medium" || s === "short";
}

function evalFormat(node: Extract<MessageNode, { type: "format" }>, ctx: EvalContext): string {
  const raw = ctx.args[node.arg];
  if (raw === undefined && !(node.arg in ctx.args)) return missing(node.arg, ctx);

  switch (node.format) {
    case "number":
      return formatNumber(toNumber(raw), ctx.locale, numberStyleOptions(node.style));
    case "percent":
      return formatPercent(toNumber(raw), ctx.locale);
    case "currency": {
      const cur = node.style || "USD";
      return formatCurrency(toNumber(raw), ctx.locale, cur);
    }
    case "unit": {
      const unit = node.style || "unit";
      return formatUnit(toNumber(raw), ctx.locale, unit);
    }
    case "date": {
      const style = node.style && isDateStyle(node.style) ? node.style : "medium";
      return formatDate(toDate(raw), ctx.locale, { dateStyle: style });
    }
    case "time": {
      const style = node.style && isDateStyle(node.style) ? node.style : "short";
      return formatTime(toDate(raw), ctx.locale, { timeStyle: style });
    }
    default:
      return stringifyValue(raw);
  }
}

function evalNodes(nodes: Message, ctx: EvalContext, pound?: number): string {
  let out = "";
  for (const node of nodes) {
    out += evalNode(node, ctx, pound);
  }
  return out;
}

function evalNode(node: MessageNode, ctx: EvalContext, pound?: number): string {
  switch (node.type) {
    case "literal":
      return node.value;
    case "pound":
      return pound === undefined ? "#" : formatNumber(pound, ctx.locale);
    case "argument": {
      if (!(node.arg in ctx.args)) return missing(node.arg, ctx);
      return stringifyValue(ctx.args[node.arg]);
    }
    case "format":
      return evalFormat(node, ctx);
    case "select": {
      const value = stringifyValue(ctx.args[node.arg]);
      const arm = node.options[value] ?? node.options.other;
      return evalNodes(arm, ctx, pound);
    }
    case "plural": {
      const original = toNumber(ctx.args[node.arg]);
      // ICU: an exact `=value` arm matches the *original* argument; category
      // resolution and `#` use the offset-adjusted value.
      const adjusted = original - node.offset;
      const exact = `=${original}`;
      let armKey: string;
      if (exact in node.options) {
        armKey = exact;
      } else {
        const category = pluralCategory(
          adjusted,
          ctx.locale,
          node.ordinal ? "ordinal" : "cardinal",
        );
        armKey = category in node.options ? category : "other";
      }
      const arm = node.options[armKey] ?? node.options.other;
      return evalNodes(arm, ctx, adjusted);
    }
    case "tag":
      // String evaluator flattens tags to their inner text.
      return evalNodes(node.children, ctx, pound);
    default:
      return "";
  }
}

/** Evaluate an ICU AST to a plain string. */
export function evaluate(message: Message, ctx: EvalContext): string {
  return evalNodes(message, ctx);
}

// ---- Rich-text parts (for React) ----------------------------------------

export type Part =
  | { type: "text"; value: string }
  | { type: "tag"; name: string; children: Part[] };

function partsFromNodes(nodes: Message, ctx: EvalContext, pound?: number): Part[] {
  const parts: Part[] = [];
  let buffer = "";
  const flush = () => {
    if (buffer) {
      parts.push({ type: "text", value: buffer });
      buffer = "";
    }
  };
  for (const node of nodes) {
    if (node.type === "tag") {
      flush();
      parts.push({ type: "tag", name: node.name, children: partsFromNodes(node.children, ctx, pound) });
    } else {
      buffer += evalNode(node, ctx, pound);
    }
  }
  flush();
  return parts;
}

/**
 * Evaluate to structured parts, preserving tag boundaries so a React renderer can
 * map `<b>` → <strong>, `<link>` → <a>, etc. Non-tag content is collapsed to text.
 */
export function evaluateParts(message: Message, ctx: EvalContext): Part[] {
  return partsFromNodes(message, ctx);
}
