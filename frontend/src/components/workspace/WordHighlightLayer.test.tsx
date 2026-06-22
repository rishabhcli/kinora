import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WordBox } from "../../api/types";
import { WordHighlightLayer } from "./WordHighlightLayer";

const words: WordBox[] = [
  { word_index: 4501, text: "She", bbox: [0.1, 0.2, 0.05, 0.02] },
  { word_index: 4502, text: "stood", bbox: [0.16, 0.2, 0.06, 0.02] },
  { word_index: 4503, text: "still", bbox: [0.23, 0.2, 0.05, 0.02] },
];

afterEach(cleanup);

describe("WordHighlightLayer", () => {
  it("marks the active word and positions spans from normalized bboxes", () => {
    const { container } = render(
      <WordHighlightLayer words={words} activeWordIndex={4502} playedThroughIndex={4502} />,
    );
    const active = container.querySelector('[data-word-index="4502"]') as HTMLElement;
    expect(active.getAttribute("data-state")).toBe("active");
    expect(active.className).toContain("karaoke-active");
    expect(active.style.left).toBe("16%");
    expect(active.style.width).toBe("6%");

    const played = container.querySelector('[data-word-index="4501"]') as HTMLElement;
    expect(played.getAttribute("data-state")).toBe("played");

    const ahead = container.querySelector('[data-word-index="4503"]') as HTMLElement;
    expect(ahead.getAttribute("data-state")).toBe("ahead");
  });

  it("invokes onWordClick with the word index when scrubbing", () => {
    const onWordClick = vi.fn();
    const { container } = render(
      <WordHighlightLayer words={words} activeWordIndex={null} onWordClick={onWordClick} />,
    );
    fireEvent.click(container.querySelector('[data-word-index="4503"]') as HTMLElement);
    expect(onWordClick).toHaveBeenCalledWith(4503);
  });
});
