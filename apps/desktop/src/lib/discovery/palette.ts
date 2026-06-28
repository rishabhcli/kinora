// Command palette core (⌘K) — a registry of commands + a fuzzy matcher that
// ranks them against a query. Pure + DOM-free so the matching logic is fully
// testable; the React layer (CommandPalette.tsx) just renders the ranked list
// and dispatches `run`.
import { fuzzyScore, normalize } from "./tokenize";

export type CommandGroup = "navigation" | "book" | "action" | "recent" | "setting";

export interface Command {
  id: string;
  /** Primary label shown in the palette. */
  title: string;
  group: CommandGroup;
  /** Extra searchable text (e.g. author, alternate names) — matched but not the
   *  primary ranked field. */
  keywords?: string[];
  /** A short hint shown on the right (e.g. a shortcut or subtitle). */
  hint?: string;
  /** Optional emoji/icon name for the row. */
  icon?: string;
  /** Side-effect to run when chosen. */
  run: () => void;
}

export interface RankedCommand {
  command: Command;
  score: number;
}

/** Group display order + weight: navigation/actions rank slightly above books so
 *  "go" finds the page before a book titled "Going…". */
const GROUP_BOOST: Record<CommandGroup, number> = {
  recent: 0.08,
  navigation: 0.06,
  action: 0.05,
  setting: 0.03,
  book: 0,
};

/**
 * Rank commands against a query. Empty query → the default order (recents first,
 * then by group order), preserving registration order within a group. Non-empty
 * → fuzzy over title + keywords, with a small group boost; non-matches dropped.
 */
export function rankCommands(commands: Command[], query: string): RankedCommand[] {
  const q = normalize(query);

  if (!q) {
    const ordered = commands
      .map((command, idx) => ({ command, idx }))
      .sort(
        (a, b) =>
          (GROUP_BOOST[b.command.group] - GROUP_BOOST[a.command.group]) || a.idx - b.idx,
      );
    return ordered.map(({ command }) => ({ command, score: 0 }));
  }

  const ranked: (RankedCommand & { idx: number })[] = [];
  commands.forEach((command, idx) => {
    let best = fuzzyScore(q, command.title);
    for (const kw of command.keywords ?? []) {
      best = Math.max(best, fuzzyScore(q, kw) * 0.9); // keyword hits worth a touch less
    }
    if (best <= 0) return;
    ranked.push({ command, score: best + GROUP_BOOST[command.group], idx });
  });

  ranked.sort((a, b) => b.score - a.score || a.idx - b.idx);
  return ranked.map(({ command, score }) => ({ command, score }));
}

/** Group ranked commands into sections for sectioned rendering, preserving the
 *  ranked order inside each section and the first-seen section order. */
export function groupRanked(ranked: RankedCommand[]): { group: CommandGroup; items: RankedCommand[] }[] {
  const order: CommandGroup[] = [];
  const byGroup = new Map<CommandGroup, RankedCommand[]>();
  for (const r of ranked) {
    const g = r.command.group;
    if (!byGroup.has(g)) {
      byGroup.set(g, []);
      order.push(g);
    }
    byGroup.get(g)!.push(r);
  }
  return order.map((group) => ({ group, items: byGroup.get(group)! }));
}

/** Clamp/normalize the highlighted index when navigating with ↑/↓ (wraps). */
export function moveSelection(current: number, delta: number, length: number): number {
  if (length === 0) return 0;
  return (((current + delta) % length) + length) % length;
}

export const GROUP_LABELS: Record<CommandGroup, string> = {
  navigation: "Go to",
  book: "Books",
  action: "Actions",
  recent: "Recent",
  setting: "Settings",
};
