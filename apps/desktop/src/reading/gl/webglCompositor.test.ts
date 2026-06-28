// The compositor orchestration, driven by a mock WebGL2 context. Runs under
// vitest (not the node:test runner) because the source under test imports sibling
// modules with extensionless specifiers — vitest resolves those the way the app
// build does. We don't need a real GPU: we assert the *sequence* of GL calls
// (program build, texture-upload gating, uniform writes, the no-black-frame guard,
// dispose cleanup) against a recording fake. The grade math is covered in
// grade.test.ts; the real shaders are exercised on a GPU by capabilities.probeGl.
import { describe, expect, it } from "vitest";
import { WebGLCompositor, type FrameSource } from "./webglCompositor";

function makeGlMock() {
  const log: string[] = [];
  let compileOk = true;
  let linkOk = true;
  const gl: Record<string, unknown> = {
    VERTEX_SHADER: 1,
    FRAGMENT_SHADER: 2,
    COMPILE_STATUS: 3,
    LINK_STATUS: 4,
    ARRAY_BUFFER: 5,
    STATIC_DRAW: 6,
    FLOAT: 7,
    TEXTURE_2D: 8,
    TEXTURE0: 33984,
    TEXTURE1: 33985,
    RGBA: 9,
    UNSIGNED_BYTE: 10,
    TRIANGLES: 11,
    CLAMP_TO_EDGE: 12,
    TEXTURE_WRAP_S: 13,
    TEXTURE_WRAP_T: 14,
    TEXTURE_MIN_FILTER: 15,
    TEXTURE_MAG_FILTER: 16,
    LINEAR: 17,
    UNPACK_FLIP_Y_WEBGL: 18,
    createShader: () => ({}),
    shaderSource: () => {},
    compileShader: () => {},
    getShaderParameter: () => compileOk,
    getShaderInfoLog: () => "shader-log",
    deleteShader: () => {},
    createProgram: () => ({}),
    attachShader: () => {},
    bindAttribLocation: () => {},
    linkProgram: () => {},
    getProgramParameter: () => linkOk,
    getProgramInfoLog: () => "program-log",
    deleteProgram: () => log.push("deleteProgram"),
    getUniformLocation: (_p: unknown, name: string) => ({ name }),
    useProgram: () => log.push("useProgram"),
    createVertexArray: () => ({}),
    bindVertexArray: () => {},
    createBuffer: () => ({}),
    bindBuffer: () => {},
    bufferData: () => {},
    enableVertexAttribArray: () => {},
    vertexAttribPointer: () => {},
    deleteVertexArray: () => log.push("deleteVertexArray"),
    deleteBuffer: () => log.push("deleteBuffer"),
    createTexture: () => ({}),
    bindTexture: () => {},
    texParameteri: () => {},
    pixelStorei: () => {},
    activeTexture: () => {},
    texImage2D: () => log.push("texImage2D"),
    deleteTexture: () => log.push("deleteTexture"),
    uniform1i: () => {},
    uniform1f: (loc: { name: string }, v: number) => log.push(`u1f:${loc?.name}=${v}`),
    uniform3f: (loc: { name: string }) => log.push(`u3f:${loc?.name}`),
    viewport: () => {},
    drawArrays: () => log.push("drawArrays"),
  };
  return { gl, log, setCompile: (v: boolean) => (compileOk = v), setLink: (v: boolean) => (linkOk = v) };
}

function makeCanvas(gl: unknown) {
  const listeners: Record<string, ((e: unknown) => void)[]> = {};
  return {
    width: 0,
    height: 0,
    getContext: () => gl,
    addEventListener: (t: string, fn: (e: unknown) => void) => {
      (listeners[t] ||= []).push(fn);
    },
    removeEventListener: () => {},
    _fire: (t: string, e: unknown) => (listeners[t] || []).forEach((fn) => fn(e)),
  };
}

function frameSource(hasFrame: boolean): FrameSource {
  return { element: {} as TexImageSource, hasFrame: () => hasFrame };
}

describe("WebGLCompositor", () => {
  it("init builds the program and reports ready", () => {
    const { gl } = makeGlMock();
    const c = new WebGLCompositor(makeCanvas(gl) as unknown as HTMLCanvasElement);
    expect(c.init()).toBe(true);
    expect(c.status).toBe("ready");
    expect(c.isOperational).toBe(true);
  });

  it("init fails cleanly when the program won't link", () => {
    const m = makeGlMock();
    m.setLink(false);
    const c = new WebGLCompositor(makeCanvas(m.gl) as unknown as HTMLCanvasElement);
    expect(c.init()).toBe(false);
    expect(c.status).toBe("failed");
    expect(c.error ?? "").toMatch(/link/);
  });

  it("init fails when there is no webgl2 context", () => {
    const c = new WebGLCompositor(makeCanvas(null) as unknown as HTMLCanvasElement);
    expect(c.init()).toBe(false);
    expect(c.status).toBe("failed");
  });

  it("render is a no-op until layer A has a decoded frame (never black)", () => {
    const { gl, log } = makeGlMock();
    const c = new WebGLCompositor(makeCanvas(gl) as unknown as HTMLCanvasElement);
    c.init();
    expect(c.render({ mix: 0 })).toBe(false);
    c.setLayerSource(0, frameSource(false));
    expect(c.render({ mix: 0 })).toBe(false);
    expect(log.includes("drawArrays")).toBe(false);
  });

  it("uploads A and draws once ready (B absent → passthrough, uHasB=0)", () => {
    const { gl, log } = makeGlMock();
    const c = new WebGLCompositor(makeCanvas(gl) as unknown as HTMLCanvasElement);
    c.init();
    c.setLayerSource(0, frameSource(true));
    expect(c.render({ mix: 0.5 })).toBe(true);
    expect(log.includes("drawArrays")).toBe(true);
    expect(log.filter((l) => l === "texImage2D").length).toBe(1);
    expect(log.includes("u1f:uHasB=0")).toBe(true);
  });

  it("uploads both layers and sets uHasB=1 when B is ready", () => {
    const { gl, log } = makeGlMock();
    const c = new WebGLCompositor(makeCanvas(gl) as unknown as HTMLCanvasElement);
    c.init();
    c.setLayerSource(0, frameSource(true));
    c.setLayerSource(1, frameSource(true));
    expect(c.render({ mix: 0.5 })).toBe(true);
    expect(log.filter((l) => l === "texImage2D").length).toBe(2);
    expect(log.includes("u1f:uHasB=1")).toBe(true);
    expect(log.includes("u1f:uMix=0.5")).toBe(true);
  });

  it("clamps the mix uniform to [0,1]", () => {
    const { gl, log } = makeGlMock();
    const c = new WebGLCompositor(makeCanvas(gl) as unknown as HTMLCanvasElement);
    c.init();
    c.setLayerSource(0, frameSource(true));
    c.render({ mix: 5 });
    expect(log.includes("u1f:uMix=1")).toBe(true);
  });

  it("a lost context flips status away from ready and stops drawing", () => {
    const { gl } = makeGlMock();
    const canvas = makeCanvas(gl);
    const c = new WebGLCompositor(canvas as unknown as HTMLCanvasElement);
    c.init();
    c.setLayerSource(0, frameSource(true));
    canvas._fire("webglcontextlost", { preventDefault() {} });
    expect(c.status).toBe("context-lost");
    expect(c.render({ mix: 0 })).toBe(false);
  });

  it("dispose frees GL objects and is idempotent", () => {
    const { gl, log } = makeGlMock();
    const c = new WebGLCompositor(makeCanvas(gl) as unknown as HTMLCanvasElement);
    c.init();
    c.dispose();
    expect(c.status).toBe("disposed");
    expect(log.includes("deleteProgram")).toBe(true);
    expect(log.includes("deleteTexture")).toBe(true);
    expect(log.includes("deleteVertexArray")).toBe(true);
    expect(() => c.dispose()).not.toThrow();
  });
});
