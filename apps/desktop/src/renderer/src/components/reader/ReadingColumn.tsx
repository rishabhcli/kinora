import { queryKeys } from "@kinora/core";
import { useQuery } from "@tanstack/react-query";
import { type CSSProperties, useEffect, useMemo, useRef } from "react";

import { api } from "../../lib/api";
import { fontStack, type ReadingSettings, type ReadingTheme } from "../../lib/readingTheme";

interface WordBox {
  word_index?: number;
  text?: string;
}

interface ReadingColumnProps {
  bookId: string;
  page: number;
  numPages: number | null;
  title: string;
  chapterLabel: string;
  highlightWordIndex: number | null;
  settings: ReadingSettings;
  theme: ReadingTheme;
  onSeekWord: (word: number) => void;
  onTurnPage: (page: number) => void;
}

interface RenderWord {
  /** Global word index (for seeking + karaoke), or null for plain punctuation. */
  index: number | null;
  text: string;
}

/**
 * Build the readable token stream for a page. We prefer the page's `text`
 * (clean prose) for typographic quality and align it positionally to the
 * `word_boxes` (which carry the global `word_index` the playhead highlights);
 * since both are page-ordered, the nth visible word maps to the nth box. If
 * `text` is absent we fall back to the boxes' own text. (§9.4)
 */
function buildWords(text: string | null | undefined, boxes: WordBox[]): RenderWord[] {
  const indexed = boxes
    .filter((b) => typeof b.word_index === "number")
    .map((b) => b.word_index as number);

  if (text && text.trim()) {
    const tokens = text.trim().split(/\s+/);
    let cursor = 0;
    return tokens.map((token) => {
      const index = cursor < indexed.length ? (indexed[cursor] as number) : null;
      cursor += 1;
      return { index, text: token };
    });
  }

  return boxes
    .filter((b) => typeof b.word_index === "number")
    .map((b) => ({ index: b.word_index as number, text: b.text ?? "" }));
}

/**
 * The left pane: a comfortable, theme-driven serif reading column rendering the
 * real page prose as a single paginated leaf. The word matching the playhead is
 * painted (karaoke) and auto-scrolled into view; clicking any word seeks the
 * shared playhead there. Themes restyle the leaf live via CSS custom properties.
 */
export function ReadingColumn({
  bookId,
  page,
  numPages,
  title,
  chapterLabel,
  highlightWordIndex,
  settings,
  theme,
  onSeekWord,
  onTurnPage,
}: ReadingColumnProps) {
  const safePage = Math.max(1, page || 1);
  const activeRef = useRef<HTMLButtonElement | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: queryKeys.page(bookId, safePage),
    enabled: Boolean(bookId),
    staleTime: 5 * 60 * 1000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/books/{book_id}/pages/{page_number}", {
        params: { path: { book_id: bookId, page_number: safePage } },
      });
      if (error || !data) throw new Error("failed to load page");
      return data;
    },
  });

  const words = useMemo(
    () => buildWords(data?.text, (data?.word_boxes ?? []) as WordBox[]),
    [data?.text, data?.word_boxes],
  );

  // A printed-book drop cap on the opening paragraph — only on page one, and only
  // when the page actually begins with a letter (so we never enlarge punctuation
  // or a page that resumes mid-sentence).
  const dropCap = safePage === 1 && /^\p{L}/u.test(words[0]?.text ?? "");

  // Keep the spoken word in view as playback advances, gently.
  useEffect(() => {
    if (highlightWordIndex === null) return;
    activeRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlightWordIndex]);

  const total = numPages ?? safePage;
  const canPrev = safePage > 1;
  const canNext = safePage < total;

  const scopeStyle = {
    ...(theme.vars as Record<string, string>),
    filter: settings.brightness < 1 ? `brightness(${settings.brightness})` : undefined,
  } as CSSProperties;

  const leafStyle: CSSProperties = {
    fontFamily: fontStack(settings.fontFamily),
    fontSize: `${settings.fontSize}px`,
    lineHeight: settings.lineSpacing,
  };

  return (
    <div
      data-reading-theme={theme.id}
      className="reading-scope flex h-full min-h-0 flex-col"
      style={scopeStyle}
    >
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-10 md:px-10">
        <article
          className="reading-leaf mx-auto w-full max-w-[34rem] rounded-[14px] px-8 py-12 md:px-12 md:py-16"
          style={leafStyle}
        >
          <header className="mb-9">
            <p
              className="font-sans text-[11px] font-semibold uppercase tracking-[0.22em]"
              style={{ color: "var(--page-accent)" }}
            >
              {chapterLabel}
            </p>
            <h1 className="mt-3 font-display text-[1.7em] font-semibold leading-tight">{title}</h1>
            <div className="mt-5 h-px w-12" style={{ background: "var(--page-accent)" }} />
          </header>

          {isLoading && (
            <div aria-hidden className="space-y-3">
              {[100, 96, 99, 92, 97, 88, 0, 95, 90].map((w, i) =>
                w === 0 ? (
                  <div key={i} className="h-3" />
                ) : (
                  <div
                    key={i}
                    className="shimmer h-[0.85em] rounded-[3px]"
                    style={
                      {
                        width: `${w}%`,
                        background: "color-mix(in srgb, var(--page-ink) 9%, transparent)",
                        "--shimmer-delay": `${i * 70}ms`,
                      } as CSSProperties
                    }
                  />
                ),
              )}
            </div>
          )}

          {!isLoading && words.length === 0 && (
            <p className="font-sans text-sm" style={{ color: "var(--page-ink-soft)" }}>
              This page has no text to read along.
            </p>
          )}

          {!isLoading && words.length > 0 && (
            <p className="font-display [text-wrap:pretty]" style={{ hyphens: "auto" }}>
              {words.map((word, i) => {
                if (word.index === null) return <span key={`p-${i}`}>{word.text} </span>;
                // The opening word carries the drop cap: its first letter is
                // rendered large and floated, the rest sits beside it.
                const isFirstWord = dropCap && i === 0;
                return (
                  <button
                    key={`w-${word.index}-${i}`}
                    type="button"
                    ref={word.index === highlightWordIndex ? activeRef : undefined}
                    data-active={word.index === highlightWordIndex}
                    onClick={() => onSeekWord(word.index as number)}
                    className="word inline px-[1px] text-left align-baseline"
                  >
                    {isFirstWord ? (
                      <>
                        <span className="drop-cap-letter">{word.text.slice(0, 1)}</span>
                        {word.text.slice(1)}
                      </>
                    ) : (
                      word.text
                    )}{" "}
                  </button>
                );
              })}
            </p>
          )}
        </article>
      </div>

      {/* Page footer: progress + paginated nav (the "page-turn" feel). */}
      <footer
        className="flex shrink-0 items-center justify-between px-6 py-3 md:px-10"
        style={{ color: "var(--page-ink-soft)" }}
      >
        <button
          type="button"
          disabled={!canPrev}
          onClick={() => onTurnPage(safePage - 1)}
          className="flex h-8 items-center gap-1.5 rounded-full px-3 font-sans text-[13px] opacity-80 transition enabled:hover:opacity-100 disabled:opacity-25 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m15 18-6-6 6-6" />
          </svg>
          Previous
        </button>
        <span className="font-sans text-[11px] uppercase tracking-[0.18em] opacity-70">
          Page {safePage} of {total}
        </span>
        <button
          type="button"
          disabled={!canNext}
          onClick={() => onTurnPage(safePage + 1)}
          className="flex h-8 items-center gap-1.5 rounded-full px-3 font-sans text-[13px] opacity-80 transition enabled:hover:opacity-100 disabled:opacity-25 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
        >
          Next
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m9 18 6-6-6-6" />
          </svg>
        </button>
      </footer>
    </div>
  );
}
