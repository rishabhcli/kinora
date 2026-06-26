// The Kinora glyph registry — original SF-Symbols-*style* drawings on a 24-unit
// grid (see the licensing note in `types.ts`). Pure data so it's testable with
// `node --test`; <Icon> turns these into SVG. `.fill` variants that need an
// interior cut-out use a single evenodd path so one currentColor shows a hole.
import type { GlyphDef, IconName } from "./types";

// Reusable circle-as-path: M(cx-r) cy a r r 0 1 0 2r 0 a r r 0 1 0 -2r 0 z
const circle = (cx: number, cy: number, r: number) =>
  `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0z`;

const DISC = circle(12, 12, 10); // status / *.circle.fill backplate

export const GLYPHS: Record<IconName, GlyphDef> = {
  // ── Navigation / primary chrome ──
  house: {
    layers: [
      { d: "M3 10.5 12 3l9 7.5" },
      { d: "M5 9.5V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.5" },
      { d: "M9.5 21v-6h5v6" },
    ],
  },
  "house.fill": {
    layers: [
      {
        d: "M11.3 3.2 3.6 9.6c-.4.3-.6.8-.6 1.3V20c0 .6.4 1 1 1h4.5v-5.5c0-.8.7-1.5 1.5-1.5h2c.8 0 1.5.7 1.5 1.5V21H20c.6 0 1-.4 1-1v-9.1c0-.5-.2-1-.6-1.3l-7.7-6.4c-.4-.3-1-.3-1.4 0z",
        fill: true,
      },
    ],
  },
  "books.vertical": {
    layers: [
      { d: "M5 5h3.2v14H5z" },
      { d: "M9.6 5h3.2v14H9.6z" },
      { d: "M15 5.4l3.4.9-2.5 13.3-3.4-.9z" },
    ],
  },
  "books.vertical.fill": {
    layers: [
      { d: "M5 5h3.2v14H5z", fill: true },
      { d: "M9.7 5h3.2v14H9.7z", fill: true },
      { d: "M15.1 5.5l3.3.9-2.5 13.2-3.3-.9z", fill: true },
    ],
  },
  "play.rectangle": {
    layers: [
      { d: "M4.5 5h15c.8 0 1.5.7 1.5 1.5v11c0 .8-.7 1.5-1.5 1.5h-15C3.7 19 3 18.3 3 17.5v-11C3 5.7 3.7 5 4.5 5z" },
      { d: "M10 8.8v6.4l5-3.2z", fill: true },
    ],
  },
  "play.rectangle.fill": {
    layers: [
      {
        d: "M4.5 5h15c.8 0 1.5.7 1.5 1.5v11c0 .8-.7 1.5-1.5 1.5h-15C3.7 19 3 18.3 3 17.5v-11C3 5.7 3.7 5 4.5 5zM10 8.8v6.4l5-3.2z",
        fill: true,
        fillRule: "evenodd",
      },
    ],
  },
  heart: {
    layers: [
      {
        d: "M12 20.3S3.7 15.4 3.7 9.6C3.7 6.7 5.9 4.7 8.5 4.7c1.7 0 3 1 3.5 2 .5-1 1.8-2 3.5-2 2.6 0 4.8 2 4.8 4.9 0 5.8-8.3 10.7-8.3 10.7z",
      },
    ],
  },
  "heart.fill": {
    layers: [
      {
        d: "M12 20.3S3.7 15.4 3.7 9.6C3.7 6.7 5.9 4.7 8.5 4.7c1.7 0 3 1 3.5 2 .5-1 1.8-2 3.5-2 2.6 0 4.8 2 4.8 4.9 0 5.8-8.3 10.7-8.3 10.7z",
        fill: true,
      },
    ],
  },
  "note.text": {
    layers: [
      { d: "M6 3.5h9l4 4v12.5c0 .6-.4 1-1 1H6c-.6 0-1-.4-1-1v-15.5c0-.6.4-1 1-1z" },
      { d: "M15 3.5V8h4" },
      { d: "M8 12h8M8 15h8M8 18h5" },
    ],
  },
  magnifyingglass: {
    layers: [{ d: circle(11, 11, 7) }, { d: "M16.2 16.2 21 21" }],
  },
  sparkles: {
    layers: [
      { d: "M11 4l1.4 3.7L16 9l-3.6 1.3L11 14l-1.4-3.7L6 9l3.6-1.3z", fill: true },
      { d: "M18 13l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z", fill: true, role: "secondary" },
    ],
  },

  // ── Generic controls ──
  "chevron.left": { layers: [{ d: "M15 5l-7 7 7 7" }] },
  "chevron.right": { layers: [{ d: "M9 5l7 7-7 7" }] },
  "chevron.up": { layers: [{ d: "M5 15l7-7 7 7" }] },
  "chevron.down": { layers: [{ d: "M5 9l7 7 7-7" }] },
  "chevron.up.chevron.down": { layers: [{ d: "M8 10l4-4 4 4" }, { d: "M8 14l4 4 4-4" }] },
  "arrow.left": { layers: [{ d: "M11 6l-6 6 6 6" }, { d: "M5 12h14" }] },
  "arrow.right": { layers: [{ d: "M13 6l6 6-6 6" }, { d: "M19 12H5" }] },
  xmark: { layers: [{ d: "M6 6l12 12" }, { d: "M18 6 6 18" }] },
  "xmark.circle.fill": {
    layers: [
      { d: `${DISC}M8 9.4 9.4 8 16 14.6 14.6 16zM14.6 8 16 9.4 9.4 16 8 14.6z`, fill: true, fillRule: "evenodd" },
    ],
  },
  checkmark: { layers: [{ d: "M5 12.5l4.5 4.5L19 7" }] },
  "checkmark.circle.fill": {
    layers: [
      { d: `${DISC}M10.4 16 6.2 11.8 7.6 10.4 10.4 13.2 16.4 7.2 17.8 8.6z`, fill: true, fillRule: "evenodd" },
    ],
  },
  plus: { layers: [{ d: "M12 5v14" }, { d: "M5 12h14" }] },
  minus: { layers: [{ d: "M5 12h14" }] },
  "arrow.counterclockwise": {
    layers: [{ d: "M4.8 8.6A7.5 7.5 0 1 0 8 4.3" }, { d: "M4.2 4v4.6h4.6" }],
  },
  ellipsis: {
    layers: [
      { d: circle(6, 12, 1.5), fill: true },
      { d: circle(12, 12, 1.5), fill: true },
      { d: circle(18, 12, 1.5), fill: true },
    ],
  },

  // ── Settings sidebar ──
  gearshape: {
    layers: [
      {
        d: "M19.4 13a7.5 7.5 0 0 0 0-2l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-1.7-1l-.3-2.5h-4l-.3 2.5a7.5 7.5 0 0 0-1.7 1l-2.4-1-2 3.4 2 1.6a7.5 7.5 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 1.7 1l.3 2.5h4l.3-2.5a7.5 7.5 0 0 0 1.7-1l2.4 1 2-3.4z",
      },
      { d: circle(12, 12, 3) },
    ],
  },
  "gearshape.fill": {
    layers: [
      {
        d:
          "M19.4 13a7.5 7.5 0 0 0 0-2l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-1.7-1l-.3-2.5h-4l-.3 2.5a7.5 7.5 0 0 0-1.7 1l-2.4-1-2 3.4 2 1.6a7.5 7.5 0 0 0 0 2l-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 1.7 1l.3 2.5h4l.3-2.5a7.5 7.5 0 0 0 1.7-1l2.4 1 2-3.4z" +
          circle(12, 12, 2.9),
        fill: true,
        fillRule: "evenodd",
      },
    ],
  },
  paintbrush: {
    layers: [
      { d: "M20.5 4.9a1.9 1.9 0 0 0-2.7 0l-7.3 7.3 2.7 2.7 7.3-7.3a1.9 1.9 0 0 0 0-2.7z" },
      { d: "M10.2 12.5 8 14.7c-1.3 1.3-1.7 4.8-1.7 4.8s3.5-.4 4.8-1.7l2.2-2.2z" },
    ],
  },
  textformat: {
    layers: [{ d: "M4 18 8.5 6h1.3L14.3 18" }, { d: "M5.7 14h6.6" }],
  },
  "textformat.size": {
    layers: [
      { d: "M3 18 6.6 7.5h1.1L11.3 18" },
      { d: "M4.3 14.5h5.4" },
      { d: "M14.5 18 17 11h.9l2.5 7" },
      { d: "M15.4 16h3.4" },
    ],
  },
  film: {
    layers: [
      { d: "M4 5.5h16c.6 0 1 .4 1 1v11c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1v-11c0-.6.4-1 1-1z" },
      { d: "M7.5 5.7v12.6M16.5 5.7v12.6" },
      { d: "M3.2 9h4.3M16.5 9h4.3M3.2 12h17.6M3.2 15h4.3M16.5 15h4.3" },
    ],
  },
  "film.fill": {
    layers: [
      {
        d:
          "M4 5.5h16c.6 0 1 .4 1 1v11c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1v-11c0-.6.4-1 1-1z" +
          "M5 7.2h1.6v1.6H5zM5 11.2h1.6v1.6H5zM5 15.2h1.6v1.6H5z" +
          "M17.4 7.2H19v1.6h-1.6zM17.4 11.2H19v1.6h-1.6zM17.4 15.2H19v1.6h-1.6z",
        fill: true,
        fillRule: "evenodd",
      },
    ],
  },
  bell: {
    layers: [
      { d: "M5 16.5c1.2 0 1.8-.6 1.8-5.5a5.2 5.2 0 0 1 10.4 0c0 4.9.6 5.5 1.8 5.5z" },
      { d: "M10 19a2.2 2.2 0 0 0 4 0" },
    ],
  },
  "bell.fill": {
    layers: [
      { d: "M5 16.5c1.2 0 1.8-.6 1.8-5.5a5.2 5.2 0 0 1 10.4 0c0 4.9.6 5.5 1.8 5.5z", fill: true },
      { d: "M9.8 18.7h4.4a2.2 2.2 0 0 1-4.4 0z", fill: true },
    ],
  },
  lock: {
    layers: [
      { d: "M6 11h12c.6 0 1 .4 1 1v7c0 .6-.4 1-1 1H6c-.6 0-1-.4-1-1v-7c0-.6.4-1 1-1z" },
      { d: "M8 11V8.5a4 4 0 0 1 8 0V11" },
      { d: "M12 14.8v2.6" },
    ],
  },
  "lock.shield": {
    layers: [
      { d: "M12 3l7 2.2v5.3c0 4.6-3 8.2-7 9.5-4-1.3-7-4.9-7-9.5V5.2z" },
      { d: "M9.8 12.5h4.4v3.3H9.8z" },
      { d: "M10.7 12.5v-1a1.3 1.3 0 0 1 2.6 0v1" },
    ],
  },
  "hand.raised": {
    layers: [
      {
        d: "M8.5 12.5V6a1.25 1.25 0 0 1 2.5 0v5M11 11V4.75a1.25 1.25 0 0 1 2.5 0V11M13.5 11V5.5a1.25 1.25 0 0 1 2.5 0V13m0-1.5a1.25 1.25 0 0 1 2.5 0V15a6 6 0 0 1-6 6 6 6 0 0 1-5.2-3l-2-3.4a1.3 1.3 0 0 1 2.2-1.4l1.5 2.3",
      },
    ],
  },
  person: {
    layers: [{ d: circle(12, 9, 3.5) }, { d: "M5.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6" }],
  },
  "person.crop.circle": {
    layers: [
      { d: circle(12, 12, 9) },
      { d: circle(12, 10, 3) },
      { d: "M6.3 18.7a6.5 6.5 0 0 1 11.4 0" },
    ],
  },
  "person.crop.circle.fill": {
    layers: [
      {
        d: `${DISC}${circle(12, 9.6, 3)}M6 18.8a6.2 6.2 0 0 1 12 0 10 10 0 0 1-12 0z`,
        fill: true,
        fillRule: "evenodd",
      },
    ],
  },
  "info.circle": {
    layers: [
      { d: circle(12, 12, 9) },
      { d: circle(12, 8, 1), fill: true },
      { d: "M12 11.2v5.4" },
    ],
  },
  "info.circle.fill": {
    layers: [
      { d: `${DISC}${circle(12, 7.6, 1.15)}M10.85 10.6h2.3v6.8h-2.3z`, fill: true, fillRule: "evenodd" },
    ],
  },

  // ── Appearance / theme ──
  "sun.max": {
    layers: [
      { d: circle(12, 12, 4) },
      { d: "M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5.1 5.1l1.6 1.6M17.3 17.3l1.6 1.6M18.9 5.1l-1.6 1.6M6.7 17.3l-1.6 1.6" },
    ],
  },
  "sun.max.fill": {
    layers: [
      { d: circle(12, 12, 4.2), fill: true },
      { d: "M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5.1 5.1l1.6 1.6M17.3 17.3l1.6 1.6M18.9 5.1l-1.6 1.6M6.7 17.3l-1.6 1.6" },
    ],
  },
  moon: { layers: [{ d: "M20 14.5A8.5 8.5 0 0 1 9.5 4 7 7 0 1 0 20 14.5z" }] },
  "moon.fill": { layers: [{ d: "M20 14.5A8.5 8.5 0 0 1 9.5 4 7 7 0 1 0 20 14.5z", fill: true }] },
  "moon.stars": {
    layers: [
      { d: "M19 15.5A7.5 7.5 0 0 1 9.6 6 6 6 0 1 0 19 15.5z" },
      { d: "M18 3.5l.6 1.6 1.6.6-1.6.6L18 8l-.6-1.6L15.8 5.8l1.6-.6z", fill: true, role: "secondary" },
    ],
  },
  "circle.lefthalf.filled": {
    layers: [{ d: circle(12, 12, 9) }, { d: "M12 3a9 9 0 0 0 0 18z", fill: true }],
  },
  eye: {
    layers: [
      { d: "M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z" },
      { d: circle(12, 12, 2.6) },
    ],
  },
  "eye.slash": {
    layers: [
      { d: "M2.5 12S6 5.8 12 5.8c1.6 0 3 .4 4.2 1M21.5 12s-1.3 2.3-3.7 3.9M9.4 9.4a2.6 2.6 0 0 0 3.7 3.7" },
      { d: "M4 4l16 16" },
    ],
  },

  // ── Media / playback ──
  "play.fill": { layers: [{ d: "M8 5.2v13.6l11-6.8z", fill: true }] },
  "pause.fill": { layers: [{ d: "M8 5h2.8v14H8z", fill: true }, { d: "M13.2 5H16v14h-2.8z", fill: true }] },
  "backward.fill": { layers: [{ d: "M11 6 4 12l7 6z", fill: true }, { d: "M20 6l-7 6 7 6z", fill: true }] },
  "forward.fill": { layers: [{ d: "M4 6l7 6-7 6z", fill: true }, { d: "M13 6l7 6-7 6z", fill: true }] },
  "speaker.wave.2.fill": {
    layers: [
      { d: "M4 9.5h2.8L11 6v12L6.8 14.5H4z", fill: true },
      { d: "M14.5 8.8a4.5 4.5 0 0 1 0 6.4M17 6.2a8 8 0 0 1 0 11.6" },
    ],
  },
  "speaker.slash.fill": {
    layers: [
      { d: "M4 9.5h2.8L11 6v12L6.8 14.5H4z", fill: true },
      { d: "M15 9.5l5 5M20 9.5l-5 5" },
    ],
  },
  "captions.bubble": {
    layers: [
      { d: "M4 5.5h16c.6 0 1 .4 1 1v8c0 .6-.4 1-1 1h-9l-4 3.5V15.5H4c-.6 0-1-.4-1-1v-8c0-.6.4-1 1-1z" },
      { d: "M7 9.8h4M7 12.3h7M13 9.8h4" },
    ],
  },
  "slider.horizontal.3": {
    layers: [
      { d: "M3 7h18M3 12h18M3 17h18" },
      { d: circle(16, 7, 2.1), fill: true },
      { d: circle(8, 12, 2.1), fill: true },
      { d: circle(16, 17, 2.1), fill: true },
    ],
  },
  gobackward: {
    layers: [{ d: "M5.5 7.8A7.5 7.5 0 1 0 9 4.3" }, { d: "M9.6 3l-1.4 4.4 4.4 1.3" }],
  },

  // ── Reading ──
  book: {
    layers: [
      { d: "M12 6.6C10.4 5.6 7.8 5.1 4.8 5.3v12.4c3-.2 5.6.3 7.2 1.3" },
      { d: "M12 6.6c1.6-1 4.2-1.5 7.2-1.3v12.4c-3-.2-5.6.3-7.2 1.3" },
      { d: "M12 6.6v12.4" },
    ],
  },
  "book.fill": {
    layers: [
      { d: "M11.5 6.8C10 5.9 7.6 5.4 4.8 5.6c-.4 0-.8.4-.8.8v11.2c0 .5.4.8.9.8 2.6-.1 4.9.4 6.6 1.3z", fill: true },
      { d: "M12.5 6.8C14 5.9 16.4 5.4 19.2 5.6c.4 0 .8.4.8.8v11.2c0 .5-.4.8-.9.8-2.6-.1-4.9.4-6.6 1.3z", fill: true },
    ],
  },
  bookmark: { layers: [{ d: "M7 4.5h10c.6 0 1 .4 1 1V20l-6-3.5L6 20V5.5c0-.6.4-1 1-1z" }] },
  "bookmark.fill": { layers: [{ d: "M7 4.5h10c.6 0 1 .4 1 1V20l-6-3.5L6 20V5.5c0-.6.4-1 1-1z", fill: true }] },
  "text.justify": { layers: [{ d: "M4 6h16M4 10h16M4 14h16M4 18h16" }] },
  "textformat.alt": {
    layers: [
      { d: "M3 17 6.5 7h1.2L11.2 17" },
      { d: "M4.3 13.8h5.4" },
      { d: "M14.2 17v-4.4a2 2 0 0 1 4 0V17m0-2.2c-2.6 0-4 .6-4 2 0 1 .8 1.6 2 1.6s2-.7 2-1.8" },
    ],
  },

  // ── Account / social / commerce ──
  envelope: {
    layers: [
      { d: "M4 6.5h16c.6 0 1 .4 1 1v9c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1v-9c0-.6.4-1 1-1z" },
      { d: "M3.5 7.5 12 13l8.5-5.5" },
    ],
  },
  "rectangle.portrait.and.arrow.right": {
    layers: [
      { d: "M13 5.5H6.5c-.6 0-1 .4-1 1v11c0 .6.4 1 1 1H13" },
      { d: "M10.5 12h9.5" },
      { d: "M16.8 8.8 20 12l-3.2 3.2" },
    ],
  },
  creditcard: {
    layers: [
      { d: "M3 6.5h18c.6 0 1 .4 1 1v9c0 .6-.4 1-1 1H3c-.6 0-1-.4-1-1v-9c0-.6.4-1 1-1z" },
      { d: "M2 10h20" },
      { d: "M6 14h4" },
    ],
  },
  globe: {
    layers: [
      { d: circle(12, 12, 9) },
      { d: "M3 12h18" },
      { d: "M12 3c3 2.5 4.5 6 4.5 9s-1.5 6.5-4.5 9c-3-2.5-4.5-6-4.5-9s1.5-6.5 4.5-9z" },
    ],
  },
  key: {
    layers: [
      { d: circle(15, 9, 3.5) },
      { d: "M12.6 11.4 6 18" },
      { d: "M8.4 16.2 10 17.8" },
      { d: "M6.4 18 8 19.6" },
    ],
  },

  // ── Files / data ──
  "square.and.arrow.up": {
    layers: [
      { d: "M7 11v7c0 .6.4 1 1 1h8c.6 0 1-.4 1-1v-7" },
      { d: "M12 4v11" },
      { d: "M8.5 7.5 12 4l3.5 3.5" },
    ],
  },
  "square.and.arrow.down": {
    layers: [
      { d: "M7 11v7c0 .6.4 1 1 1h8c.6 0 1-.4 1-1v-7" },
      { d: "M12 4v11" },
      { d: "M8.5 11.5 12 15l3.5-3.5" },
    ],
  },
  trash: {
    layers: [
      { d: "M6 7.5l.9 12.1c0 .5.5.9 1 .9h8.2c.5 0 1-.4 1-.9L18 7.5" },
      { d: "M4 7.5h16" },
      { d: "M9.5 7.5V5.6c0-.6.4-1 1-1h3c.6 0 1 .4 1 1v1.9" },
      { d: "M10 11v6M14 11v6" },
    ],
  },
  photo: {
    layers: [
      { d: "M4 5.5h16c.6 0 1 .4 1 1v11c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1v-11c0-.6.4-1 1-1z" },
      { d: circle(8.5, 9.5, 1.5) },
      { d: "M4.5 17.5 9 12l3 3 3.5-3.5L20 16" },
    ],
  },
  "doc.text": {
    layers: [
      { d: "M6 3.5h7l5 5v11c0 .6-.4 1-1 1H6c-.6 0-1-.4-1-1v-15c0-.6.4-1 1-1z" },
      { d: "M13 3.5V9h5" },
      { d: "M8 12.5h8M8 15.5h8M8 18.5h5" },
    ],
  },
  folder: {
    layers: [
      { d: "M3 7c0-.6.4-1 1-1h4.6l2 2H20c.6 0 1 .4 1 1v9c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1z" },
    ],
  },

  // ── Status ──
  "exclamationmark.triangle": {
    layers: [
      { d: "M10.3 4.5 2.5 18.5c-.7 1.3.2 3 1.7 3h15.6c1.5 0 2.4-1.7 1.7-3L13.7 4.5a2 2 0 0 0-3.4 0z" },
      { d: "M12 10v4.5" },
      { d: circle(12, 17.6, 1), fill: true },
    ],
  },
  clock: { layers: [{ d: circle(12, 12, 9) }, { d: "M12 7.5V12l3 2" }] },
  "bolt.fill": { layers: [{ d: "M13 2 4 13.5h6L9 22l9-12h-6z", fill: true }] },
  star: {
    layers: [{ d: "M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8L12 17l-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z" }],
  },
  "star.fill": {
    layers: [{ d: "M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8L12 17l-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z", fill: true }],
  },
  "questionmark.circle": {
    layers: [
      { d: circle(12, 12, 9) },
      { d: "M9.6 9.5a2.5 2.5 0 0 1 4.8 1c0 1.7-2.4 2-2.4 3.6" },
      { d: circle(12, 17.3, 1), fill: true },
    ],
  },
};

/** Every registered icon name (runtime list mirroring the `IconName` union). */
export const ICON_NAMES = Object.keys(GLYPHS) as IconName[];
