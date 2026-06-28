import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, act } from "@testing-library/react";
import { GpuFilmOverlay } from "./GpuFilmOverlay";
import type { GlCapabilities } from "./capabilities";

const OK_CAPS: GlCapabilities = { webgl2: true, programOk: true, maxTextureSize: 4096, renderer: null, note: "ok" };
const NO_CAPS: GlCapabilities = { webgl2: false, programOk: false, maxTextureSize: 0, renderer: null, note: "none" };

// A recording WebGL2 mock installed on the canvas prototype so the real
// WebGLCompositor.init() succeeds inside jsdom.
function installGl() {
  let linkOk = true;
  const gl: Record<string, unknown> = {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4, ARRAY_BUFFER: 5,
    STATIC_DRAW: 6, FLOAT: 7, TEXTURE_2D: 8, TEXTURE0: 33984, TEXTURE1: 33985, RGBA: 9,
    UNSIGNED_BYTE: 10, TRIANGLES: 11, CLAMP_TO_EDGE: 12, TEXTURE_WRAP_S: 13, TEXTURE_WRAP_T: 14,
    TEXTURE_MIN_FILTER: 15, TEXTURE_MAG_FILTER: 16, LINEAR: 17, UNPACK_FLIP_Y_WEBGL: 18,
    createShader: () => ({}), shaderSource: () => {}, compileShader: () => {},
    getShaderParameter: () => true, getShaderInfoLog: () => "", deleteShader: () => {},
    createProgram: () => ({}), attachShader: () => {}, bindAttribLocation: () => {}, linkProgram: () => {},
    getProgramParameter: () => linkOk, getProgramInfoLog: () => "", deleteProgram: () => {},
    getUniformLocation: (_p: unknown, name: string) => ({ name }), useProgram: () => {},
    createVertexArray: () => ({}), bindVertexArray: () => {}, createBuffer: () => ({}), bindBuffer: () => {},
    bufferData: () => {}, enableVertexAttribArray: () => {}, vertexAttribPointer: () => {},
    deleteVertexArray: () => {}, deleteBuffer: () => {}, createTexture: () => ({}), bindTexture: () => {},
    texParameteri: () => {}, pixelStorei: () => {}, activeTexture: () => {}, texImage2D: () => {},
    deleteTexture: () => {}, uniform1i: () => {}, uniform1f: () => {}, uniform3f: () => {},
    viewport: () => {}, drawArrays: () => {},
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    ((id: string) => (id === "webgl2" ? gl : null)) as typeof HTMLCanvasElement.prototype.getContext,
  );
  return { setLink: (v: boolean) => (linkOk = v) };
}

// Drive a controllable rAF.
function installRaf() {
  let cb: FrameRequestCallback | null = null;
  let now = 0;
  vi.stubGlobal("requestAnimationFrame", (fn: FrameRequestCallback) => {
    cb = fn;
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {
    cb = null;
  });
  return {
    tick(dt = 16) {
      now += dt;
      const fn = cb;
      cb = null;
      act(() => fn?.(now));
    },
  };
}

function fakeVideo(ready: boolean): HTMLVideoElement {
  return { readyState: ready ? 2 : 0, videoWidth: ready ? 720 : 0 } as unknown as HTMLVideoElement;
}

beforeEach(() => {
  vi.spyOn(performance, "now").mockReturnValue(0);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("GpuFilmOverlay", () => {
  it("renders no canvas when capabilities say no", () => {
    const { queryByTestId } = render(
      <GpuFilmOverlay
        capsOverride={NO_CAPS}
        reducedMotion={false}
        getLayers={() => [null, null]}
        getMix={() => 0}
      />,
    );
    expect(queryByTestId("gpu-film-overlay")).toBeNull();
  });

  it("renders no canvas under reduced motion even with capable hardware", () => {
    const { queryByTestId } = render(
      <GpuFilmOverlay
        capsOverride={OK_CAPS}
        reducedMotion
        getLayers={() => [null, null]}
        getMix={() => 0}
      />,
    );
    expect(queryByTestId("gpu-film-overlay")).toBeNull();
  });

  it("mounts the canvas hidden, and reveals it only after drawing a real frame", () => {
    installGl();
    const raf = installRaf();
    const layerReady = fakeVideo(true);
    const { getByTestId } = render(
      <GpuFilmOverlay
        capsOverride={OK_CAPS}
        reducedMotion={false}
        getLayers={() => [layerReady, null]}
        getMix={() => 0.5}
      />,
    );
    const canvas = getByTestId("gpu-film-overlay") as HTMLCanvasElement;
    // Mounted but hidden before the first draw.
    expect(canvas.style.opacity).toBe("0");
    // One loop iteration: layer A is decodable → it draws → reveal.
    raf.tick();
    expect(canvas.style.opacity).toBe("1");
  });

  it("stays hidden while layer A has no decoded frame (no black frame)", () => {
    installGl();
    const raf = installRaf();
    const { getByTestId } = render(
      <GpuFilmOverlay
        capsOverride={OK_CAPS}
        reducedMotion={false}
        getLayers={() => [fakeVideo(false), null]}
        getMix={() => 0}
      />,
    );
    raf.tick();
    expect((getByTestId("gpu-film-overlay") as HTMLCanvasElement).style.opacity).toBe("0");
  });
});
