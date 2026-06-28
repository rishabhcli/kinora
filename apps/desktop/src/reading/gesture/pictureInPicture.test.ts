// Picture-in-Picture controller, driven by stub video/document — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { canUsePip, pipState, enterPip, exitPip, togglePip } from "./pictureInPicture.ts";

const video = (opts: Partial<{ req: boolean; disabled: boolean }> = {}) => ({
  requestPictureInPicture: opts.req === false ? undefined : async () => ({}),
  disablePictureInPicture: opts.disabled ?? false,
});

const doc = (opts: Partial<{ enabled: boolean; activeEl: unknown }> = {}) => ({
  pictureInPictureEnabled: opts.enabled ?? true,
  pictureInPictureElement: opts.activeEl,
  exitPictureInPicture: async () => {},
});

test("canUsePip requires the API, the document enabled, and not disabled on the video", () => {
  assert.equal(canUsePip(video(), doc()), true);
  assert.equal(canUsePip(video({ req: false }), doc()), false);
  assert.equal(canUsePip(video({ disabled: true }), doc()), false);
  assert.equal(canUsePip(video(), doc({ enabled: false })), false);
  assert.equal(canUsePip(null, doc()), false);
});

test("pipState reflects unavailable / inactive / active", () => {
  assert.equal(pipState(video({ req: false }), doc()), "unavailable");
  assert.equal(pipState(video(), doc()), "inactive");
  assert.equal(pipState(video(), doc({ activeEl: {} })), "active");
});

test("enterPip resolves true on success, false when unavailable", async () => {
  assert.equal(await enterPip(video(), doc()), true);
  assert.equal(await enterPip(video({ req: false }), doc()), false);
});

test("enterPip swallows a rejection and returns false", async () => {
  const v = { requestPictureInPicture: async () => Promise.reject(new Error("denied")) };
  assert.equal(await enterPip(v, doc()), false);
});

test("exitPip returns false when nothing is active", async () => {
  assert.equal(await exitPip(doc()), false);
  assert.equal(await exitPip(doc({ activeEl: {} })), true);
});

test("togglePip enters from inactive and exits from active", async () => {
  assert.equal(await togglePip(video(), doc()), "active");
  assert.equal(await togglePip(video(), doc({ activeEl: {} })), "inactive");
  assert.equal(await togglePip(video({ req: false }), doc()), "unavailable");
});
