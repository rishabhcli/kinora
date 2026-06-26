#!/usr/bin/env node
/* WCAG 2.1 contrast gate for the Kinora token palette (Agent 08).
   Mirrors the RGB triples in src/styles/tokens.css. Run: `node scripts/check-contrast.mjs`
   Exits non-zero if any audited text/background pair falls below its target
   (AA: 4.5 normal text, 3.0 large text / UI / non-text). Keep this green. */

const srgb = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const L = ([r, g, b]) => 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
const ratio = (a, b) => {
  const hi = Math.max(L(a), L(b)), lo = Math.min(L(a), L(b));
  return (hi + 0.05) / (lo + 0.05);
};

// ── DARK spine + ink + accents (tokens.css :root) ──
const D = {
  bgDeep: [11, 10, 8], bg: [21, 19, 15], surface: [30, 27, 22], surfaceRaised: [39, 35, 28],
  text: [237, 231, 219], muted: [178, 167, 150], subtle: [143, 133, 118],
  accent: [224, 166, 78], accentStrong: [242, 208, 138], accentCool: [122, 169, 173],
  success: [104, 188, 132], warning: [232, 180, 74], danger: [233, 116, 102], info: [122, 168, 206],
};
// ── reading-theme ink/paper pairs (Agent 06 binds these) ──
const READ = {
  nightInk: [210, 205, 196], nightBg: [0, 0, 0],
  sepiaInk: [60, 48, 32], sepiaBg: [240, 231, 213],
  paperInk: [30, 28, 26], paperBg: [247, 244, 238],
  contrastInk: [255, 255, 255], contrastBg: [0, 0, 0],
};

const pairs = [
  ["text / bg", D.text, D.bg, 4.5],
  ["text / bg-deep", D.text, D.bgDeep, 4.5],
  ["text / surface", D.text, D.surface, 4.5],
  ["text / surface-raised", D.text, D.surfaceRaised, 4.5],
  ["muted / bg", D.muted, D.bg, 4.5],
  ["muted / surface", D.muted, D.surface, 4.5],
  ["muted / surface-raised", D.muted, D.surfaceRaised, 4.5],
  ["subtle / bg (normal text)", D.subtle, D.bg, 4.5],
  ["subtle / surface (large/UI)", D.subtle, D.surface, 3.0],
  ["accent / bg", D.accent, D.bg, 4.5],
  ["accent / surface", D.accent, D.surface, 4.5],
  ["accent-strong / bg", D.accentStrong, D.bg, 4.5],
  ["accent-cool / bg (large/UI)", D.accentCool, D.bg, 3.0],
  ["bg-deep ink / accent CTA", D.bgDeep, D.accent, 4.5],
  ["success / bg (UI)", D.success, D.bg, 3.0],
  ["warning / bg (UI)", D.warning, D.bg, 3.0],
  ["danger / bg (UI)", D.danger, D.bg, 3.0],
  ["info / bg (UI)", D.info, D.bg, 3.0],
  ["read night ink / bg", READ.nightInk, READ.nightBg, 4.5],
  ["read sepia ink / paper", READ.sepiaInk, READ.sepiaBg, 4.5],
  ["read paper ink / paper", READ.paperInk, READ.paperBg, 4.5],
  ["read contrast ink / bg", READ.contrastInk, READ.contrastBg, 7.0],
];

let fail = 0;
for (const [name, fg, bg, min] of pairs) {
  const r = ratio(fg, bg);
  const ok = r >= min;
  if (!ok) fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${r.toFixed(2)}  (>=${min})  ${name}`);
}
console.log(`\n${fail === 0 ? "ALL PASS — palette meets WCAG AA" : fail + " FAILURE(S)"}`);
process.exit(fail === 0 ? 0 : 1);
