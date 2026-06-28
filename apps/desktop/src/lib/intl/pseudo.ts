// Pseudo-localization — a QA transform that surfaces i18n bugs without a real
// translation. It (1) accents Latin letters so untranslated/hard-coded strings
// stand out, (2) pads length to expose layout that can't grow (~30-50% like
// German), and (3) brackets each message so truncation/clipping is obvious.
//
// Crucially it does NOT touch ICU placeholders (`{name}`, `{n, plural, …}`), the
// `#` pound, or rich-text tags (`<b>…</b>`) — mangling those would break the
// message. We tokenise the source and only accent the literal runs.

const ACCENT_MAP: Record<string, string> = {
  a: "á", b: "ƀ", c: "ç", d: "ð", e: "é", f: "ƒ", g: "ĝ", h: "ĥ", i: "í",
  j: "ĵ", k: "ķ", l: "ļ", m: "ɱ", n: "ñ", o: "ö", p: "þ", q: "ɋ", r: "ŕ",
  s: "š", t: "ţ", u: "ü", v: "ṽ", w: "ŵ", x: " x", y: "ý", z: "ž",
  A: "Á", B: "Ɓ", C: "Ç", D: "Ð", E: "É", F: "Ƒ", G: "Ĝ", H: "Ĥ", I: "Í",
  J: "Ĵ", K: "Ķ", L: "Ļ", M: "Ɱ", N: "Ñ", O: "Ö", P: "Þ", Q: "Ɋ", R: "Ŕ",
  S: "Š", T: "Ţ", U: "Ü", V: "Ṽ", W: "Ŵ", X: "X", Y: "Ý", Z: "Ž",
};

export interface PseudoOptions {
  /** Multiply literal length toward this factor by repeating vowels (default 1.4). */
  expand?: number;
  /** Wrap the whole message in brackets to expose clipping (default true). */
  brackets?: boolean;
  /** Accent Latin letters (default true). */
  accent?: boolean;
}

const DEFAULTS: Required<PseudoOptions> = {
  expand: 1.4,
  brackets: true,
  accent: true,
};

/** Accent a run of plain literal text and pad it toward the expand factor. */
function transformLiteral(text: string, opts: Required<PseudoOptions>): string {
  let out = "";
  let letters = 0;
  let padded = 0;
  for (const ch of text) {
    if (opts.accent && ACCENT_MAP[ch]) {
      out += ACCENT_MAP[ch];
    } else {
      out += ch;
    }
    // Count any Latin letter toward the expansion budget (independent of accenting).
    if (/[A-Za-z]/.test(ch)) letters++;
    // Pad after vowels to lengthen the string without harming readability.
    if (opts.expand > 1 && "aeiouAEIOU".includes(ch)) {
      const want = Math.round(letters * (opts.expand - 1));
      if (padded < want) {
        out += ch;
        padded++;
      }
    }
  }
  return out;
}

/**
 * Tokenise an ICU source into "code" spans (placeholders/tags we must not touch)
 * and "literal" spans (free text we accent). We don't fully parse here — a robust
 * scanner that recognises `{…}` (balanced), `#`, and `<…>` is enough and keeps
 * pseudo independent of the parser.
 */
function splitTokens(src: string): Array<{ code: boolean; text: string }> {
  const tokens: Array<{ code: boolean; text: string }> = [];
  let i = 0;
  let literal = "";
  const flushLiteral = () => {
    if (literal) {
      tokens.push({ code: false, text: literal });
      literal = "";
    }
  };
  while (i < src.length) {
    const ch = src[i];
    if (ch === "{") {
      flushLiteral();
      // consume a balanced brace group
      let depth = 0;
      let j = i;
      for (; j < src.length; j++) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") {
          depth--;
          if (depth === 0) {
            j++;
            break;
          }
        }
      }
      tokens.push({ code: true, text: src.slice(i, j) });
      i = j;
      continue;
    }
    if (ch === "<") {
      // consume a tag up to the next '>'
      const end = src.indexOf(">", i);
      if (end >= 0) {
        flushLiteral();
        tokens.push({ code: true, text: src.slice(i, end + 1) });
        i = end + 1;
        continue;
      }
    }
    if (ch === "#") {
      flushLiteral();
      tokens.push({ code: true, text: "#" });
      i++;
      continue;
    }
    if (ch === "'") {
      // keep ICU-quoted spans verbatim so escaping still works
      flushLiteral();
      let j = i + 1;
      if (src[j] === "'") {
        tokens.push({ code: true, text: "''" });
        i = j + 1;
        continue;
      }
      while (j < src.length && src[j] !== "'") j++;
      tokens.push({ code: true, text: src.slice(i, Math.min(j + 1, src.length)) });
      i = j + 1;
      continue;
    }
    literal += ch;
    i++;
  }
  flushLiteral();
  return tokens;
}

/**
 * Pseudo-localize an ICU source string. Placeholders, tags, the pound, and quoted
 * spans pass through untouched; literal text is accented and expanded. The result
 * is still a valid ICU message (so it can be fed to the engine in QA mode).
 *
 *   "Hi {name}, you have {n, plural, one {# msg} other {# msgs}}"
 *   → "⟦Ħíí {name}, ýööü ĥávé {n, plural, one {# msg} other {# msgs}}⟧"
 */
export function pseudoLocalize(src: string, options: PseudoOptions = {}): string {
  const opts = { ...DEFAULTS, ...options };
  const tokens = splitTokens(src);
  let body = "";
  for (const tok of tokens) {
    body += tok.code ? tok.text : transformLiteral(tok.text, opts);
  }
  return opts.brackets ? `⟦${body}⟧` : body;
}

/** Deep-pseudo a whole catalog tree (preserving structure). */
export function pseudoLocalizeCatalog<T extends Record<string, unknown>>(
  tree: T,
  options?: PseudoOptions,
): T {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(tree)) {
    if (typeof value === "string") {
      out[key] = pseudoLocalize(value, options);
    } else if (value && typeof value === "object") {
      out[key] = pseudoLocalizeCatalog(value as Record<string, unknown>, options);
    } else {
      out[key] = value;
    }
  }
  return out as T;
}
