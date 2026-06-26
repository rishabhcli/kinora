import type { CSSProperties } from "react";
import type { IconName, RenderingMode, SymbolWeight } from "./types";
import { GLYPHS } from "./glyphs";
import { hierarchicalOpacity, resolveAccessibility, weightToStrokeWidth } from "./symbol";

export interface IconProps {
  /** SF-Symbols-style name, e.g. `book.fill`. */
  name: IconName;
  /** Rendered px (square). Default 18. */
  size?: number;
  /** SF Symbols weight → stroke thickness for outline glyphs. Default `regular`. */
  weight?: SymbolWeight;
  /** `hierarchical` fades secondary/tertiary layers; `monochrome` is flat. Default monochrome. */
  mode?: RenderingMode;
  className?: string;
  /** Accessible label. Present → `role="img"` + `<title>`; absent → decorative (hidden).
   *  Icon-only *buttons* should still label the button itself (Agent 6 a11y checklist). */
  title?: string;
  style?: CSSProperties;
}

const DEFAULT_VIEWBOX = "0 0 24 24";

/**
 * The one icon primitive for the whole app. Colour comes from `currentColor`
 * (so it inherits text colour / Agent 8 tokens); crisp at any size because it's
 * vector. Tree-shaken per-glyph isn't possible with a single registry, but the
 * full set is a few KB and ships once.
 */
export function Icon({
  name,
  size = 18,
  weight = "regular",
  mode = "monochrome",
  className,
  title,
  style,
}: IconProps) {
  const glyph = GLYPHS[name];
  const a11y = resolveAccessibility(title);
  const strokeWidth = weightToStrokeWidth(weight, size);

  return (
    <svg
      width={size}
      height={size}
      viewBox={glyph.viewBox ?? DEFAULT_VIEWBOX}
      className={className}
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }}
      fill="none"
      {...a11y}
    >
      {title ? <title>{title}</title> : null}
      {glyph.layers.map((layer, i) => {
        const opacity = mode === "hierarchical" ? hierarchicalOpacity(layer.role) : 1;
        const common = { d: layer.d, opacity: opacity === 1 ? undefined : opacity } as const;
        return layer.fill ? (
          <path key={i} {...common} fill="currentColor" fillRule={layer.fillRule} />
        ) : (
          <path
            key={i}
            {...common}
            stroke="currentColor"
            strokeWidth={strokeWidth * (layer.strokeScale ?? 1)}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        );
      })}
    </svg>
  );
}

export default Icon;
