// Key extractor — statically scans source text for translation call-sites and
// reports the message keys the code references. Pairing the extracted set with a
// catalog tells you (a) which keys are used-but-undefined and (b) which are
// defined-but-unused (dead translations).
//
// This is a pragmatic regex scanner, not a full TS parser: it recognises the
// common call shapes `t("k")`, `t('k')`, `i18n.t("k")`, `<Trans i18nKey="k">`,
// and the typed `tx("k")`. Dynamic keys (`t(varName)`) are reported separately so
// they aren't mistaken for literals.

export interface ExtractedKey {
  key: string;
  /** 1-based line where the call appears. */
  line: number;
  /** The call form that matched, e.g. "t" or "Trans". */
  via: string;
}

export interface ExtractResult {
  keys: ExtractedKey[];
  /** Count of call-sites whose key was a non-literal expression. */
  dynamic: number;
}

// `t("key")`, `t('key')`, `i18n.t("key")`, `tx("key")` — capture function + key.
const CALL_RE = /\b(t|tx|i18n\.t|i18next\.t)\(\s*(["'`])((?:\\.|(?!\2).)*)\2/g;
// `<Trans i18nKey="key">` or `i18nKey={"key"}`
const TRANS_RE = /i18nKey\s*=\s*(?:\{?\s*)(["'`])((?:\\.|(?!\1).)*)\1/g;
// `t(` followed by something that is NOT an opening quote → dynamic key
const DYNAMIC_RE = /\b(?:t|tx)\(\s*(?![\s)])(?!["'`])/g;

function lineOf(text: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index && i < text.length; i++) {
    if (text[i] === "\n") line++;
  }
  return line;
}

/** Extract translation keys referenced in a single source string. */
export function extractKeys(source: string): ExtractResult {
  const keys: ExtractedKey[] = [];
  let dynamic = 0;

  CALL_RE.lastIndex = 0;
  for (let m = CALL_RE.exec(source); m; m = CALL_RE.exec(source)) {
    keys.push({ key: m[3], line: lineOf(source, m.index), via: m[1] });
  }

  TRANS_RE.lastIndex = 0;
  for (let m = TRANS_RE.exec(source); m; m = TRANS_RE.exec(source)) {
    keys.push({ key: m[2], line: lineOf(source, m.index), via: "Trans" });
  }

  DYNAMIC_RE.lastIndex = 0;
  for (let m = DYNAMIC_RE.exec(source); m; m = DYNAMIC_RE.exec(source)) {
    dynamic++;
  }

  // De-dup identical (key,line) pairs that the call + a stray match could double.
  const seen = new Set<string>();
  const unique = keys.filter((k) => {
    const id = `${k.key}@${k.line}@${k.via}`;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });

  return { keys: unique, dynamic };
}

/** The distinct set of literal keys referenced across many source strings. */
export function extractKeySet(sources: Iterable<string>): Set<string> {
  const set = new Set<string>();
  for (const src of sources) {
    for (const k of extractKeys(src).keys) set.add(k.key);
  }
  return set;
}

export interface CoverageReport {
  /** Keys referenced in code but absent from the catalog (broken at runtime). */
  undefinedKeys: string[];
  /** Keys defined in the catalog but never referenced in code (dead strings). */
  unusedKeys: string[];
}

/**
 * Cross-reference the keys used in source against the keys a catalog defines.
 * `catalogKeys` is the flat (dotted) key set of the source catalog.
 */
export function crossReference(
  usedInCode: Set<string>,
  catalogKeys: Iterable<string>,
): CoverageReport {
  const defined = new Set(catalogKeys);
  const undefinedKeys: string[] = [];
  for (const k of usedInCode) {
    if (!defined.has(k)) undefinedKeys.push(k);
  }
  const unusedKeys: string[] = [];
  for (const k of defined) {
    if (!usedInCode.has(k)) unusedKeys.push(k);
  }
  return {
    undefinedKeys: undefinedKeys.sort(),
    unusedKeys: unusedKeys.sort(),
  };
}
