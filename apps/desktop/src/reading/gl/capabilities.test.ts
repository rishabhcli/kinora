// Pure compositor-decision policy + probe (driven by a fake gl) — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { decideCompositor, probeGl, NO_GL, type GlCapabilities } from "./capabilities.ts";

const OK_CAPS: GlCapabilities = {
  webgl2: true,
  programOk: true,
  maxTextureSize: 4096,
  renderer: null,
  note: "ok",
};

test("decideCompositor runs the GPU path when everything is healthy", () => {
  const d = decideCompositor({ caps: OK_CAPS, reducedMotion: false, jankRatio: 0.02 });
  assert.equal(d.useGpu, true);
  assert.equal(d.reason, "ok");
});

test("reduced motion, force-off, and missing WebGL2 each disable the GPU path", () => {
  assert.equal(decideCompositor({ caps: OK_CAPS, reducedMotion: true }).useGpu, false);
  assert.equal(decideCompositor({ caps: OK_CAPS, reducedMotion: false, forceOff: true }).reason, "forced-off");
  assert.equal(decideCompositor({ caps: NO_GL, reducedMotion: false }).reason, "no-webgl2");
});

test("a failed program link disables the GPU path", () => {
  const d = decideCompositor({ caps: { ...OK_CAPS, programOk: false }, reducedMotion: false });
  assert.equal(d.useGpu, false);
  assert.equal(d.reason, "program-compile-failed");
});

test("a renderer that's already janking keeps the GPU pass off", () => {
  const d = decideCompositor({ caps: OK_CAPS, reducedMotion: false, jankRatio: 0.5 });
  assert.equal(d.useGpu, false);
  assert.equal(d.reason, "renderer-starved");
});

test("too-small max texture size disables the GPU path", () => {
  const d = decideCompositor({ caps: { ...OK_CAPS, maxTextureSize: 256 }, reducedMotion: false, minTextureSize: 720 });
  assert.equal(d.useGpu, false);
  assert.equal(d.reason, "texture-size-too-small");
});

// --- probe ---------------------------------------------------------------

function fakeGl(opts: { compileOk: boolean; linkOk: boolean; maxTex: number }) {
  return {
    VERTEX_SHADER: 1,
    FRAGMENT_SHADER: 2,
    COMPILE_STATUS: 10,
    LINK_STATUS: 11,
    MAX_TEXTURE_SIZE: 12,
    createShader: () => ({}),
    shaderSource: () => {},
    compileShader: () => {},
    getShaderParameter: () => opts.compileOk,
    deleteShader: () => {},
    createProgram: () => ({}),
    attachShader: () => {},
    linkProgram: () => {},
    getProgramParameter: () => opts.linkOk,
    deleteProgram: () => {},
    getParameter: (p: number) => (p === 12 ? opts.maxTex : 0),
  };
}

test("probeGl reports no-document when no canvas factory is available", () => {
  const caps = probeGl({ createCanvas: () => null, vertSrc: "v", fragSrc: "f" });
  assert.equal(caps.webgl2, false);
  assert.equal(caps.note, "no-document");
});

test("probeGl reports a healthy program when the fake gl compiles + links", () => {
  const gl = fakeGl({ compileOk: true, linkOk: true, maxTex: 8192 });
  const caps = probeGl({
    createCanvas: () => ({ getContext: () => gl }),
    vertSrc: "v",
    fragSrc: "f",
  });
  assert.equal(caps.webgl2, true);
  assert.equal(caps.programOk, true);
  assert.equal(caps.maxTextureSize, 8192);
});

test("probeGl reports a link failure cleanly", () => {
  const gl = fakeGl({ compileOk: true, linkOk: false, maxTex: 8192 });
  const caps = probeGl({
    createCanvas: () => ({ getContext: () => gl }),
    vertSrc: "v",
    fragSrc: "f",
  });
  assert.equal(caps.webgl2, true);
  assert.equal(caps.programOk, false);
  assert.equal(caps.note, "link-failed");
});

test("probeGl handles a context-less canvas (no webgl2)", () => {
  const caps = probeGl({
    createCanvas: () => ({ getContext: () => null }),
    vertSrc: "v",
    fragSrc: "f",
  });
  assert.equal(caps.webgl2, false);
  assert.equal(caps.note, "no-webgl2-context");
});
