// GLSL for the film compositor (WebGL2 / GLSL ES 3.00). Two video textures are
// sampled and blended on the GPU: a crossfade `mix` between the outgoing and
// incoming clip, then a cinematic colour grade (lift/gamma/gain + saturation +
// vignette) and an animated film-grain dither. This is the GPU path that layers
// OVER today's opacity crossfade (FilmPane); it is a visual enhancement only —
// the no-black-frame guarantee is upheld by the CSS path remaining the source of
// truth for *what src is on screen*, and by the compositor falling back to a
// straight passthrough when a texture isn't ready.
//
// Kept as plain strings here (no DOM, no GL context) so the program inventory,
// uniform names, and attribute layout are unit-testable and reviewable in
// isolation from any GPU. `program.ts` compiles them; `webglCompositor.ts` drives
// them. The shader sources never reference time/clock directly — `uTime` and all
// grade parameters are uniforms, so the same program animates deterministically
// under test.

/** A fullscreen-triangle vertex shader: one oversized triangle covers the
 *  viewport (cheaper than a quad, no diagonal seam). `aPos` is clip-space; `vUv`
 *  is the derived [0,1] texture coordinate. */
export const VERT_SRC = `#version 300 es
precision highp float;
layout(location = 0) in vec2 aPos;
out vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

/** The composite fragment shader: crossfade → grade → grain.
 *
 *  Uniforms:
 *   - uTexA / uTexB     the two film layers (outgoing, incoming)
 *   - uMix              0 = pure A, 1 = pure B (the crossfade position)
 *   - uHasB             1.0 when B is a real texture, else 0.0 (passthrough A)
 *   - uFlipA / uFlipB   1.0 to flip the layer vertically (WebGL textures are
 *                       bottom-up; HTMLVideoElement upload may need a flip)
 *   - uLift/uGamma/uGain per-channel ASC-CDL-ish grade (vec3 each)
 *   - uSaturation       1 = unchanged
 *   - uVignette         0 = off, 1 = strong corner falloff
 *   - uGrain            0 = off; amount of animated luma dither
 *   - uTime             seconds, animates the grain
 */
export const FRAG_SRC = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 fragColor;

uniform sampler2D uTexA;
uniform sampler2D uTexB;
uniform float uMix;
uniform float uHasB;
uniform float uFlipA;
uniform float uFlipB;
uniform vec3 uLift;
uniform vec3 uGamma;
uniform vec3 uGain;
uniform float uSaturation;
uniform float uVignette;
uniform float uGrain;
uniform float uTime;

// Cheap hash for per-pixel grain (no texture lookup).
float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

vec3 grade(vec3 c) {
  // Lift/gamma/gain: gain*(c)+lift, then gamma. Guard gamma against 0.
  c = c * uGain + uLift;
  vec3 g = max(uGamma, vec3(0.001));
  c = pow(max(c, vec3(0.0)), vec3(1.0) / g);
  // Saturation around Rec.709 luma.
  float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
  c = mix(vec3(l), c, uSaturation);
  return c;
}

void main() {
  vec2 uvA = vec2(vUv.x, mix(vUv.y, 1.0 - vUv.y, uFlipA));
  vec2 uvB = vec2(vUv.x, mix(vUv.y, 1.0 - vUv.y, uFlipB));
  vec3 a = texture(uTexA, uvA).rgb;
  vec3 b = texture(uTexB, uvB).rgb;
  // Crossfade only counts B when it's a real texture; otherwise passthrough A so
  // a not-yet-ready incoming layer never darkens the frame.
  float m = uMix * uHasB;
  vec3 col = mix(a, b, m);
  col = grade(col);
  // Radial vignette toward the corners.
  vec2 d = vUv - 0.5;
  float vig = 1.0 - uVignette * dot(d, d) * 2.0;
  col *= clamp(vig, 0.0, 1.0);
  // Animated film grain (luma dither), kept subtle.
  if (uGrain > 0.0) {
    float n = hash(vUv * 1024.0 + fract(uTime) * 1000.0) - 0.5;
    col += n * uGrain;
  }
  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
`;

/** The fullscreen triangle's clip-space positions (3 verts, 2 floats each). */
export const FULLSCREEN_TRIANGLE = new Float32Array([-1, -1, 3, -1, -1, 3]);

/** Every uniform the fragment program exposes — the single source of truth so
 *  the compositor's `getUniformLocation` calls and tests can't drift from GLSL. */
export const UNIFORM_NAMES = [
  "uTexA",
  "uTexB",
  "uMix",
  "uHasB",
  "uFlipA",
  "uFlipB",
  "uLift",
  "uGamma",
  "uGain",
  "uSaturation",
  "uVignette",
  "uGrain",
  "uTime",
] as const;

export type UniformName = (typeof UNIFORM_NAMES)[number];

/** The single vertex attribute (location 0, matching `layout(location=0)`). */
export const ATTRIB_POS_LOCATION = 0;
