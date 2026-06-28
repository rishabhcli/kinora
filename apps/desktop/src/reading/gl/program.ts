// Compile + link a WebGL2 program and resolve its uniform/attribute locations.
// Separated from the compositor so the (small amount of) imperative GL plumbing
// has one home and the compositor reads as orchestration. Errors are returned, not
// thrown: a compile/link failure must degrade to the CSS film, never crash the
// reading room.

import { UNIFORM_NAMES, ATTRIB_POS_LOCATION, type UniformName } from "./shaders";

export interface CompiledProgram {
  program: WebGLProgram;
  /** resolved location for every uniform in UNIFORM_NAMES (null if optimised out) */
  uniforms: Record<UniformName, WebGLUniformLocation | null>;
}

export interface ProgramResult {
  ok: boolean;
  program: CompiledProgram | null;
  error: string | null;
}

/** Compile a single shader; returns null + logs (caller surfaces the message). */
function compileShader(gl: WebGL2RenderingContext, type: number, src: string): { shader: WebGLShader | null; log: string } {
  const shader = gl.createShader(type);
  if (!shader) return { shader: null, log: "createShader returned null" };
  gl.shaderSource(shader, src);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) ?? "compile failed";
    gl.deleteShader(shader);
    return { shader: null, log };
  }
  return { shader, log: "" };
}

/** Build the composite program from sources. Never throws. */
export function buildProgram(gl: WebGL2RenderingContext, vertSrc: string, fragSrc: string): ProgramResult {
  const vert = compileShader(gl, gl.VERTEX_SHADER, vertSrc);
  if (!vert.shader) return { ok: false, program: null, error: `vertex: ${vert.log}` };
  const frag = compileShader(gl, gl.FRAGMENT_SHADER, fragSrc);
  if (!frag.shader) {
    gl.deleteShader(vert.shader);
    return { ok: false, program: null, error: `fragment: ${frag.log}` };
  }

  const program = gl.createProgram();
  if (!program) {
    gl.deleteShader(vert.shader);
    gl.deleteShader(frag.shader);
    return { ok: false, program: null, error: "createProgram returned null" };
  }
  gl.attachShader(program, vert.shader);
  gl.attachShader(program, frag.shader);
  // Bind the single position attribute to a fixed location so we don't depend on
  // the linker's choice (matches layout(location=0) in the shader).
  gl.bindAttribLocation(program, ATTRIB_POS_LOCATION, "aPos");
  gl.linkProgram(program);
  // Shaders can be detached/deleted after a successful link.
  gl.deleteShader(vert.shader);
  gl.deleteShader(frag.shader);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(program) ?? "link failed";
    gl.deleteProgram(program);
    return { ok: false, program: null, error: `link: ${log}` };
  }

  const uniforms = {} as Record<UniformName, WebGLUniformLocation | null>;
  for (const name of UNIFORM_NAMES) {
    uniforms[name] = gl.getUniformLocation(program, name);
  }
  return { ok: true, program: { program, uniforms }, error: null };
}
