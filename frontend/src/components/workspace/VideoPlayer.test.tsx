import { cleanup, render } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VideoPlayer } from "./VideoPlayer";

afterEach(cleanup);

const noop = () => undefined;

function renderPlayer(props: Partial<Parameters<typeof VideoPlayer>[0]> = {}) {
  const videoRef = createRef<HTMLVideoElement>();
  const utils = render(
    <VideoPlayer
      videoRef={videoRef}
      src={null}
      preloadSrc={null}
      bridging={false}
      bridgeKeyframeUrl={null}
      seekNonce={0}
      seekToS={0}
      playing={false}
      onTogglePlay={noop}
      onTime={noop}
      onEnded={noop}
      {...props}
    />,
  );
  const videos = () => Array.from(utils.container.querySelectorAll("video"));
  return { ...utils, videoRef, videos };
}

describe("VideoPlayer — true two-element hot-swap (kinora.md §5.2 / §5.6)", () => {
  it("warms the next clip in a hidden buffer element while the current one is visible", () => {
    const { videos, videoRef } = renderPlayer({ src: "clipA.mp4", preloadSrc: "clipB.mp4" });

    expect(videos()).toHaveLength(2);
    const visible = videos().filter((v) => v.classList.contains("object-contain"));
    // Exactly one element is visible (the e2e `video.object-contain` locator
    // must resolve to a single node).
    expect(visible).toHaveLength(1);
    expect(visible[0].getAttribute("src")).toBe("clipA.mp4");
    // The other element is buffering the next clip, hidden.
    const buffer = videos().find((v) => !v.classList.contains("object-contain"));
    expect(buffer?.getAttribute("src")).toBe("clipB.mp4");
    // The parent ref points at the visible element.
    expect(videoRef.current).toBe(visible[0]);
  });

  it("promotes the already-buffered element on swap WITHOUT repointing its src (frame-clean)", () => {
    const { videos, videoRef, rerender } = renderPlayer({
      src: "clipA.mp4",
      preloadSrc: "clipB.mp4",
    });

    // The element that is buffering clipB before the swap…
    const bufferedEl = videos().find((v) => v.getAttribute("src") === "clipB.mp4")!;
    const loadSpy = vi.spyOn(bufferedEl, "load");

    // Engine advances: the visible clip becomes clipB (what the buffer already
    // has) and clipC becomes the new preload.
    rerender(
      <VideoPlayer
        videoRef={videoRef}
        src="clipB.mp4"
        preloadSrc="clipC.mp4"
        bridging={false}
        bridgeKeyframeUrl={null}
        seekNonce={0}
        seekToS={0}
        playing={false}
        onTogglePlay={noop}
        onTime={noop}
        onEnded={noop}
      />,
    );

    // …is now the SAME node, now visible, with its src untouched (no reload of
    // the visible path — that is what makes the swap frame-clean).
    expect(bufferedEl.classList.contains("object-contain")).toBe(true);
    expect(bufferedEl.getAttribute("src")).toBe("clipB.mp4");
    expect(loadSpy).not.toHaveBeenCalled();
    expect(videoRef.current).toBe(bufferedEl);

    // Still exactly one visible element, and the demoted element now warms clipC.
    const visible = videos().filter((v) => v.classList.contains("object-contain"));
    expect(visible).toHaveLength(1);
    const demoted = videos().find((v) => v !== bufferedEl)!;
    expect(demoted.getAttribute("src")).toBe("clipC.mp4");
  });

  it("falls back to loading a non-buffered src into the visible element in place", () => {
    const { videos, videoRef, rerender } = renderPlayer({ src: "clipA.mp4", preloadSrc: null });
    const visibleBefore = videos().find((v) => v.classList.contains("object-contain"))!;

    rerender(
      <VideoPlayer
        videoRef={videoRef}
        src="clipX.mp4" // never preloaded → cold seek / regen
        preloadSrc={null}
        bridging={false}
        bridgeKeyframeUrl={null}
        seekNonce={1}
        seekToS={0}
        playing={false}
        onTogglePlay={noop}
        onTime={noop}
        onEnded={noop}
      />,
    );

    // Same visible element, repointed to the new src (an acceptable reload).
    const visibleAfter = videos().find((v) => v.classList.contains("object-contain"))!;
    expect(visibleAfter).toBe(visibleBefore);
    expect(visibleAfter.getAttribute("src")).toBe("clipX.mp4");
    expect(videoRef.current).toBe(visibleAfter);
  });
});
