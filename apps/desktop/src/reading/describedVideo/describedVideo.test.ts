// Pure described-video (audio-description) model — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import {
  buildTrack,
  activeCue,
  spokenDurationS,
  decideAnnounce,
  initialAnnouncerState,
} from "./describedVideo.ts";

const cue = (id: string, text: string, wordStart: number, wordEnd: number) => ({ id, text, wordStart, wordEnd });

test("buildTrack sorts, drops empties, and makes cues contiguous", () => {
  const t = buildTrack([
    cue("c", "third shot", 200, 999),
    cue("a", "first shot", 0, 50),
    cue("blank", "   ", 60, 100),
    cue("b", "second shot", 100, 150),
  ]);
  assert.deepEqual(
    t.cues.map((c) => c.id),
    ["a", "b", "c"],
  );
  // a runs up to b's start (100), b up to c's start (200).
  assert.equal(t.cues[0].wordEnd, 100);
  assert.equal(t.cues[1].wordEnd, 200);
});

test("activeCue picks the greatest cue starting at/before the focus word", () => {
  const t = buildTrack([cue("a", "A", 0, 100), cue("b", "B", 100, 200)]);
  assert.equal(activeCue(t, -1), null);
  assert.equal(activeCue(t, 0)?.id, "a");
  assert.equal(activeCue(t, 99)?.id, "a");
  assert.equal(activeCue(t, 100)?.id, "b");
  assert.equal(activeCue(t, 5000)?.id, "b");
});

test("spokenDurationS scales with words at the wpm rate", () => {
  // 170 words at 170 wpm = 60s.
  const text = Array.from({ length: 170 }, () => "w").join(" ");
  assert.ok(Math.abs(spokenDurationS(text, 170) - 60) < 1e-6);
  assert.equal(spokenDurationS("", 170), 0);
});

test("decideAnnounce announces only when the active cue changes", () => {
  const t = buildTrack([cue("a", "A", 0, 100), cue("b", "B", 100, 200)]);
  let state = initialAnnouncerState;

  let d = decideAnnounce(t, 10, state); // enter cue a
  assert.equal(d.cue?.id, "a");
  state = d.next;

  d = decideAnnounce(t, 50, state); // still in a → no re-announce
  assert.equal(d.cue, null);
  state = d.next;

  d = decideAnnounce(t, 120, state); // crossed into b → announce
  assert.equal(d.cue?.id, "b");
  state = d.next;

  d = decideAnnounce(t, 130, state); // still b → silent
  assert.equal(d.cue, null);
});
