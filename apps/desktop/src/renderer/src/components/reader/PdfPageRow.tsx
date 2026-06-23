import { useQuery } from "@tanstack/react-query";
import {
  type CSSProperties,
  memo,
  type MouseEvent,
  type ReactNode,
  useCallback,
  useMemo,
  useRef,
  useState,
} from "react";

import { fontStack, type ReadingSettings } from "../../lib/readingTheme";
import {
  DEFAULT_PAGE_RATIO,
  PageLoadError,
  pageQueryOptions,
  parseWordBoxes,
  type WordBox,
} from "./pdfPage";

/** Stable empty word list so non-raster pages don't re-register every render. */
const NO_WORDS: WordBox[] = [];

interface PdfPageRowProps {
  bookId: string;
  pageNumber: number;
  /** Global word_index to paint as the karaoke highlight, or null. Routed by the
   *  container so only the page that owns the word re-renders during playback. */
  highlightWordIndex: number | null;
  /** Reflow-fallback typography (the raster path keeps the page's own layout). */
  settings: ReadingSettings;
  /** The book's learned page aspect (width/height) — reserves space before load. */
  estimatedRatio: number | null;
  /** Rendered page width in px (driven by the container's fit + zoom). */
  surfaceWidth: number;
  onSeekWord: (word: number) => void;
  /** Register this page's rendered surface + words with the container scroll-spy. */
  registerPage: (page: number, surface: HTMLElement | null, words: WordBox[]) => void;
  /** Report the first-seen page aspect (width/height) so the list can refine sizes. */
  onAspect: (ratio: number) => void;
}

/**
 * One page of the virtualised reader: the **real rasterised page** (PyMuPDF PNG,
 * §5.2) with a transparent, **selectable** text layer positioned from the word
 * boxes — click a word to seek, drag to select/copy, and the page reads in order
 * for screen readers; the spoken word lights up in place (karaoke on the raster,
 * §5.3). Loading, still-preparing (mid-ingest), failed (with retry), and
 * image-less pages each get a real state rather than an endless shimmer (§12.4).
 *
 * Memoised, with the static text layer split from the single moving highlight
 * box so karaoke advancing word-by-word never re-renders the words.
 */
export const PdfPageRow = memo(function PdfPageRow({
  bookId,
  pageNumber,
  highlightWordIndex,
  settings,
  estimatedRatio,
  surfaceWidth,
  onSeekWord,
  registerPage,
  onAspect,
}: PdfPageRowProps) {
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const [ratio, setRatio] = useState<number | null>(null); // loaded image width/height
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);

  // Read the latest seek handler without rebinding the (memoised) text layer.
  const seekRef = useRef(onSeekWord);
  seekRef.current = onSeekWord;
  const onLayerClick = useCallback((event: MouseEvent<HTMLDivElement>) => {
    // A drag that selected text is a copy gesture, not a seek.
    const selection = typeof window !== "undefined" ? window.getSelection() : null;
    if (selection && !selection.isCollapsed) return;
    const wi = (event.target as HTMLElement).getAttribute("data-wi");
    if (wi !== null) seekRef.current(Number(wi));
  }, []);

  const { data, error, refetch, isFetching } = useQuery({
    ...pageQueryOptions(bookId, pageNumber),
    enabled: Boolean(bookId) && pageNumber > 0,
  });

  const words = useMemo(
    () => parseWordBoxes(data?.word_boxes as ReadonlyArray<Record<string, unknown>> | undefined),
    [data?.word_boxes],
  );
  const imageUrl = data?.image_url ?? null;
  const useRaster = Boolean(imageUrl) && !failed;

  // Only the raster path participates in the scroll-spy: its boxes match the
  // rendered page. (Reflow re-lays the words, so their original boxes wouldn't.)
  const spyWords = useRaster ? words : NO_WORDS;
  const registerRef = useRef(registerPage);
  registerRef.current = registerPage;
  // Re-register whenever the surface or the spy words change.
  const surfaceCallback = useCallback(
    (node: HTMLDivElement | null) => {
      surfaceRef.current = node;
      registerRef.current(pageNumber, node, spyWords);
    },
    [pageNumber, spyWords],
  );

  const aspect = ratio ?? estimatedRatio ?? DEFAULT_PAGE_RATIO;
  const surfaceHeightPx = surfaceWidth / aspect;

  // The transparent, selectable text layer — depends only on the words + the
  // rendered height (font scale), so karaoke advancing never rebuilds it.
  const textLayer = useMemo(
    () =>
      words.map((word) => {
        const boxHeightPx = word.bbox[3] * surfaceHeightPx;
        return (
          <span
            key={word.word_index}
            data-wi={word.word_index}
            className="word-token"
            style={{
              left: `${word.bbox[0] * 100}%`,
              top: `${word.bbox[1] * 100}%`,
              width: `${word.bbox[2] * 100}%`,
              height: `${word.bbox[3] * 100}%`,
              fontSize: `${Math.max(4, boxHeightPx * 0.9)}px`,
            }}
          >
            {word.text}
          </span>
        );
      }),
    [words, surfaceHeightPx],
  );

  const active =
    highlightWordIndex !== null ? words.find((w) => w.word_index === highlightWordIndex) : undefined;

  const isError = Boolean(error);
  const isPreparing = error instanceof PageLoadError && error.status === 404;
  // The surface reserves space (aspect) for everything except the reflow text rung.
  const showReflow = !useRaster && !isError && data !== undefined;
  const surfaceStyle: CSSProperties = {
    width: surfaceWidth,
    aspectRatio: showReflow ? undefined : String(aspect),
  };

  return (
    <div className="flex justify-center px-5 py-4 md:px-8">
      <div ref={surfaceCallback} data-page={pageNumber} className="pdf-page relative" style={surfaceStyle}>
        {isError ? (
          <PageState>
            {isPreparing ? (
              <>
                <Spinner />
                <span>Preparing page {pageNumber}…</span>
              </>
            ) : (
              <>
                <span>Couldn’t load page {pageNumber}.</span>
                <button type="button" onClick={() => void refetch()} disabled={isFetching} className="page-retry">
                  {isFetching ? "Retrying…" : "Retry"}
                </button>
              </>
            )}
          </PageState>
        ) : useRaster ? (
          <>
            <img
              src={imageUrl as string}
              alt={`Page ${pageNumber}`}
              draggable={false}
              decoding="async"
              onLoad={(event) => {
                const el = event.currentTarget;
                if (el.naturalWidth > 0 && el.naturalHeight > 0) {
                  const r = el.naturalWidth / el.naturalHeight;
                  setRatio(r);
                  onAspect(r);
                }
                setLoaded(true);
              }}
              onError={() => setFailed(true)}
              className="block h-full w-full select-none rounded-[8px] object-contain"
              style={{ opacity: loaded ? 1 : 0, transition: "opacity 220ms ease" }}
            />
            {/* Screen-reader text in reading order (the layer below is the
                visual/selectable copy, hidden from the a11y tree to avoid echo). */}
            {data?.text ? (
              <p className="sr-only" style={{ userSelect: "none" }}>
                {data.text}
              </p>
            ) : null}
            <div className="word-layer absolute inset-0" aria-hidden onClick={onLayerClick}>
              {textLayer}
            </div>
            {active ? (
              <div
                aria-hidden
                className="karaoke-box"
                style={{
                  left: `${active.bbox[0] * 100}%`,
                  top: `${active.bbox[1] * 100}%`,
                  width: `${active.bbox[2] * 100}%`,
                  height: `${active.bbox[3] * 100}%`,
                }}
              />
            ) : null}
            {!loaded && <div aria-hidden className="shimmer absolute inset-0 rounded-[8px]" />}
          </>
        ) : showReflow ? (
          <ReflowFallback
            words={words}
            highlightWordIndex={highlightWordIndex}
            settings={settings}
            onSeekWord={onSeekWord}
          />
        ) : (
          <div aria-hidden className="shimmer absolute inset-0 rounded-[8px]" />
        )}
      </div>
    </div>
  );
});

/** A centred message card filling the page surface (preparing / failed states). */
function PageState({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="absolute inset-0 flex flex-col items-center justify-center gap-3 rounded-[8px] px-6 text-center font-sans text-[13px]"
      style={{ color: "var(--page-ink-soft)" }}
    >
      {children}
    </div>
  );
}

function Spinner() {
  return (
    <svg className="kinora-spin" width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

/**
 * The §12.4 ladder's text rung: when a page has no rasterised image we still
 * render its words as a readable, theme-driven leaf with the same click-to-seek
 * and karaoke highlight, so the reader never hits a blank page.
 */
function ReflowFallback({
  words,
  highlightWordIndex,
  settings,
  onSeekWord,
}: {
  words: WordBox[];
  highlightWordIndex: number | null;
  settings: ReadingSettings;
  onSeekWord: (word: number) => void;
}) {
  const leafStyle: CSSProperties = {
    fontFamily: fontStack(settings.fontFamily),
    fontSize: `${settings.fontSize}px`,
    lineHeight: settings.lineSpacing,
  };
  return (
    <article className="reading-leaf rounded-[10px] px-8 py-12 md:px-12 md:py-14" style={leafStyle}>
      {words.length === 0 ? (
        <p className="font-sans text-sm" style={{ color: "var(--page-ink-soft)" }}>
          This page has no text to read along.
        </p>
      ) : (
        <p className="font-display [text-wrap:pretty]" style={{ hyphens: "auto" }}>
          {words.map((word) => (
            <button
              key={word.word_index}
              type="button"
              data-active={word.word_index === highlightWordIndex}
              onClick={() => onSeekWord(word.word_index)}
              className="word inline px-[1px] text-left align-baseline"
            >
              {word.text}{" "}
            </button>
          ))}
        </p>
      )}
    </article>
  );
}
