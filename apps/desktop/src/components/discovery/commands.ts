// Command registry builder — assembles the ⌘K command list from the app's nav
// targets, the catalog (one "open <book>" command each), recent books, and a few
// global actions. Pure: inputs → Command[]. The host wires the `run` callbacks.
import type { Command } from "../../lib/discovery/palette";
import type { DiscoveryBook } from "../../lib/discovery/types";
import { resolveRecents } from "../../lib/discovery/recents";

export interface CommandContext {
  /** Nav page labels (e.g. Navbar's navItems labels + Pricing/Settings). */
  navTargets: { label: string; hint?: string }[];
  navigate: (label: string) => void;
  books: DiscoveryBook[];
  recents?: string[];
  openBook: (book: DiscoveryBook) => void;
  /** Open the full search surface (optionally seeded with a query). */
  openSearch?: (query?: string) => void;
  /** Resume the single best continue-reading book, if any. */
  resume?: () => void;
}

export function buildCommands(ctx: CommandContext): Command[] {
  const commands: Command[] = [];

  // 1. Recent books (most useful — surfaced first).
  const recentBooks = resolveRecents(ctx.recents ?? [], ctx.books).slice(0, 5);
  for (const b of recentBooks) {
    commands.push({
      id: `recent-${b.id}`,
      title: b.title,
      group: "recent",
      keywords: [b.author, b.genre ?? ""].filter(Boolean),
      hint: "Recent",
      icon: "🕮",
      run: () => ctx.openBook(b),
    });
  }

  // 2. Navigation.
  for (const target of ctx.navTargets) {
    commands.push({
      id: `nav-${target.label}`,
      title: `Go to ${target.label}`,
      group: "navigation",
      keywords: [target.label],
      hint: target.hint,
      icon: "→",
      run: () => ctx.navigate(target.label),
    });
  }

  // 3. Global actions.
  if (ctx.resume) {
    commands.push({
      id: "action-resume",
      title: "Resume reading",
      group: "action",
      keywords: ["continue", "last"],
      icon: "▶",
      run: ctx.resume,
    });
  }
  if (ctx.openSearch) {
    commands.push({
      id: "action-search",
      title: "Search the library",
      group: "action",
      keywords: ["find", "filter", "browse"],
      icon: "⌕",
      run: () => ctx.openSearch!(),
    });
  }

  // 4. Every book (excluding ones already in recents to avoid duplicates).
  const recentIds = new Set(recentBooks.map((b) => b.id));
  for (const b of ctx.books) {
    if (recentIds.has(b.id)) continue;
    commands.push({
      id: `book-${b.id}`,
      title: b.title,
      group: "book",
      keywords: [b.author, b.genre ?? "", b.era ?? ""].filter(Boolean),
      hint: b.author,
      icon: "📖",
      run: () => ctx.openBook(b),
    });
  }

  return commands;
}
