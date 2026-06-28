// Whether the GPU compositor should run at all — and the pure decision rule
// behind it, separated from the runtime probe so the policy is unit-testable.
//
// The GPU path is OPTIONAL and ADDITIVE. The no-black-frame guarantee is owned by
// the CSS opacity crossfade (FilmPane), which is always present; the WebGL
// compositor layers a graded/transition pass over it only when (a) the platform
// can compile our WebGL2 program, (b) the user hasn't asked for reduced motion,
// and (c) the renderer isn't already starved (high jank → don't pile a GPU pass
// onto a struggling frame). If any check fails we simply don't mount the canvas
// and the reader keeps the proven CSS film. There is never a moment where the GPU
// path is the *only* thing on screen.

export interface GlCapabilities {
  /** a WebGL2 context can be created */
  webgl2: boolean;
  /** our vertex+fragment program compiles & links */
  programOk: boolean;
  /** max texture size the GPU reports (0 if unknown) */
  maxTextureSize: number;
  /** the GL_RENDERER string, if exposed (often masked) */
  renderer: string | null;
  /** a human note when unavailable (telemetry) */
  note: string;
}

export const NO_GL: GlCapabilities = {
  webgl2: false,
  programOk: false,
  maxTextureSize: 0,
  renderer: null,
  note: "not probed",
};

export interface CompositorDecisionInputs {
  caps: GlCapabilities;
  /** the app-wide reduced-motion preference */
  reducedMotion: boolean;
  /** rolling rAF jank ratio [0,1]; high jank → keep the GPU pass off */
  jankRatio?: number;
  /** explicit user/app opt-out (e.g. a "simple film" setting) */
  forceOff?: boolean;
  /** the pane needs at least this texture size to look right (height px) */
  minTextureSize?: number;
}

export interface CompositorDecision {
  /** mount + drive the WebGL compositor */
  useGpu: boolean;
  /** why (telemetry / the observability panel) */
  reason: string;
}

const JANK_GATE = 0.2; // ≥20% janky frames → don't add a GPU compositing pass

/** Pure policy: should we run the GPU compositor right now? Defaults conservative
 *  — any doubt falls back to the CSS film. */
export function decideCompositor(inputs: CompositorDecisionInputs): CompositorDecision {
  if (inputs.forceOff) return { useGpu: false, reason: "forced-off" };
  if (inputs.reducedMotion) return { useGpu: false, reason: "reduced-motion" };
  if (!inputs.caps.webgl2) return { useGpu: false, reason: "no-webgl2" };
  if (!inputs.caps.programOk) return { useGpu: false, reason: "program-compile-failed" };
  const needSize = inputs.minTextureSize ?? 720;
  if (inputs.caps.maxTextureSize > 0 && inputs.caps.maxTextureSize < needSize) {
    return { useGpu: false, reason: "texture-size-too-small" };
  }
  if ((inputs.jankRatio ?? 0) >= JANK_GATE) return { useGpu: false, reason: "renderer-starved" };
  return { useGpu: true, reason: "ok" };
}

/** Minimal type surface so the probe doesn't depend on lib.dom in tests. */
type Ctx2Like = {
  createShader(type: number): unknown;
  shaderSource(s: unknown, src: string): void;
  compileShader(s: unknown): void;
  getShaderParameter(s: unknown, p: number): unknown;
  deleteShader(s: unknown): void;
  createProgram(): unknown;
  attachShader(p: unknown, s: unknown): void;
  linkProgram(p: unknown): void;
  getProgramParameter(p: unknown, pn: number): unknown;
  deleteProgram(p: unknown): void;
  getParameter(p: number): unknown;
  VERTEX_SHADER: number;
  FRAGMENT_SHADER: number;
  COMPILE_STATUS: number;
  LINK_STATUS: number;
  MAX_TEXTURE_SIZE: number;
};

export interface ProbeOpts {
  /** inject a canvas factory (tests pass a stub; default uses document) */
  createCanvas?: () => { getContext(id: string): unknown } | null;
  vertSrc: string;
  fragSrc: string;
}

/** Runtime probe: try to create a WebGL2 context and compile/link the program.
 *  Never throws — any failure returns a {@link GlCapabilities} with the reason.
 *  The compile/link work is delegated so tests can drive it with a fake gl. */
export function probeGl(opts: ProbeOpts): GlCapabilities {
  const make =
    opts.createCanvas ??
    (() => {
      try {
        if (typeof document === "undefined") return null;
        return document.createElement("canvas");
      } catch {
        return null;
      }
    });
  let canvas: { getContext(id: string): unknown } | null = null;
  try {
    canvas = make();
  } catch {
    return { ...NO_GL, note: "canvas-create-threw" };
  }
  if (!canvas) return { ...NO_GL, note: "no-document" };

  let gl: Ctx2Like | null = null;
  try {
    gl = canvas.getContext("webgl2") as Ctx2Like | null;
  } catch {
    return { ...NO_GL, note: "getContext-threw" };
  }
  if (!gl) return { ...NO_GL, note: "no-webgl2-context" };

  try {
    const vert = compile(gl, gl.VERTEX_SHADER, opts.vertSrc);
    const frag = compile(gl, gl.FRAGMENT_SHADER, opts.fragSrc);
    if (!vert || !frag) {
      return { webgl2: true, programOk: false, maxTextureSize: 0, renderer: null, note: "shader-compile-failed" };
    }
    const program = gl.createProgram();
    gl.attachShader(program, vert);
    gl.attachShader(program, frag);
    gl.linkProgram(program);
    const linked = Boolean(gl.getProgramParameter(program, gl.LINK_STATUS));
    gl.deleteShader(vert);
    gl.deleteShader(frag);
    gl.deleteProgram(program);
    const maxTex = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE)) || 0;
    return {
      webgl2: true,
      programOk: linked,
      maxTextureSize: maxTex,
      renderer: null,
      note: linked ? "ok" : "link-failed",
    };
  } catch {
    return { webgl2: true, programOk: false, maxTextureSize: 0, renderer: null, note: "probe-threw" };
  }
}

function compile(gl: Ctx2Like, type: number, src: string): unknown {
  const s = gl.createShader(type);
  if (!s) return null;
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    gl.deleteShader(s);
    return null;
  }
  return s;
}
