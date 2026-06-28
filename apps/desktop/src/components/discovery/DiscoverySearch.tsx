// DiscoverySearch — a full search surface: a text box (with semantic expansion),
// a faceted sidebar (genre / era / author / status) whose counts update live,
// and a ranked grid of results. All ranking/faceting is delegated to the pure
// cores (search.ts, facets.ts, semantic.ts).
import { useMemo, useState } from "react";
import type { DiscoveryBook, FacetSelection } from "../../lib/discovery/types";
import { search, didYouMean } from "../../lib/discovery/search";
import { semanticSearch } from "../../lib/discovery/semantic";
import {
  deriveFacets,
  toggleFacetValue,
  activeFacetCount,
  hasActiveFacets,
  stateKeyFromLabel,
} from "../../lib/discovery/facets";
import BookPreviewCard, { type PreviewActions } from "./BookPreviewCard";

interface DiscoverySearchProps extends PreviewActions {
  books: DiscoveryBook[];
  /** Seed the query (e.g. from the ⌘K palette or the nav search). */
  initialQuery?: string;
  /** "semantic" blends synonym-expanded matches in; "exact" is field-tier only. */
  mode?: "exact" | "semantic";
}

type FacetKey = "genre" | "era" | "author" | "state";

export default function DiscoverySearch({
  books,
  initialQuery = "",
  mode = "semantic",
  ...actions
}: DiscoverySearchProps) {
  const [sel, setSel] = useState<FacetSelection>({ text: initialQuery });

  const facets = useMemo(() => deriveFacets(books, sel), [books, sel]);

  // Results: faceted exact search, optionally unioned with semantic hits so a
  // "space adventure" query still surfaces SF even without a literal token.
  const results = useMemo(() => {
    const exact = search(books, sel);
    if (mode !== "semantic" || !sel.text?.trim()) return exact.map((h) => h.book);
    const seen = new Set(exact.map((h) => h.book.id));
    const facetOnly = new Set(search(books, { ...sel, text: "" }).map((h) => h.book.id));
    const sem = semanticSearch(books, sel.text)
      .map((h) => h.book)
      .filter((b) => !seen.has(b.id) && facetOnly.has(b.id)); // respect active facets
    return [...exact.map((h) => h.book), ...sem];
  }, [books, sel, mode]);

  const setText = (text: string) => setSel((s) => ({ ...s, text }));

  const toggle = (key: FacetKey, value: string) =>
    setSel((s) => {
      if (key === "state") {
        const stateKey = stateKeyFromLabel(value);
        if (!stateKey) return s;
        return { ...s, state: toggleFacetValue(s.state, stateKey) as FacetSelection["state"] };
      }
      return { ...s, [key]: toggleFacetValue(s[key], value) };
    });

  const isSelected = (key: FacetKey, value: string): boolean => {
    if (key === "state") {
      const stateKey = stateKeyFromLabel(value);
      return Boolean(stateKey && (sel.state ?? []).includes(stateKey));
    }
    return (sel[key] ?? []).includes(value);
  };

  const clearAll = () => setSel({ text: sel.text });
  const activeCount = activeFacetCount(sel);

  // "Did you mean …" — only when a text query yielded nothing.
  const suggestion = useMemo(() => {
    if (results.length > 0 || !sel.text?.trim()) return null;
    return didYouMean(books, sel.text);
  }, [results.length, books, sel.text]);

  return (
    <div className="max-w-[1280px] mx-auto px-6 pt-6">
      {/* Search box */}
      <div className="flex items-center gap-2.5 mb-5 max-w-[640px]">
        <div className="flex-1 flex items-center gap-2.5 px-4 py-2.5 rounded-full" style={{ background: "rgba(40,38,34,0.85)", border: "1px solid rgba(255,255,255,0.08)" }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" style={{ color: "#c4b8aa" }} aria-hidden>
            <circle cx="11" cy="11" r="7" />
            <path d="M16.5 16.5 21 21" />
          </svg>
          <input
            type="search"
            aria-label="Search the library"
            value={sel.text ?? ""}
            onChange={(e) => setText(e.target.value)}
            placeholder="Search by title, author, or theme…"
            className="flex-1 bg-transparent border-none outline-none text-[13px] text-kinora-text placeholder:text-kinora-muted"
            autoComplete="off"
          />
        </div>
      </div>

      <div className="flex gap-6">
        {/* Facet sidebar */}
        <aside className="w-[200px] flex-shrink-0" aria-label="Filters">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-kinora-muted">Filters</h3>
            {activeCount > 0 && (
              <button onClick={clearAll} className="text-[10px] text-kinora-muted hover:text-kinora-text transition-colors">
                Clear ({activeCount})
              </button>
            )}
          </div>
          {facets.map((facet) => (
            <fieldset key={facet.key} className="mb-4 border-0 p-0 m-0">
              <legend className="text-[10px] font-semibold text-kinora-text/80 mb-1.5">{facet.label}</legend>
              <div className="flex flex-col gap-0.5 max-h-[180px] overflow-y-auto hide-scrollbar">
                {facet.values.map((v) => {
                  const checked = isSelected(facet.key, v.value);
                  return (
                    <button
                      key={v.value}
                      role="checkbox"
                      aria-checked={checked}
                      aria-pressed={checked}
                      onClick={() => toggle(facet.key, v.value)}
                      className="flex items-center justify-between text-left rounded px-2 py-1 text-[11px] transition-colors hover:bg-white/[0.04]"
                      style={{ color: checked ? "rgba(212,164,78,0.95)" : "rgba(232,226,216,0.72)" }}
                    >
                      <span className="truncate flex items-center gap-1.5">
                        <span
                          aria-hidden
                          className="inline-block w-2.5 h-2.5 rounded-sm flex-shrink-0"
                          style={{
                            background: checked ? "rgba(212,164,78,0.9)" : "transparent",
                            border: "1px solid rgba(255,255,255,0.2)",
                          }}
                        />
                        {v.value}
                      </span>
                      <span className="text-[9px] text-kinora-muted ml-1">{v.count}</span>
                    </button>
                  );
                })}
              </div>
            </fieldset>
          ))}
        </aside>

        {/* Results grid */}
        <div className="flex-1 min-w-0">
          <p className="text-[12px] text-kinora-muted mb-3" aria-live="polite" data-testid="result-count">
            {results.length} {results.length === 1 ? "result" : "results"}
            {hasActiveFacets(sel) ? "" : " · everything"}
          </p>
          {results.length === 0 ? (
            <div className="py-16 text-center">
              <p className="text-[13px] text-kinora-text mb-1">Nothing matched.</p>
              {suggestion ? (
                <p className="text-[11px] text-kinora-muted">
                  Did you mean{" "}
                  <button
                    onClick={() => setText(suggestion)}
                    className="underline hover:text-kinora-text transition-colors"
                    style={{ color: "rgba(212,164,78,0.92)" }}
                  >
                    {suggestion}
                  </button>
                  ?
                </p>
              ) : (
                <p className="text-[11px] text-kinora-muted">Try fewer filters or a different search.</p>
              )}
            </div>
          ) : (
            <div className="grid gap-x-4 gap-y-6" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))" }}>
              {results.map((book) => (
                <BookPreviewCard key={book.id} book={book} {...actions} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
