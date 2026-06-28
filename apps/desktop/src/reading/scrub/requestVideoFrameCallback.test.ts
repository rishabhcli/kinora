// rVFC wrapper with rAF fallback — node:test (stubbed video + clocks).
import test from "node:test";
import assert from "node:assert/strict";
import { watchPresentedFrames, hasFrameCallback, type FrameCallbackVideo } from "./requestVideoFrameCallback.ts";

test("uses the precise rVFC path when available and reports metadata", () => {
  let next: ((now: number, meta: { mediaTime: number; presentedFrames: number }) => void) | null = null;
  let cancelled = false;
  const video = {
    currentTime: 0,
    requestVideoFrameCallback: (cb: (now: number, meta: { mediaTime: number; presentedFrames: number }) => void) => {
      next = cb;
      return 1;
    },
    cancelVideoFrameCallback: () => {
      cancelled = true;
    },
  } as unknown as FrameCallbackVideo;

  const frames: { mediaTime: number; precise: boolean }[] = [];
  const stop = watchPresentedFrames(video, (f) => frames.push({ mediaTime: f.mediaTime, precise: f.precise }));
  assert.equal(hasFrameCallback(video), true);
  // Simulate one presented frame.
  next!(123, { mediaTime: 2.5, presentedFrames: 1 });
  assert.equal(frames.length, 1);
  assert.equal(frames[0].mediaTime, 2.5);
  assert.equal(frames[0].precise, true);
  stop();
  assert.equal(cancelled, true);
});

test("falls back to rAF, synthesising mediaTime from currentTime", () => {
  let rafCb: ((t: number) => void) | null = null;
  const video = { currentTime: 4.2 } as FrameCallbackVideo; // no rVFC
  const frames: { mediaTime: number; precise: boolean }[] = [];
  const stop = watchPresentedFrames(
    video,
    (f) => frames.push({ mediaTime: f.mediaTime, precise: f.precise }),
    {
      raf: (cb) => {
        rafCb = cb;
        return 7;
      },
      caf: () => {},
      now: () => 999,
    },
  );
  assert.equal(hasFrameCallback(video), false);
  rafCb!(0); // fire one frame
  assert.equal(frames.length, 1);
  assert.equal(frames[0].mediaTime, 4.2);
  assert.equal(frames[0].precise, false);
  stop();
});

test("stopping prevents further callbacks", () => {
  let rafCb: ((t: number) => void) | null = null;
  const video = { currentTime: 0 } as FrameCallbackVideo;
  let count = 0;
  const stop = watchPresentedFrames(video, () => count++, {
    raf: (cb) => {
      rafCb = cb;
      return 1;
    },
    caf: () => {},
    now: () => 0,
  });
  rafCb!(0);
  assert.equal(count, 1);
  stop();
  rafCb!(0); // after stop → ignored
  assert.equal(count, 1);
});
