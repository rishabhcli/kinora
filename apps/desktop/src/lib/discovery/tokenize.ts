// Text normalization, tokenization, and fuzzy matching — the pure core under
// search + the command palette. No DOM, no deps; deterministic.

/** Lowercase, strip diacritics, collapse punctuation to spaces. The single
 *  normalization used everywhere so "Brontë" matches "bronte". */
export function normalize(input: string): string {
  return input
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "") // strip combining marks (accents)
    .toLowerCase()
    .replace(/[^a-z0-9\s]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Split normalized text into unique-order-preserving tokens. */
export function tokenize(input: string): string[] {
  const norm = normalize(input);
  if (!norm) return [];
  return norm.split(" ");
}

/** Unique tokens (set semantics, stable order). */
export function uniqueTokens(input: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const tok of tokenize(input)) {
    if (!seen.has(tok)) {
      seen.add(tok);
      out.push(tok);
    }
  }
  return out;
}

/** Bounded Levenshtein distance: returns the edit distance, but stops early once
 *  it exceeds `max` (returns max+1). Keeps fuzzy matching cheap on long inputs. */
export function levenshtein(a: string, b: string, max = Infinity): number {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;
  if (Math.abs(a.length - b.length) > max) return max + 1;

  let prev = new Array<number>(b.length + 1);
  let curr = new Array<number>(b.length + 1);
  for (let j = 0; j <= b.length; j++) prev[j] = j;

  for (let i = 1; i <= a.length; i++) {
    curr[0] = i;
    let rowMin = curr[0];
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost);
      if (curr[j] < rowMin) rowMin = curr[j];
    }
    // Whole row already beyond budget — no path back under it.
    if (rowMin > max) return max + 1;
    [prev, curr] = [curr, prev];
  }
  return prev[b.length];
}

/** True if `needle`'s characters appear in `haystack` in order (the classic
 *  subsequence/"fzf" test). Both should be normalized first. */
export function isSubsequence(needle: string, haystack: string): boolean {
  if (!needle) return true;
  let i = 0;
  for (let j = 0; j < haystack.length && i < needle.length; j++) {
    if (needle[i] === haystack[j]) i++;
  }
  return i === needle.length;
}

/**
 * Subsequence match score in [0,1] (0 = no match). Rewards:
 *  - contiguous runs (consecutive matched chars)
 *  - matches at word boundaries / the very start
 *  - a tight match (fewer skipped chars)
 * This is the command-palette / quick-search scorer (fzf-like).
 */
export function fuzzyScore(query: string, target: string): number {
  const q = normalize(query);
  const t = normalize(target);
  if (!q) return 0;
  if (!t) return 0;
  if (q === t) return 1;

  let qi = 0;
  let score = 0;
  let run = 0;
  let lastMatch = -2;
  let firstMatchIdx = -1;

  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      if (firstMatchIdx === -1) firstMatchIdx = ti;
      // Boundary bonus: start of string or right after a space.
      const atBoundary = ti === 0 || t[ti - 1] === " ";
      let charScore = 1;
      if (atBoundary) charScore += 2;
      if (ti === lastMatch + 1) {
        run += 1;
        charScore += run; // reward contiguity, growing with run length
      } else {
        run = 0;
      }
      score += charScore;
      lastMatch = ti;
      qi += 1;
    }
  }

  if (qi < q.length) return 0; // not all query chars matched

  // Normalize: max possible is roughly len*(3+len) with full contiguous match at
  // start. Use a soft denominator so prefix matches score near 1, scattered near 0.
  const maxRun = q.length; // best contiguous run
  const ideal = q.length * 3 + (maxRun * (maxRun + 1)) / 2;
  let norm = score / ideal;

  // Early-match bonus: matches near the start rank above deep matches.
  const startPenalty = firstMatchIdx / (t.length + 1);
  norm = norm * (1 - 0.3 * startPenalty);

  return Math.max(0, Math.min(1, norm));
}

/** A relevance score for one query token against one field value, blending exact
 *  / prefix / substring / fuzzy tiers. Returns 0 when there is no plausible
 *  match. Inputs need not be normalized (handled internally). */
export function tokenFieldScore(token: string, field: string): number {
  const q = normalize(token);
  const f = normalize(field);
  if (!q || !f) return 0;
  if (f === q) return 1;
  if (f.startsWith(q)) return 0.85;
  // word-boundary prefix (e.g. "prejudice" in "pride and prejudice")
  if (f.split(" ").some((w) => w.startsWith(q))) return 0.7;
  if (f.includes(q)) return 0.55;
  // fuzzy for typos — only worthwhile for tokens of length ≥ 3
  if (q.length >= 3) {
    const maxEdits = q.length <= 4 ? 1 : 2;
    // compare against each word; take the best
    let best = 0;
    for (const w of f.split(" ")) {
      const d = levenshtein(q, w, maxEdits);
      if (d <= maxEdits) {
        best = Math.max(best, 0.45 * (1 - d / (maxEdits + 1)));
      }
    }
    if (best > 0) return best;
  }
  return 0;
}
