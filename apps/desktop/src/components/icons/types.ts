// The unified Kinora icon system — types & the SF-Symbols-style name registry.
//
// LICENSING NOTE: These glyphs are *original drawings* authored for Kinora that
// mirror Apple's SF Symbols visual language and naming conventions (dot-notation
// names, `.fill` variants, weight/scale/rendering-mode semantics). We do NOT ship
// Apple's SF Symbols assets — their license forbids redistribution and restricts
// them to Apple-platform UI. Drawing our own SF-Symbols-*compatible* set keeps us
// license-clean and renders identically on macOS / Windows / Linux (bundled SVG,
// not an OS font lookup).

/** SF Symbols weight semantics — drives stroke width for outline glyphs. */
export type SymbolWeight =
  | "ultralight"
  | "light"
  | "regular"
  | "medium"
  | "semibold"
  | "bold";

/** SF Symbols rendering modes we support. `hierarchical` fades secondary/tertiary
 *  layers of multi-layer glyphs; `monochrome` paints every layer in currentColor. */
export type RenderingMode = "monochrome" | "hierarchical";

/** A glyph layer's depth — only meaningful in `hierarchical` rendering. */
export type LayerRole = "primary" | "secondary" | "tertiary";

export interface GlyphLayer {
  /** SVG path data. */
  d: string;
  /** Solid shape painted with `fill: currentColor` (true) vs. an outline stroked
   *  with `stroke: currentColor` (false / default). */
  fill?: boolean;
  /** Hierarchical depth → opacity. Defaults to `primary` (full strength). */
  role?: LayerRole;
  /** Per-layer stroke-width multiplier (rare; e.g. a hairline inside a heavier mark). */
  strokeScale?: number;
  /** For filled layers with an interior cut-out (a `.fill` glyph knocking a symbol
   *  out of a solid shape) — `evenodd` lets a single currentColor path show a hole. */
  fillRule?: "evenodd" | "nonzero";
}

export interface GlyphDef {
  /** Defaults to "0 0 24 24" — the grid every Kinora glyph is drawn on. */
  viewBox?: string;
  layers: GlyphLayer[];
}

/** Every icon the app can render. Dot-notation + `.fill` variants mirror SF Symbols.
 *  The registry in `glyphs.ts` is typed `Record<IconName, GlyphDef>`, so adding a
 *  name here without drawing it (or vice-versa) is a compile error. */
export type IconName =
  // ── Navigation / primary chrome ──
  | "house"
  | "house.fill"
  | "books.vertical"
  | "books.vertical.fill"
  | "play.rectangle"
  | "play.rectangle.fill"
  | "heart"
  | "heart.fill"
  | "note.text"
  | "magnifyingglass"
  | "sparkles"
  // ── Generic controls ──
  | "chevron.left"
  | "chevron.right"
  | "chevron.up"
  | "chevron.down"
  | "chevron.up.chevron.down"
  | "arrow.left"
  | "arrow.right"
  | "xmark"
  | "xmark.circle.fill"
  | "checkmark"
  | "checkmark.circle.fill"
  | "plus"
  | "minus"
  | "arrow.counterclockwise"
  | "ellipsis"
  // ── Settings sidebar ──
  | "gearshape"
  | "gearshape.fill"
  | "paintbrush"
  | "textformat"
  | "textformat.size"
  | "film"
  | "film.fill"
  | "bell"
  | "bell.fill"
  | "lock"
  | "lock.shield"
  | "hand.raised"
  | "person"
  | "person.crop.circle"
  | "person.crop.circle.fill"
  | "info.circle"
  | "info.circle.fill"
  // ── Appearance / theme ──
  | "sun.max"
  | "sun.max.fill"
  | "moon"
  | "moon.fill"
  | "moon.stars"
  | "circle.lefthalf.filled"
  | "eye"
  | "eye.slash"
  // ── Media / playback ──
  | "play.fill"
  | "pause.fill"
  | "backward.fill"
  | "forward.fill"
  | "speaker.wave.2.fill"
  | "speaker.slash.fill"
  | "captions.bubble"
  | "slider.horizontal.3"
  | "gobackward"
  // ── Reading ──
  | "book"
  | "book.fill"
  | "bookmark"
  | "bookmark.fill"
  | "text.justify"
  | "textformat.alt"
  // ── Account / social / commerce ──
  | "envelope"
  | "rectangle.portrait.and.arrow.right"
  | "creditcard"
  | "globe"
  | "key"
  // ── Files / data ──
  | "square.and.arrow.up"
  | "square.and.arrow.down"
  | "trash"
  | "photo"
  | "doc.text"
  | "folder"
  // ── Status ──
  | "exclamationmark.triangle"
  | "clock"
  | "bolt.fill"
  | "star"
  | "star.fill"
  | "questionmark.circle";
