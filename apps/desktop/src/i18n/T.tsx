// <T> — a declarative translation component that renders ICU rich-text into real
// React elements. Use it when a message contains markup (`<b>…</b>`, `<link>…`)
// you want mapped to components, instead of the plain-string `t()`.
//
//   <T k="login.tagline" />
//   <T k="reading.readingAria" args={{ title }} />
//   <T k="terms.accept" components={{ link: (c) => <a href="/tos">{c}</a> }} />
//
// Tags with no matching component render their children inline (no wrapper),
// so an un-mapped `<b>` still contributes its text — never a broken element.

import { Fragment, type ReactNode } from "react";
import { useTParts } from "./useT.ts";
import type { Part } from "../lib/intl/icu/index.ts";
import type { IntlArgs } from "../lib/intl/types.ts";
import type { MessageKey } from "./messages.ts";

/** Maps an ICU tag name to a renderer that wraps the tag's children. */
export type Components = Record<string, (children: ReactNode) => ReactNode>;

/** Built-in mappings for the common inline markup tags. */
const DEFAULT_COMPONENTS: Components = {
  b: (c) => <strong>{c}</strong>,
  strong: (c) => <strong>{c}</strong>,
  i: (c) => <em>{c}</em>,
  em: (c) => <em>{c}</em>,
  u: (c) => <u>{c}</u>,
  br: () => <br />,
};

function renderParts(parts: Part[], components: Components, keyPrefix: string): ReactNode[] {
  return parts.map((part, i) => {
    const key = `${keyPrefix}.${i}`;
    if (part.type === "text") {
      return <Fragment key={key}>{part.value}</Fragment>;
    }
    const children = renderParts(part.children, components, key);
    const renderer = components[part.name] ?? components[part.name.toLowerCase()];
    if (renderer) {
      return <Fragment key={key}>{renderer(<>{children}</>)}</Fragment>;
    }
    // Unknown tag → render children inline (graceful, never breaks the tree).
    return <Fragment key={key}>{children}</Fragment>;
  });
}

export interface TProps {
  /** The (type-checked) message key. */
  k: MessageKey;
  /** ICU arguments. */
  args?: IntlArgs;
  /** Tag-name → renderer overrides, merged over the built-ins. */
  components?: Components;
}

export function T({ k, args, components }: TProps) {
  const tParts = useTParts();
  const parts = tParts(k, args);
  const merged = components ? { ...DEFAULT_COMPONENTS, ...components } : DEFAULT_COMPONENTS;
  return <>{renderParts(parts, merged, k)}</>;
}
