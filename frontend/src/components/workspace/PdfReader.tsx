import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { books as booksApi } from "../../api/client";
import type { Page } from "../../api/types";
import { useReducedMotion } from "../../hooks/useReducedMotion";
import {
  computeFocusWord,
  type PageLayout,
  type PositionedWord,
} from "../../lib/scrollspy";
import type { SyncEngine } from "../../sync/SyncEngine";
import { useSyncSnapshot } from "../../sync/useSyncEngine";
import { WordHighlightLayer } from "./WordHighlightLayer";

interface PdfReaderProps {
  bookId: string;
  numPages: number;
  engine: SyncEngine;
}

const DEFAULT_ASPECT = 1.4142; // height/width (A-series portrait) until measured.
const PAGE_GAP = 18;
const HORIZONTAL_PADDING = 24;

type PageEntry = Page | "error" | undefined;

function buildLayout(
  numPages: number,
  aspects: Map<number, number>,
  width: number,
): { layout: PageLayout[]; total: number } {
  const layout: PageLayout[] = [];
  let top = 0;
  for (let p = 1; p <= numPages; p += 1) {
    const height = width * (aspects.get(p) ?? DEFAULT_ASPECT);
    layout.push({ page: p, top, height });
    top += height + PAGE_GAP;
  }
  return { layout, total: top };
}

export function PdfReader({ bookId, numPages, engine }: PdfReaderProps) {
  const snap = useSyncSnapshot(engine);
  const reduceMotion = useReducedMotion();

  const scrollRef = useRef<HTMLDivElement>(null);
  const inflight = useRef<Set<number>>(new Set());
  const programmaticUntil = useRef(0);
  const rafPending = useRef(false);

  const [pages, setPages] = useState<Map<number, PageEntry>>(new Map());
  const [aspects, setAspects] = useState<Map<number, number>>(new Map());
  const [width, setWidth] = useState(560);
  const [viewport, setViewport] = useState({ scrollTop: 0, height: 800 });

  // Track the reader's width + height (drives both layout and virtualization).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return undefined;
    const measure = () => {
      setWidth(Math.max(240, el.clientWidth - HORIZONTAL_PADDING * 2));
      setViewport((v) => ({ ...v, height: el.clientHeight }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { layout, total } = useMemo(
    () => buildLayout(numPages, aspects, width),
    [numPages, aspects, width],
  );
  const layoutByPage = useMemo(() => {
    const m = new Map<number, PageLayout>();
    for (const l of layout) m.set(l.page, l);
    return m;
  }, [layout]);

  const visiblePages = useMemo(() => {
    const overscan = viewport.height;
    const top0 = viewport.scrollTop - overscan;
    const bot0 = viewport.scrollTop + viewport.height + overscan;
    return layout
      .filter((l) => l.top + l.height >= top0 && l.top <= bot0)
      .map((l) => l.page);
  }, [layout, viewport]);

  // Lazily load metadata (image + word boxes) for visible pages, one ahead.
  useEffect(() => {
    const wanted = new Set(visiblePages);
    const last = visiblePages[visiblePages.length - 1];
    if (last && last + 1 <= numPages) wanted.add(last + 1);
    wanted.forEach((p) => {
      if (pages.has(p) || inflight.current.has(p)) return;
      inflight.current.add(p);
      booksApi
        .getPage(bookId, p)
        .then((data) => setPages((prev) => new Map(prev).set(p, data)))
        .catch(() => setPages((prev) => new Map(prev).set(p, "error")))
        .finally(() => inflight.current.delete(p));
    });
  }, [visiblePages, pages, bookId, numPages]);

  const recomputeFocus = useCallback(
    (scrollTop: number, vh: number) => {
      if (performance.now() < programmaticUntil.current) return;
      const words: PositionedWord[] = [];
      const layouts: PageLayout[] = [];
      for (const p of visiblePages) {
        const pg = pages.get(p);
        const l = layoutByPage.get(p);
        if (!l || !pg || pg === "error") continue;
        layouts.push(l);
        for (const wb of pg.word_boxes) {
          words.push({ word_index: wb.word_index, page: p, bbox: wb.bbox });
        }
      }
      if (words.length === 0) return;
      const w = computeFocusWord({ scrollTop, viewportHeight: vh, pages: layouts, words });
      if (w !== null) engine.onScrollInput(w);
    },
    [visiblePages, pages, layoutByPage, engine],
  );

  const onScroll = useCallback(() => {
    if (rafPending.current) return;
    rafPending.current = true;
    requestAnimationFrame(() => {
      rafPending.current = false;
      const el = scrollRef.current;
      if (!el) return;
      const scrollTop = el.scrollTop;
      const vh = el.clientHeight;
      setViewport({ scrollTop, height: vh });
      recomputeFocus(scrollTop, vh);
    });
  }, [recomputeFocus]);

  // Video-driven auto page-turn (Viewer mode): when the engine flips currentPage
  // and the video owns the playhead, scroll the reader to that page.
  useEffect(() => {
    if (snap.owner !== "video" || snap.currentPage == null) return;
    const l = layoutByPage.get(snap.currentPage);
    const el = scrollRef.current;
    if (!l || !el) return;
    programmaticUntil.current = performance.now() + 450;
    el.scrollTo({ top: Math.max(0, l.top - 12), behavior: reduceMotion ? "auto" : "smooth" });
  }, [snap.currentPage, snap.owner, layoutByPage, reduceMotion]);

  const onImageLoad = useCallback(
    (page: number, img: HTMLImageElement) => {
      if (!img.naturalWidth || !img.naturalHeight) return;
      const aspect = img.naturalHeight / img.naturalWidth;
      setAspects((prev) => {
        const current = prev.get(page);
        if (current !== undefined && Math.abs(current - aspect) < 0.001) return prev;
        return new Map(prev).set(page, aspect);
      });
    },
    [],
  );

  return (
    <div className="relative h-full bg-kinora-ink/40">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="scrollbar-thin h-full overflow-y-auto"
      >
        <div className="relative mx-auto" style={{ height: total }}>
          {visiblePages.map((p) => {
            const l = layoutByPage.get(p);
            if (!l) return null;
            const pg = pages.get(p);
            return (
              <div
                key={p}
                className="absolute left-0 right-0"
                style={{ top: l.top, height: l.height, paddingInline: HORIZONTAL_PADDING }}
              >
                <div
                  className="relative mx-auto h-full overflow-hidden rounded-lg bg-white shadow-xl ring-1 ring-black/40"
                  style={{ width }}
                >
                  {pg && pg !== "error" ? (
                    <>
                      <img
                        src={pg.image_url}
                        alt={`Page ${p}`}
                        className="block h-full w-full select-none object-contain"
                        draggable={false}
                        onLoad={(e) => onImageLoad(p, e.currentTarget)}
                      />
                      <WordHighlightLayer
                        words={pg.word_boxes}
                        activeWordIndex={snap.activeWordIndex}
                        focusWordIndex={snap.focusWord}
                        playedThroughIndex={snap.activeWordIndex}
                        onWordClick={(w) => engine.seek(w)}
                      />
                    </>
                  ) : (
                    <div className="flex h-full w-full items-center justify-center bg-kinora-panel text-sm text-kinora-muted">
                      {pg === "error" ? `Page ${p} unavailable` : `Loading page ${p}…`}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* The reading line — eyes lead the scroll, so it sits at the top third. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-1/3 z-10"
      >
        <div className="mx-6 h-px bg-gradient-to-r from-transparent via-kinora-iris/30 to-transparent" />
      </div>
    </div>
  );
}
