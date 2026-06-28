// The GPU film compositor. Draws two video layers into a <canvas> with a GPU
// crossfade + colour grade + grain (shaders.ts), layered OVER the CSS opacity
// crossfade owned by FilmPane. This is strictly an enhancement: the canvas sits
// on top of the <video> layers and is only mounted when capabilities.ts says the
// platform can run it AND reduced motion is off. If anything goes wrong at runtime
// (context lost, a layer not yet decodable) the compositor reports it and the host
// hides the canvas, revealing the proven CSS film underneath — so there is never a
// black frame that originates here.
//
// Lifecycle: construct with a canvas → `setLayerSource(slot, video)` binds the two
// HTMLVideoElements → `render(opts)` each rAF uploads their current frames and
// draws. `dispose()` frees every GL object. The class owns ONLY GL state; the
// playhead (which video, what currentTime) stays with FilmPane/useScrollFilm.

import { FULLSCREEN_TRIANGLE, FRAG_SRC, VERT_SRC, ATTRIB_POS_LOCATION } from "./shaders";
import { buildProgram, type CompiledProgram } from "./program";
import { NEUTRAL_GRADE, type FilmGrade } from "./grade";

/** A video-frame source the compositor can sample. We only need the bits that
 *  make a frame uploadable + a readiness check, so tests can pass a stub. */
export interface FrameSource {
  /** the element to upload (an HTMLVideoElement at runtime) */
  readonly element: TexImageSource;
  /** is there a decoded frame to upload? (readyState ≥ 2 for a <video>) */
  hasFrame(): boolean;
  /** does the source need a vertical flip on upload? (false for <video>) */
  readonly flipY?: boolean;
}

export interface RenderOptions {
  /** crossfade position: 0 = layer A, 1 = layer B */
  mix: number;
  /** the colour grade to apply (defaults to neutral = passthrough) */
  grade?: FilmGrade;
  /** seconds, animates the grain */
  timeSeconds?: number;
}

export type CompositorStatus = "uninitialised" | "ready" | "context-lost" | "failed" | "disposed";

const A = 0 as const;
const B = 1 as const;
export type LayerSlot = typeof A | typeof B;

export class WebGLCompositor {
  private gl: WebGL2RenderingContext | null = null;
  private prog: CompiledProgram | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private buffer: WebGLBuffer | null = null;
  private texA: WebGLTexture | null = null;
  private texB: WebGLTexture | null = null;
  private sources: [FrameSource | null, FrameSource | null] = [null, null];
  private _status: CompositorStatus = "uninitialised";
  private _error: string | null = null;
  private width = 0;
  private height = 0;
  private readonly canvas: HTMLCanvasElement;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
  }

  get status(): CompositorStatus {
    return this._status;
  }
  get error(): string | null {
    return this._error;
  }
  /** A draw is only meaningful once initialised and not in a lost/failed state. */
  get isOperational(): boolean {
    return this._status === "ready";
  }

  /** Create the context + program + geometry. Returns true on success; on any
   *  failure sets status="failed" and an error string (the host then falls back to
   *  the CSS film). Never throws. */
  init(): boolean {
    if (this._status === "ready") return true;
    let gl: WebGL2RenderingContext | null = null;
    try {
      gl = this.canvas.getContext("webgl2", {
        alpha: false,
        antialias: false,
        depth: false,
        stencil: false,
        premultipliedAlpha: false,
        // The film overwrites the whole frame each draw; preserving is wasteful.
        preserveDrawingBuffer: false,
        powerPreference: "high-performance",
      }) as WebGL2RenderingContext | null;
    } catch (e) {
      return this.fail(`getContext threw: ${String(e)}`);
    }
    if (!gl) return this.fail("no webgl2 context");
    this.gl = gl;

    const built = buildProgram(gl, VERT_SRC, FRAG_SRC);
    if (!built.ok || !built.program) return this.fail(built.error ?? "program build failed");
    this.prog = built.program;

    // Fullscreen-triangle VAO.
    this.vao = gl.createVertexArray();
    this.buffer = gl.createBuffer();
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, FULLSCREEN_TRIANGLE, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(ATTRIB_POS_LOCATION);
    gl.vertexAttribPointer(ATTRIB_POS_LOCATION, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    this.texA = this.createTex(gl);
    this.texB = this.createTex(gl);

    // React to GPU context loss/restore so a transient loss doesn't dead-end.
    this.canvas.addEventListener("webglcontextlost", this.onContextLost);
    this.canvas.addEventListener("webglcontextrestored", this.onContextRestored);

    this._status = "ready";
    this._error = null;
    return true;
  }

  /** Bind (or clear) the video source for a layer slot. */
  setLayerSource(slot: LayerSlot, source: FrameSource | null): void {
    this.sources[slot] = source;
  }

  /** Resize the drawing buffer to match the displayed pane (CSS px × dpr). No-op
   *  if unchanged. */
  resize(cssWidth: number, cssHeight: number, dpr = 1): void {
    const w = Math.max(1, Math.round(cssWidth * dpr));
    const h = Math.max(1, Math.round(cssHeight * dpr));
    if (w === this.width && h === this.height) return;
    this.width = w;
    this.height = h;
    if (this.canvas.width !== w) this.canvas.width = w;
    if (this.canvas.height !== h) this.canvas.height = h;
    this.gl?.viewport(0, 0, w, h);
  }

  /** Draw one composited frame. Safe to call every rAF. Returns false (and does
   *  nothing) when not operational, when layer A has no frame yet (so we never
   *  paint black), so the host can keep the CSS film visible until the GPU path
   *  has something real to show. */
  render(opts: RenderOptions): boolean {
    const gl = this.gl;
    const prog = this.prog;
    if (!gl || !prog || this._status !== "ready") return false;

    const srcA = this.sources[A];
    // The no-black-frame rule, enforced here too: do not draw until layer A has a
    // decoded frame. The host keeps the underlying CSS <video> visible meanwhile.
    if (!srcA || !srcA.hasFrame()) return false;
    const srcB = this.sources[B];
    const hasB = Boolean(srcB && srcB.hasFrame());

    try {
      this.uploadFrame(gl, this.texA!, srcA);
      if (hasB) this.uploadFrame(gl, this.texB!, srcB!);

      gl.useProgram(prog.program);
      gl.bindVertexArray(this.vao);

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.texA);
      if (prog.uniforms.uTexA) gl.uniform1i(prog.uniforms.uTexA, 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, hasB ? this.texB : this.texA);
      if (prog.uniforms.uTexB) gl.uniform1i(prog.uniforms.uTexB, 1);

      const grade = opts.grade ?? NEUTRAL_GRADE;
      const mix = clamp01(opts.mix);
      this.setUniforms(gl, prog, mix, hasB, grade, opts.timeSeconds ?? 0, srcA, srcB);

      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindVertexArray(null);
      return true;
    } catch (e) {
      this._error = `render threw: ${String(e)}`;
      // A render throw isn't fatal — keep status ready so a later frame can retry,
      // but signal failure to the caller for this frame.
      return false;
    }
  }

  /** Free every GL object + listener. Idempotent. */
  dispose(): void {
    const gl = this.gl;
    this.canvas.removeEventListener("webglcontextlost", this.onContextLost);
    this.canvas.removeEventListener("webglcontextrestored", this.onContextRestored);
    if (gl) {
      if (this.texA) gl.deleteTexture(this.texA);
      if (this.texB) gl.deleteTexture(this.texB);
      if (this.buffer) gl.deleteBuffer(this.buffer);
      if (this.vao) gl.deleteVertexArray(this.vao);
      if (this.prog) gl.deleteProgram(this.prog.program);
    }
    this.texA = this.texB = null;
    this.buffer = null;
    this.vao = null;
    this.prog = null;
    this.gl = null;
    this.sources = [null, null];
    this._status = "disposed";
  }

  // --- internals ---------------------------------------------------------

  private setUniforms(
    gl: WebGL2RenderingContext,
    prog: CompiledProgram,
    mix: number,
    hasB: boolean,
    grade: FilmGrade,
    timeSeconds: number,
    srcA: FrameSource,
    srcB: FrameSource | null,
  ): void {
    const u = prog.uniforms;
    if (u.uMix) gl.uniform1f(u.uMix, mix);
    if (u.uHasB) gl.uniform1f(u.uHasB, hasB ? 1 : 0);
    if (u.uFlipA) gl.uniform1f(u.uFlipA, srcA.flipY ? 1 : 0);
    if (u.uFlipB) gl.uniform1f(u.uFlipB, srcB?.flipY ? 1 : 0);
    if (u.uLift) gl.uniform3f(u.uLift, grade.lift[0], grade.lift[1], grade.lift[2]);
    if (u.uGamma) gl.uniform3f(u.uGamma, grade.gamma[0], grade.gamma[1], grade.gamma[2]);
    if (u.uGain) gl.uniform3f(u.uGain, grade.gain[0], grade.gain[1], grade.gain[2]);
    if (u.uSaturation) gl.uniform1f(u.uSaturation, grade.saturation);
    if (u.uVignette) gl.uniform1f(u.uVignette, grade.vignette);
    if (u.uGrain) gl.uniform1f(u.uGrain, grade.grain);
    if (u.uTime) gl.uniform1f(u.uTime, timeSeconds);
  }

  private uploadFrame(gl: WebGL2RenderingContext, tex: WebGLTexture, src: FrameSource): void {
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false); // we flip in the shader instead
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, src.element);
  }

  private createTex(gl: WebGL2RenderingContext): WebGLTexture | null {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    return tex;
  }

  private onContextLost = (e: Event): void => {
    e.preventDefault(); // allow restoration
    this._status = "context-lost";
    this._error = "webgl context lost";
  };

  private onContextRestored = (): void => {
    // Rebuild program + geometry against the restored context.
    this._status = "uninitialised";
    this.prog = null;
    this.vao = null;
    this.buffer = null;
    this.texA = this.texB = null;
    this.init();
  };

  private fail(msg: string): boolean {
    this._status = "failed";
    this._error = msg;
    return false;
  }
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}
