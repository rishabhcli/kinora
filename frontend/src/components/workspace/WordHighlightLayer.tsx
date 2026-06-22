import { memo } from "react";

import type { WordBox } from "../../api/types";

interface WordHighlightLayerProps {
  /** Normalized word boxes for this page (0..1 coordinates). */
  words: WordBox[];
  /** The word currently being narrated (karaoke active highlight). */
  activeWordIndex: number | null;
  /** The scroll focus word `w` — gets a faint marker. */
  focusWordIndex?: number | null;
  /** Words with index <= this are shown as "played" (already spoken). */
  playedThroughIndex?: number | null;
  onWordClick?: (wordIndex: number) => void;
}

function stateFor(
  wordIndex: number,
  active: number | null,
  playedThrough: number | null,
): "active" | "played" | "ahead" {
  if (active !== null && wordIndex === active) return "active";
  if (playedThrough !== null && wordIndex <= playedThrough) return "played";
  return "ahead";
}

/**
 * Absolutely-positioned highlight spans painted over a rasterised page image.
 * Because boxes are normalized, the layer fills its (relative) parent and
 * positions each word as a percentage — so it stays exact at any render width
 * and exactly matches the backend's word boxes (no pdf.js in the browser).
 */
function WordHighlightLayerImpl({
  words,
  activeWordIndex,
  focusWordIndex = null,
  playedThroughIndex = null,
  onWordClick,
}: WordHighlightLayerProps) {
  return (
    <div className="pointer-events-none absolute inset-0" aria-hidden="true">
      {words.map((word) => {
        const [x, y, w, h] = word.bbox;
        const state = stateFor(word.word_index, activeWordIndex, playedThroughIndex);
        const isFocus = focusWordIndex !== null && word.word_index === focusWordIndex;
        const className =
          state === "active"
            ? "karaoke-active"
            : state === "played"
              ? "karaoke-played"
              : isFocus
                ? "ring-1 ring-inset ring-kinora-iris/40 rounded-[3px]"
                : "";
        return (
          <button
            key={word.word_index}
            type="button"
            data-word-index={word.word_index}
            data-state={state}
            title={word.text}
            onClick={onWordClick ? () => onWordClick(word.word_index) : undefined}
            tabIndex={-1}
            className={`pointer-events-auto absolute cursor-pointer transition-colors duration-150 ${className}`}
            style={{
              left: `${x * 100}%`,
              top: `${y * 100}%`,
              width: `${w * 100}%`,
              height: `${h * 100}%`,
            }}
          >
            <span className="sr-only">{word.text}</span>
          </button>
        );
      })}
    </div>
  );
}

export const WordHighlightLayer = memo(WordHighlightLayerImpl);
