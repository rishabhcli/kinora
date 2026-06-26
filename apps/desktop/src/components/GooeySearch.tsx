import { useState, useRef, useEffect } from "react";

export default function GooeySearch() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (open && inputRef.current) {
      const t = setTimeout(() => inputRef.current?.focus(), 100);
      return () => clearTimeout(t);
    }
  }, [open]);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as HTMLElement)) {
        if (value === "") setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [value]);

  useEffect(() => {
    return () => { if (closeTimer.current) clearTimeout(closeTimer.current); };
  }, []);

  const handleMouseEnter = () => {
    if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; }
    setOpen(true);
  };

  const handleMouseLeave = () => {
    if (value === "") {
      closeTimer.current = setTimeout(() => setOpen(false), 300);
    }
  };

  return (
    <div
      ref={wrapperRef}
      className="relative flex items-center"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      onClick={() => { if (!open) setOpen(true); }}
      style={{
        height: 28,
        borderRadius: "999px",
        width: open ? 180 : 28,
        background: open ? "rgba(40, 38, 34, 0.9)" : "transparent",
        boxShadow: open ? "0 2px 12px rgba(0,0,0,0.3)" : "none",
        transition: "width 0.3s cubic-bezier(0.4, 0, 0.2, 1), background 0.25s ease, box-shadow 0.25s ease",
        flexDirection: "row-reverse",
        cursor: "pointer",
        overflow: "hidden",
        zIndex: 10,
      }}
    >
      {/* Search icon — always visible, right side */}
      <div
        className="flex items-center justify-center flex-shrink-0"
        style={{ width: 28, height: 28 }}
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{ color: "#c4b8aa" }}
        >
          <circle cx="11" cy="11" r="7" />
          <path d="M16.5 16.5L21 21" />
        </svg>
      </div>

      {/* Input — fades in when expanded */}
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onClick={(e) => e.stopPropagation()}
        placeholder="Search"
        className="bg-transparent border-none outline-none text-[11px] flex-1 min-w-0"
        style={{
          color: "rgba(232, 226, 216, 0.9)",
          opacity: open ? 1 : 0,
          transition: "opacity 0.2s ease 0.15s",
          whiteSpace: "nowrap",
          paddingLeft: 8,
          paddingRight: 4,
          textAlign: "left",
          pointerEvents: open ? "auto" : "none",
        }}
      />
    </div>
  );
}
