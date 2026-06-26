// Dev-only harness to drive the Scroll Film Engine in a real browser (Playwright).
// Not part of the production build (index.html never references it).
//   ?mode=fallback   single bundled film, live=false (WS3, purest scrub)
//   ?mode=live       4 shots → 4 bundled clips, live=true (WS1/WS2 handoff)
//   ?reduce=1        force reduced motion (instant cuts)
import ReactDOM from "react-dom/client";
import { ScrollFilmEngine } from "../ScrollFilmEngine";
import type { ShotResponse } from "../../lib/api";
import type { ReadingPrefs } from "../../lib/readingPrefs";
import "../../index.css";

const q = new URLSearchParams(location.search);
const mode = q.get("mode") ?? "fallback";
const reduce = q.get("reduce") === "1";
const live = mode === "live";

const book = {
  id: "demo-book",
  title: "The Scroll Film",
  author: "Agent 02",
  progress: 0,
  coverColor: "#1e3a5f",
  coverGradient: "linear-gradient(135deg, #1e3a5f 0%, #0d1f33 100%)",
  coverImage: "",
  textColor: "#e8eef5",
  spineColor: "#0a1622",
};

// Enough paragraphs that the column actually scrolls.
const pages = Array.from({ length: 24 }, (_, i) => ({
  n: i + 1,
  text: `Paragraph ${i + 1}. ` + "Words assemble into a scene a few seconds ahead of the reader, and the film scrubs to follow the eye. ".repeat(3),
}));

// 4 shots, ~250 words each → 4 bundled clips. Word ranges drive the sync map.
const shots: ShotResponse[] = live
  ? [0, 1, 2, 3].map((i) => ({
      shot_id: `shot-${i}`,
      status: "ready",
      duration_s: 5,
      clip_url: `/generated/film-0${i + 1}.mp4`,
      source_span: { word_range: [i * 250, (i + 1) * 250] as [number, number] },
    }))
  : [];
const clips: Record<string, string> = live
  ? Object.fromEntries(shots.map((s) => [s.shot_id, s.clip_url as string]))
  : {};

const prefs: ReadingPrefs = {
  theme: "dark",
  autoNight: false,
  fontScale: 1,
  leading: 1.8,
  measure: 64,
  spacing: "normal",
  // Fields added by A6's ReadingPrefs contract (a11y/readingPrefs) — Captain filled
  // with DEFAULT_READING_PREFS values to keep the demo typechecking. (A12 integration)
  fontFamily: "sans",
  brightness: 1,
  readingMode: "scroll",
  ttsRate: 1,
  ttsVoiceURI: null,
};

// Live state for the Playwright harness (codec-independent).
const w = window as unknown as {
  __kinora: {
    fraction: number;
    focusWord: number;
    read(): Record<string, unknown>;
  };
};
w.__kinora = {
  fraction: 0,
  focusWord: 0,
  read() {
    const scroller = document.querySelector<HTMLElement>('[data-testid="reading-scroll"]');
    const videos = Array.from(document.querySelectorAll("video"));
    const active = videos[videos.length - 1];
    const scrub = document.querySelector<HTMLElement>('[data-testid="scrub-indicator"]');
    return {
      fraction: w.__kinora.fraction,
      focusWord: w.__kinora.focusWord,
      scrollTop: scroller?.scrollTop ?? -1,
      scrollRange: scroller ? scroller.scrollHeight - scroller.clientHeight : -1,
      videoCount: videos.length,
      activeSrc: active?.currentSrc ?? active?.src ?? "",
      activeTime: active?.currentTime ?? -1,
      activeDuration: Number.isFinite(active?.duration) ? active!.duration : -1,
      activePaused: active?.paused ?? null,
      scrubOpacity: scrub ? Number(getComputedStyle(scrub).opacity) : -1,
    };
  },
};

ReactDOM.createRoot(document.getElementById("root")!).render(
  <div style={{ position: "fixed", inset: 0, display: "flex", flexDirection: "column" }} className="kinora-bg">
    <ScrollFilmEngine
      book={book}
      pages={pages}
      shots={shots}
      clips={clips}
      live={live}
      reducedMotion={reduce}
      prefs={prefs}
      bufferAhead={live ? 8 : null}
      onProgress={(fraction, focusWord) => {
        w.__kinora.fraction = fraction;
        w.__kinora.focusWord = focusWord;
      }}
    />
  </div>,
);
