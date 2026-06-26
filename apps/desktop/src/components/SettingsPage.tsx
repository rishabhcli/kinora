import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { startAppearanceSync } from "../lib/appearance";
import { Icon } from "./icons";
import { SETTINGS_SECTIONS } from "./settings/sections";
import "./settings/settings.css";

export default function SettingsPage() {
  const reduce = useReducedMotion();
  const [activeId, setActiveId] = useState(SETTINGS_SECTIONS[0].id);
  const [query, setQuery] = useState("");
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  // Appearance overrides (reduce motion / transparency / contrast) go live and
  // stay synced with the store + OS while Settings is mounted (and after).
  useEffect(() => startAppearanceSync(), []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return SETTINGS_SECTIONS;
    return SETTINGS_SECTIONS.filter(
      (s) => s.label.toLowerCase().includes(q) || s.keywords.toLowerCase().includes(q),
    );
  }, [query]);

  // Keep a valid selection when the filter changes.
  useEffect(() => {
    if (filtered.length && !filtered.some((s) => s.id === activeId)) {
      setActiveId(filtered[0].id);
    }
  }, [filtered, activeId]);

  const active = SETTINGS_SECTIONS.find((s) => s.id === activeId) ?? SETTINGS_SECTIONS[0];
  const ActiveComponent = active.Component;

  const onSidebarKey = (e: React.KeyboardEvent) => {
    const idx = filtered.findIndex((s) => s.id === activeId);
    if (idx < 0) return;
    let next = idx;
    if (e.key === "ArrowDown") next = Math.min(filtered.length - 1, idx + 1);
    else if (e.key === "ArrowUp") next = Math.max(0, idx - 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = filtered.length - 1;
    else return;
    e.preventDefault();
    const id = filtered[next].id;
    setActiveId(id);
    tabRefs.current[id]?.focus();
  };

  return (
    <div className="pt-12 pb-8 px-6 max-w-[1100px] mx-auto relative z-10">
      <div className="flex items-end justify-between gap-4 mb-6 pt-4">
        <h1 className="font-serif text-2xl font-semibold text-kinora-text">Settings</h1>
        <div className="relative" style={{ width: 240 }}>
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-kinora-subtle pointer-events-none">
            <Icon name="magnifyingglass" size={14} />
          </span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search settings"
            aria-label="Search settings"
            className="glass-input w-full pl-8 pr-3 py-2 rounded-xl text-[12.5px] text-kinora-text"
          />
        </div>
      </div>

      <div className="flex gap-7 items-start">
        {/* Sidebar */}
        <nav
          role="tablist"
          aria-label="Settings categories"
          aria-orientation="vertical"
          onKeyDown={onSidebarKey}
          className="w-[210px] shrink-0 sticky top-16"
        >
          {filtered.length === 0 && <p className="text-[12px] text-kinora-muted px-3 py-2">No matches.</p>}
          {filtered.map((s) => {
            const selected = s.id === activeId;
            return (
              <button
                key={s.id}
                ref={(el) => (tabRefs.current[s.id] = el)}
                role="tab"
                id={`tab-${s.id}`}
                aria-selected={selected}
                aria-controls="settings-panel"
                tabIndex={selected ? 0 : -1}
                onClick={() => setActiveId(s.id)}
                className={`kn-set-focusable w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-[13px] mb-0.5 transition-colors ${
                  selected ? "text-kinora-text" : "text-kinora-muted hover:text-kinora-text"
                }`}
                style={selected ? { background: "rgba(212,164,78,0.16)" } : undefined}
              >
                <span style={{ color: selected ? "#e8c878" : undefined }}>
                  <Icon name={selected ? s.activeIcon : s.icon} size={17} weight={selected ? "medium" : "regular"} />
                </span>
                <span className="font-medium">{s.label}</span>
              </button>
            );
          })}
        </nav>

        {/* Detail pane */}
        <div
          id="settings-panel"
          role="tabpanel"
          aria-labelledby={`tab-${active.id}`}
          className="flex-1 min-w-0 kn-set-scroll"
        >
          <AnimatePresence mode="wait">
            <motion.div
              key={active.id}
              initial={reduce ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: -6 }}
              transition={{ duration: reduce ? 0 : 0.22, ease: [0.22, 1, 0.36, 1] }}
            >
              <ActiveComponent />
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
