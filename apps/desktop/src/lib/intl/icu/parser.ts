// ICU MessageFormat parser — recursive descent over the raw message string.
//
// Grammar (subset, but the practically complete one):
//
//   message       := (literal | argument | tag)*
//   argument      := '{' name '}'
//                  | '{' name ',' keyword (',' style)? '}'
//                  | '{' name ',' ('plural'|'selectordinal') ',' plural_arms '}'
//                  | '{' name ',' 'select' ',' select_arms '}'
//   plural_arms   := ('offset:' int)? (arm_key '{' message '}')+
//   select_arms   := (arm_key '{' message '}')+
//   tag           := '<' name '>' message '</' name '>'  |  '<' name '/>'
//
// ICU apostrophe quoting is honoured: '' is a literal apostrophe; '{' escapes a
// brace run; an opening quote before a special char starts a quoted span that
// ends at the next lone apostrophe.

import type { Message, MessageNode } from "./ast.ts";

export class ICUParseError extends Error {
  readonly offset: number;

  constructor(message: string, offset: number) {
    super(`ICU parse error at ${offset}: ${message}`);
    this.name = "ICUParseError";
    this.offset = offset;
  }
}

const PLURAL_KEYWORDS = new Set(["plural", "selectordinal"]);
const SIMPLE_FORMATS = new Set([
  "number",
  "date",
  "time",
  "currency",
  "percent",
  "unit",
  "spellout",
  "ordinal",
  "duration",
]);

interface ParseOptions {
  /** Parse `<tag>…</tag>` rich-text markup into TagNodes (default true). */
  tags?: boolean;
}

class Parser {
  private pos = 0;
  private readonly tags: boolean;
  private readonly src: string;

  constructor(src: string, options: ParseOptions = {}) {
    this.src = src;
    this.tags = options.tags ?? true;
  }

  parse(): Message {
    const nodes = this.parseMessage(false);
    if (this.pos < this.src.length) {
      throw new ICUParseError(`unexpected '${this.src[this.pos]}'`, this.pos);
    }
    return nodes;
  }

  /**
   * Parse a run of message nodes. When `inArm` is true we stop at an unescaped
   * `}` (the end of a plural/select submessage); when parsing a tag body we stop
   * at a `<` that begins a closing tag.
   */
  private parseMessage(inArm: boolean): Message {
    const nodes: MessageNode[] = [];
    let literal = "";

    const flush = () => {
      if (literal.length > 0) {
        nodes.push({ type: "literal", value: literal });
        literal = "";
      }
    };

    while (this.pos < this.src.length) {
      const ch = this.src[this.pos];

      if (ch === "'") {
        literal += this.readQuoted();
        continue;
      }

      if (ch === "}") {
        if (inArm) break;
        throw new ICUParseError("unexpected '}'", this.pos);
      }

      if (ch === "#") {
        flush();
        nodes.push({ type: "pound" });
        this.pos++;
        continue;
      }

      if (ch === "{") {
        // i18next-compatible `{{var}}` interpolation: a doubled brace is an
        // argument reference, NOT an ICU submessage. This makes the engine a
        // superset of the existing i18next catalogs so they work unchanged.
        if (this.src[this.pos + 1] === "{") {
          flush();
          nodes.push(this.parseDoubleBrace());
          continue;
        }
        flush();
        nodes.push(this.parseArgument());
        continue;
      }

      if (ch === "<" && this.tags && this.looksLikeTag()) {
        // A closing tag ends the current tag body — let the caller (parseTag) see it.
        if (this.src[this.pos + 1] === "/") break;
        flush();
        nodes.push(this.parseTag());
        continue;
      }

      literal += ch;
      this.pos++;
    }

    flush();
    return nodes;
  }

  /** Read an ICU-quoted span starting at the current apostrophe. */
  private readQuoted(): string {
    // src[pos] === "'"
    const next = this.src[this.pos + 1];
    if (next === "'") {
      this.pos += 2;
      return "'";
    }
    // Quoting only "activates" before a special char; otherwise a lone ' is literal.
    if (next === undefined || !"{}#<".includes(next)) {
      this.pos += 1;
      return "'";
    }
    // Quoted span: consume until the next lone apostrophe (or end of string).
    this.pos += 1; // skip opening quote
    let out = "";
    while (this.pos < this.src.length) {
      const c = this.src[this.pos];
      if (c === "'") {
        if (this.src[this.pos + 1] === "'") {
          out += "'";
          this.pos += 2;
          continue;
        }
        this.pos += 1; // closing quote
        return out;
      }
      out += c;
      this.pos += 1;
    }
    return out; // unterminated quote → treat rest as literal
  }

  /**
   * Parse an i18next-style `{{name}}` interpolation. The name may contain dots and
   * an i18next `, format` suffix (e.g. `{{count, number}}`); we keep only the
   * variable name and ignore the trailing format hint (the engine's typed format
   * nodes use the single-brace ICU form instead).
   */
  private parseDoubleBrace(): MessageNode {
    this.expect("{");
    this.expect("{");
    this.skipSpace();
    const start = this.pos;
    // i18next keys allow letters, digits, _, ., and (namespaced) :
    while (
      this.pos < this.src.length &&
      /[A-Za-z0-9_.:]/.test(this.src[this.pos])
    ) {
      this.pos++;
    }
    const arg = this.src.slice(start, this.pos);
    if (!arg) throw new ICUParseError("expected variable name in {{…}}", this.pos);
    // Skip any i18next format suffix `, foo` up to the closing braces.
    while (this.pos < this.src.length && this.src[this.pos] !== "}") this.pos++;
    this.expect("}");
    this.expect("}");
    return { type: "argument", arg };
  }

  private parseArgument(): MessageNode {
    const start = this.pos;
    this.expect("{");
    this.skipSpace();
    const arg = this.readName();
    if (!arg) throw new ICUParseError("expected argument name", this.pos);
    this.skipSpace();

    const ch = this.src[this.pos];
    if (ch === "}") {
      this.pos++;
      return { type: "argument", arg };
    }
    if (ch !== ",") {
      throw new ICUParseError("expected ',' or '}' after argument name", this.pos);
    }
    this.pos++; // ,
    this.skipSpace();
    const keyword = this.readName();
    this.skipSpace();

    if (PLURAL_KEYWORDS.has(keyword)) {
      return this.parsePlural(arg, keyword === "selectordinal");
    }
    if (keyword === "select") {
      return this.parseSelect(arg);
    }
    if (SIMPLE_FORMATS.has(keyword)) {
      return this.parseFormat(arg, keyword);
    }
    throw new ICUParseError(`unknown argument keyword '${keyword}'`, start);
  }

  private parseFormat(arg: string, format: string): MessageNode {
    let style: string | undefined;
    if (this.src[this.pos] === ",") {
      this.pos++;
      this.skipSpace();
      style = this.readStyle();
    }
    this.skipSpace();
    this.expect("}");
    return { type: "format", arg, format, style: style || undefined };
  }

  private parsePlural(arg: string, ordinal: boolean): MessageNode {
    this.expect(",");
    this.skipSpace();
    let offset = 0;
    // optional `offset:N`
    if (this.src.startsWith("offset:", this.pos)) {
      this.pos += "offset:".length;
      this.skipSpace();
      const num = this.readName();
      offset = Number.parseInt(num, 10);
      if (Number.isNaN(offset)) throw new ICUParseError("bad offset", this.pos);
      this.skipSpace();
    }
    const options = this.parseArms();
    if (!("other" in options)) {
      throw new ICUParseError("plural requires an 'other' arm", this.pos);
    }
    return { type: "plural", arg, ordinal, offset, options };
  }

  private parseSelect(arg: string): MessageNode {
    this.expect(",");
    this.skipSpace();
    const options = this.parseArms();
    if (!("other" in options)) {
      throw new ICUParseError("select requires an 'other' arm", this.pos);
    }
    return { type: "select", arg, options };
  }

  /** Parse one-or-more `key {submessage}` arms until the closing `}`. */
  private parseArms(): Record<string, MessageNode[]> {
    const options: Record<string, MessageNode[]> = {};
    while (true) {
      this.skipSpace();
      if (this.src[this.pos] === "}") break;
      const key = this.readArmKey();
      if (!key) throw new ICUParseError("expected arm key", this.pos);
      this.skipSpace();
      this.expect("{");
      options[key] = this.parseMessage(true);
      this.expect("}");
    }
    this.expect("}");
    return options;
  }

  private parseTag(): MessageNode {
    this.expect("<");
    const name = this.readName();
    this.skipSpace();
    // self-closing <name/>
    if (this.src[this.pos] === "/") {
      this.pos++;
      this.expect(">");
      return { type: "tag", name, children: [] };
    }
    this.expect(">");
    const children = this.parseMessage(false);
    // closing tag
    this.expect("<");
    this.expect("/");
    const closing = this.readName();
    if (closing !== name) {
      throw new ICUParseError(`mismatched tag </${closing}> for <${name}>`, this.pos);
    }
    this.skipSpace();
    this.expect(">");
    return { type: "tag", name, children };
  }

  // ---- low-level scanners ----

  private looksLikeTag(): boolean {
    // `<` followed by a letter (open) or `/letter` (close)
    const a = this.src[this.pos + 1];
    if (a === "/") {
      const b = this.src[this.pos + 2];
      return b !== undefined && /[A-Za-z]/.test(b);
    }
    return a !== undefined && /[A-Za-z]/.test(a);
  }

  private readName(): string {
    const start = this.pos;
    while (this.pos < this.src.length && /[A-Za-z0-9_]/.test(this.src[this.pos])) {
      this.pos++;
    }
    return this.src.slice(start, this.pos);
  }

  /** Arm keys allow `=0`, `=42`, or identifiers like `one`, `male`. */
  private readArmKey(): string {
    const start = this.pos;
    if (this.src[this.pos] === "=") {
      this.pos++;
      while (this.pos < this.src.length && /[0-9]/.test(this.src[this.pos])) this.pos++;
      return this.src.slice(start, this.pos);
    }
    return this.readName();
  }

  /** A style token runs to the matching `}` (can contain spaces, e.g. `::compact short`). */
  private readStyle(): string {
    const start = this.pos;
    let depth = 0;
    while (this.pos < this.src.length) {
      const c = this.src[this.pos];
      if (c === "{") depth++;
      else if (c === "}") {
        if (depth === 0) break;
        depth--;
      }
      this.pos++;
    }
    return this.src.slice(start, this.pos).trim();
  }

  private skipSpace(): void {
    while (this.pos < this.src.length && /\s/.test(this.src[this.pos])) this.pos++;
  }

  private expect(ch: string): void {
    if (this.src[this.pos] !== ch) {
      throw new ICUParseError(`expected '${ch}' but found '${this.src[this.pos] ?? "EOF"}'`, this.pos);
    }
    this.pos++;
  }
}

/** Parse an ICU MessageFormat string into an AST. Throws ICUParseError on bad input. */
export function parse(src: string, options?: ParseOptions): Message {
  return new Parser(src, options).parse();
}

/** Parse, returning `null` instead of throwing (used by the linter). */
export function tryParse(src: string, options?: ParseOptions): Message | null {
  try {
    return parse(src, options);
  } catch {
    return null;
  }
}
