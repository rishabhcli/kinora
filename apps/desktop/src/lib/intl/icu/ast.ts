// ICU MessageFormat AST.
//
// A parsed message is a list of nodes. Literal text, simple argument
// interpolation, formatted arguments (number/date/time/currency/…), and the
// three submessage selectors (plural / selectordinal / select). Tag nodes carry
// rich-text markup like `<b>…</b>` for the React renderer to map to elements.

export type MessageNode =
  | LiteralNode
  | ArgumentNode
  | FormatNode
  | PluralNode
  | SelectNode
  | PoundNode
  | TagNode;

/** Raw literal text. `value` already has ICU quoting/escapes resolved. */
export interface LiteralNode {
  type: "literal";
  value: string;
}

/** `{name}` — substitute the raw argument value. */
export interface ArgumentNode {
  type: "argument";
  arg: string;
}

/**
 * `{count, number}`, `{count, number, percent}`, `{when, date, medium}`,
 * `{price, number, ::currency/USD}` … — a typed format with an optional style.
 */
export interface FormatNode {
  type: "format";
  arg: string;
  /** "number" | "date" | "time" | "currency" | "percent" | "unit" | … */
  format: string;
  /** Optional style token after the second comma ("medium", "::compact-short", …). */
  style?: string;
}

/** `{count, plural, ...}` or `{n, selectordinal, ...}`. */
export interface PluralNode {
  type: "plural";
  arg: string;
  ordinal: boolean;
  /** `offset:N` applied to the value before category resolution and `#`. */
  offset: number;
  /** Arm key ("one", "few", "=0", …) → submessage. */
  options: Record<string, MessageNode[]>;
}

/** `{gender, select, male {...} female {...} other {...}}`. */
export interface SelectNode {
  type: "select";
  arg: string;
  options: Record<string, MessageNode[]>;
}

/** The `#` placeholder inside a plural arm — the (offset-adjusted) number. */
export interface PoundNode {
  type: "pound";
}

/** `<b>bold</b>` rich-text markup. Children are themselves message nodes. */
export interface TagNode {
  type: "tag";
  name: string;
  children: MessageNode[];
}

export type Message = MessageNode[];
