// The colour-grade model fed to the compositor's fragment shader, plus a small
// preset library and a CPU reference implementation. Pure + DOM-free so the grade
// math is unit-testable independent of any GL context: the same `applyGrade()`
// the test exercises mirrors the GLSL in shaders.ts (lift/gamma/gain → saturation
// → vignette), which lets us assert the grade is identity by default and behaves
// monotonically without spinning up WebGL.

export type RGB = readonly [number, number, number];

/** ASC-CDL-ish grade: per-channel gain then lift, gamma, then global saturation,
 *  plus a vignette + film-grain amount that the shader animates. All identity at
 *  the {@link NEUTRAL_GRADE} defaults so the GPU path is a no-op until styled. */
export interface FilmGrade {
  /** added after gain, per channel (shadows offset) */
  lift: RGB;
  /** power curve denominator per channel (1 = linear) */
  gamma: RGB;
  /** multiplier per channel (highlights / white balance) */
  gain: RGB;
  /** 1 = unchanged; 0 = greyscale; >1 = punchier */
  saturation: number;
  /** 0 = off; corner darkening strength */
  vignette: number;
  /** 0 = off; animated luma dither amount */
  grain: number;
}

export const NEUTRAL_GRADE: FilmGrade = {
  lift: [0, 0, 0],
  gamma: [1, 1, 1],
  gain: [1, 1, 1],
  saturation: 1,
  vignette: 0,
  grain: 0,
};

/** A handful of cinematic looks the reading room can apply per book / mood. Kept
 *  subtle — the film should read as graded, not filtered. */
export const GRADE_PRESETS: Record<string, FilmGrade> = {
  neutral: NEUTRAL_GRADE,
  // Warm, slightly lifted blacks, gentle vignette — a "lamplight" reading mood.
  warm: {
    lift: [0.02, 0.01, -0.01],
    gamma: [1.0, 0.98, 0.95],
    gain: [1.06, 1.02, 0.96],
    saturation: 1.08,
    vignette: 0.18,
    grain: 0.015,
  },
  // Cool, contrasty — a "night" / thriller mood.
  cool: {
    lift: [-0.01, 0, 0.02],
    gamma: [1.02, 1.0, 0.98],
    gain: [0.97, 1.0, 1.07],
    saturation: 0.96,
    vignette: 0.24,
    grain: 0.02,
  },
  // Faded, low-saturation — a "memory" / sepia-leaning flashback.
  faded: {
    lift: [0.04, 0.03, 0.02],
    gamma: [1.0, 1.0, 1.0],
    gain: [1.0, 0.98, 0.92],
    saturation: 0.7,
    vignette: 0.3,
    grain: 0.03,
  },
} as const;

/** Resolve a preset by id, falling back to neutral for an unknown id. */
export function gradeByName(name: string | null | undefined): FilmGrade {
  return (name && GRADE_PRESETS[name]) || NEUTRAL_GRADE;
}

const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v);

/** CPU reference of the shader's grade (lift/gamma/gain → saturation), for tests
 *  and any non-GL fallback tinting. Vignette/grain are position/time dependent so
 *  they're excluded here (the GPU owns them). Returns clamped [0,1] rgb. */
export function applyGrade(color: RGB, grade: FilmGrade): RGB {
  const out: number[] = [0, 0, 0];
  for (let i = 0; i < 3; i++) {
    let c = color[i] * grade.gain[i] + grade.lift[i];
    const g = Math.max(grade.gamma[i], 0.001);
    c = Math.pow(Math.max(c, 0), 1 / g);
    out[i] = c;
  }
  const luma = 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2];
  for (let i = 0; i < 3; i++) {
    out[i] = clamp01(luma + (out[i] - luma) * grade.saturation);
  }
  return [out[0], out[1], out[2]];
}

/** Linearly interpolate two grades — used to animate a grade transition (e.g. a
 *  scene's mood shift) without popping. `t` in [0,1]. */
export function lerpGrade(a: FilmGrade, b: FilmGrade, t: number): FilmGrade {
  const k = t < 0 ? 0 : t > 1 ? 1 : t;
  const mixRGB = (x: RGB, y: RGB): RGB => [
    x[0] + (y[0] - x[0]) * k,
    x[1] + (y[1] - x[1]) * k,
    x[2] + (y[2] - x[2]) * k,
  ];
  const mix = (x: number, y: number): number => x + (y - x) * k;
  return {
    lift: mixRGB(a.lift, b.lift),
    gamma: mixRGB(a.gamma, b.gamma),
    gain: mixRGB(a.gain, b.gain),
    saturation: mix(a.saturation, b.saturation),
    vignette: mix(a.vignette, b.vignette),
    grain: mix(a.grain, b.grain),
  };
}
