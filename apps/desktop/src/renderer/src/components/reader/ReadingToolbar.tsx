import { type ReactNode, useEffect, useRef, useState } from "react";

import { NATIVE_TOP_INSET, useNativeShell } from "../../hooks/useNativeShell";
import { type UseReadingThemeResult } from "../../lib/readingTheme";
import { ThemePopover } from "./ThemePopover";

interface ReadingToolbarProps {
  title: string;
  author: string | null;
  reading: UseReadingThemeResult;
  bookmarked: boolean;
  onToggleBookmark: () => void;
  search: string;
  onSearch: (value: string) => void;
  onBack: () => void;
  onShare: () => void;
  shareConfirmed: boolean;
}

function IconButton({
  label,
  on,
  onClick,
  children,
  refEl,
}: {
  label: string;
  on?: boolean;
  onClick: () => void;
  children: ReactNode;
  refEl?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <button
      ref={refEl}
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={on}
      data-on={on ? "true" : undefined}
      onClick={onClick}
      className="toolbar-btn no-drag"
    >
      {children}
    </button>
  );
}

/**
 * The reading-room top toolbar (Apple Books): Library/back · AA (typography) ·
 * Search · Bookmark · Share, as refined glass icon buttons over the draggable
 * titlebar. AA opens the Themes & Settings popover; Search expands an inline
 * find-on-page field. The center shows the book's title + author.
 */
export function ReadingToolbar({
  title,
  author,
  reading,
  bookmarked,
  onToggleBookmark,
  search,
  onSearch,
  onBack,
  onShare,
  shareConfirmed,
}: ReadingToolbarProps) {
  const [themeOpen, setThemeOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const searchExpanded = searchOpen || search.length > 0;
  const native = useNativeShell();

  useEffect(() => {
    if (searchOpen) searchRef.current?.focus();
  }, [searchOpen]);

  return (
    // Inside the native shell, sit below its glass title strip and relax the
    // traffic-light gutter (the native window owns the controls there).
    <header
      className={`drag relative z-40 flex shrink-0 items-center gap-2 border-b border-white/10 pr-4 ${
        native ? "pl-5" : "h-16 pl-24"
      }`}
      style={native ? { paddingTop: NATIVE_TOP_INSET, height: NATIVE_TOP_INSET + 64 } : undefined}
    >
      <IconButton label="Back to library" onClick={onBack}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M15 5l-7 7 7 7" />
          <path d="M9 12h11" opacity="0.55" />
        </svg>
      </IconButton>

      {/* Center title block. */}
      <div className="pointer-events-none mx-1 flex min-w-0 flex-1 flex-col items-center px-2 text-center">
        <p className="max-w-full truncate font-display text-[15px] font-semibold leading-tight text-white [text-shadow:0_1px_8px_rgba(0,0,0,0.55)]">
          {title}
        </p>
        {author && <p className="max-w-full truncate text-[11px] text-white/45">{author}</p>}
      </div>

      <div className="no-drag flex items-center gap-2">
        {/* AA — typography / themes */}
        <div className="relative">
          <IconButton
            label="Typography and themes"
            on={themeOpen}
            onClick={() => setThemeOpen((v) => !v)}
          >
            <span className="font-display text-[15px] font-semibold leading-none">
              A<span className="text-[11px]">A</span>
            </span>
          </IconButton>
          {themeOpen && <ThemePopover {...reading} onClose={() => setThemeOpen(false)} />}
        </div>

        {/* Search — expands inline */}
        <div
          className={`glass flex h-9 items-center overflow-hidden rounded-full transition-[width] duration-300 ease-out ${
            searchExpanded ? "w-52" : "w-9"
          }`}
        >
          <button
            type="button"
            aria-label="Search in this book"
            onClick={() => setSearchOpen(true)}
            className="flex h-9 w-9 shrink-0 items-center justify-center text-white/75 transition hover:text-white focus-visible:outline-none"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" strokeLinecap="round" />
            </svg>
          </button>
          <input
            ref={searchRef}
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            onBlur={() => {
              if (!search) setSearchOpen(false);
            }}
            placeholder="Find in book"
            className={`min-w-0 bg-transparent pr-3 text-sm text-white placeholder-white/40 outline-none transition-opacity ${
              searchExpanded ? "w-full opacity-100" : "w-0 opacity-0"
            }`}
          />
        </div>

        <IconButton label={bookmarked ? "Remove bookmark" : "Add bookmark"} on={bookmarked} onClick={onToggleBookmark}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill={bookmarked ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4.5L5 21V4a1 1 0 0 1 1-1Z" />
          </svg>
        </IconButton>

        <IconButton label={shareConfirmed ? "Link copied" : "Share"} on={shareConfirmed} onClick={onShare}>
          {shareConfirmed ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="m5 13 4 4L19 7" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 15V4M8.5 7.5 12 4l3.5 3.5" />
              <path d="M5 12v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" />
            </svg>
          )}
        </IconButton>
      </div>
    </header>
  );
}
