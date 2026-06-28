// Pure instrumented-fetch byte/time accounting — node:test. (Imports only a TYPE
// from bandwidth.ts, which strip-types erases, so it self-resolves.)
import test from "node:test";
import assert from "node:assert/strict";
import { makeInstrumentedFetch } from "./instrumentedFetch.ts";

// A fake Response whose clone() streams a fixed number of bytes.
function fakeResponse(bytes: number, contentLength?: number) {
  const chunk = new Uint8Array(bytes);
  const makeBody = () => ({
    getReader() {
      let sent = false;
      return {
        read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: chunk })),
      };
    },
  });
  const res: Record<string, unknown> = {
    ok: true,
    status: 200,
    headers: { get: (h: string) => (h.toLowerCase() === "content-length" && contentLength != null ? String(contentLength) : null) },
    body: makeBody(),
    clone() {
      return { body: makeBody(), headers: this.headers };
    },
  };
  return res as unknown as Response;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

test("reports a {bytes, durationMs} sample from a streamed body", async () => {
  let t = 0;
  const samples: { bytes: number; durationMs: number }[] = [];
  const base = (async () => fakeResponse(12345)) as unknown as typeof fetch;
  const f = makeInstrumentedFetch({
    fetchImpl: base,
    now: () => (t += 100), // start=100, end=200 → 100ms
    onSample: (s) => samples.push(s),
  });
  const res = await f("https://oss/a.mp4");
  await flush();
  assert.equal(res.status, 200);
  assert.equal(samples.length, 1);
  assert.equal(samples[0].bytes, 12345);
  assert.equal(samples[0].durationMs, 100);
});

test("falls back to content-length when there's no streamable body", async () => {
  const samples: { bytes: number; durationMs: number }[] = [];
  let t = 0;
  const noBody = { ok: true, status: 200, headers: { get: () => "5000" }, body: null } as unknown as Response;
  const base = (async () => noBody) as unknown as typeof fetch;
  const f = makeInstrumentedFetch({ fetchImpl: base, now: () => (t += 50), onSample: (s) => samples.push(s) });
  await f("https://oss/b.mp4");
  await flush();
  assert.equal(samples.length, 1);
  assert.equal(samples[0].bytes, 5000);
});

test("passes the original Response through untouched (caller can still read it)", async () => {
  const res = fakeResponse(100);
  const base = (async () => res) as unknown as typeof fetch;
  const f = makeInstrumentedFetch({ fetchImpl: base, onSample: () => {} });
  const got = await f("https://oss/c.mp4");
  assert.equal(got, res); // same object — clone() is what we measured
});

test("a failing transport rejects and emits no sample", async () => {
  const samples: unknown[] = [];
  const base = (async () => {
    throw new Error("network down");
  }) as unknown as typeof fetch;
  const f = makeInstrumentedFetch({ fetchImpl: base, onSample: () => samples.push(1) });
  await assert.rejects(() => f("https://oss/d.mp4"));
  assert.equal(samples.length, 0);
});
