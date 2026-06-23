import { useRef, useState } from "react";

/** A macOS-style search control: a glass circle that expands into a field on click. */
export function SearchField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const expanded = open || value.length > 0;

  return (
    <div
      className={`glass flex h-9 items-center overflow-hidden rounded-full transition-[width] duration-300 ease-out focus-within:ring-2 focus-within:ring-ember-glow/70 ${
        expanded ? "w-56" : "w-9"
      }`}
    >
      <button
        type="button"
        aria-label="Search library"
        onClick={() => {
          setOpen(true);
          requestAnimationFrame(() => inputRef.current?.focus());
        }}
        className="flex h-9 w-9 shrink-0 items-center justify-center text-white/70 transition hover:text-white focus-visible:text-white focus-visible:outline-none"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" strokeLinecap="round" />
        </svg>
      </button>
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => {
          if (!value) setOpen(false);
        }}
        placeholder="Search library"
        className={`min-w-0 bg-transparent pr-3 text-sm text-white placeholder-white/40 outline-none transition-opacity ${
          expanded ? "w-full opacity-100" : "w-0 opacity-0"
        }`}
      />
    </div>
  );
}
