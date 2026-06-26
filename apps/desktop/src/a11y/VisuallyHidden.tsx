import { createElement, type ElementType, type ReactNode } from "react";

// Renders content that is invisible on screen but available to screen readers
// (relies on the `.sr-only` rule in a11y.css). Use for icon-button labels,
// status text, headings that orient SR users but would clutter the visual design.

export interface VisuallyHiddenProps {
  children: ReactNode;
  /** Element to render (default `span`). */
  as?: ElementType;
  className?: string;
  [key: string]: unknown;
}

export function VisuallyHidden({ children, as = "span", className, ...rest }: VisuallyHiddenProps) {
  return createElement(
    as,
    { className: ["sr-only", className].filter(Boolean).join(" "), ...rest },
    children,
  );
}
