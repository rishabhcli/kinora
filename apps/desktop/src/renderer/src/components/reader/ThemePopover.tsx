import { useEffect, useRef } from "react";

import {
  FONT_FAMILIES,
  FONT_SIZE_MAX,
  FONT_SIZE_MIN,
  LINE_SPACING_MAX,
  LINE_SPACING_MIN,
  READING_THEMES,
  type UseReadingThemeResult,
} from "../../lib/readingTheme";

interface ThemePopoverProps extends UseReadingThemeResult {
  onClose: () => void;
}

const FONT_STEP = 1;
const LINE_STEP = 0.1;

function Stepper({
  label,
  value,
  onDec,
  onInc,
  canDec,
  canInc,
}: {
  label: string;
  value: string;
  onDec: () => void;
  onInc: () => void;
  canDec: boolean;
  canInc: boolean;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[13px] text-white/75">{label}</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-label={`Decrease ${label.toLowerCase()}`}
          disabled={!canDec}
          onClick={onDec}
          className="flex h-7 w-7 items-center justify-center rounded-lg bg-white/10 text-white/85 transition hover:bg-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:opacity-30"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
            <path d="M5 12h14" />
          </svg>
        </button>
        <span className="w-12 text-center font-sans text-[12px] tabular-nums text-white/60">{value}</span>
        <button
          type="button"
          aria-label={`Increase ${label.toLowerCase()}`}
          disabled={!canInc}
          onClick={onInc}
          className="flex h-7 w-7 items-center justify-center rounded-lg bg-white/10 text-white/85 transition hover:bg-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:opacity-30"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
      </div>
    </div>
  );
}

/**
 * The Apple Books "Themes & Settings" popover, opened from AA. A sectioned,
 * frosted macOS card: six theme swatches that live-restyle the reading pane, and
 * a Customize block (font size, family, line spacing, brightness). Closes on
 * Escape or an outside click; all controls are keyboard-focusable.
 */
export function ThemePopover({
  settings,
  setTheme,
  setFontSize,
  setFontFamily,
  setLineSpacing,
  setBrightness,
  onClose,
}: ThemePopoverProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    const onPointer = (event: MouseEvent): void => {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener("keydown", onKey);
    // Defer so the click that opened the popover doesn't immediately close it.
    const id = window.setTimeout(() => document.addEventListener("mousedown", onPointer), 0);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
      window.clearTimeout(id);
    };
  }, [onClose]);

  const brightnessPct = Math.round(settings.brightness * 100);

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Themes and settings"
      className="popover no-drag absolute right-0 top-[calc(100%+12px)] z-50 w-[320px] origin-top p-4 text-white"
    >
      <span className="popover-arrow right-6 -top-[9px]" aria-hidden />

      {/* Themes */}
      <p className="px-1 pb-2.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-white/45">
        Themes
      </p>
      <div className="grid grid-cols-3 gap-2.5">
        {READING_THEMES.map((theme) => {
          const active = theme.id === settings.themeId;
          return (
            <button
              key={theme.id}
              type="button"
              onClick={() => setTheme(theme.id)}
              aria-pressed={active}
              className="group flex flex-col items-center gap-1.5 rounded-xl p-1.5 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
            >
              <span
                className={`relative flex h-11 w-full items-center justify-center overflow-hidden rounded-[10px] border transition ${
                  active
                    ? "border-ember-glow ring-2 ring-ember-glow/70"
                    : "border-white/15 group-hover:border-white/35"
                }`}
                style={{ background: theme.swatch[0] }}
              >
                <span className="font-display text-[15px] font-semibold" style={{ color: theme.swatch[1] }}>
                  Aa
                </span>
              </span>
              <span className={`text-[11px] ${active ? "text-white" : "text-white/55"}`}>
                {theme.label}
              </span>
            </button>
          );
        })}
      </div>

      <div className="my-3.5 h-px bg-white/10" />

      {/* Customize */}
      <p className="px-1 pb-3 text-[11px] font-semibold uppercase tracking-[0.16em] text-white/45">
        Customize
      </p>
      <div className="space-y-3.5 px-1">
        <Stepper
          label="Font size"
          value={`${settings.fontSize}px`}
          canDec={settings.fontSize > FONT_SIZE_MIN}
          canInc={settings.fontSize < FONT_SIZE_MAX}
          onDec={() => setFontSize(settings.fontSize - FONT_STEP)}
          onInc={() => setFontSize(settings.fontSize + FONT_STEP)}
        />

        <div>
          <p className="mb-2 text-[13px] text-white/75">Font</p>
          <div className="grid grid-cols-2 gap-2">
            {FONT_FAMILIES.map((font) => {
              const active = font.id === settings.fontFamily;
              return (
                <button
                  key={font.id}
                  type="button"
                  onClick={() => setFontFamily(font.id)}
                  aria-pressed={active}
                  style={{ fontFamily: font.stack }}
                  className={`rounded-lg px-3 py-2 text-[13px] transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
                    active
                      ? "bg-white text-walnut-deep"
                      : "bg-white/8 text-white/80 hover:bg-white/16"
                  }`}
                >
                  {font.label}
                </button>
              );
            })}
          </div>
        </div>

        <Stepper
          label="Line spacing"
          value={`${settings.lineSpacing.toFixed(1)}×`}
          canDec={settings.lineSpacing > LINE_SPACING_MIN + 0.001}
          canInc={settings.lineSpacing < LINE_SPACING_MAX - 0.001}
          onDec={() => setLineSpacing(settings.lineSpacing - LINE_STEP)}
          onInc={() => setLineSpacing(settings.lineSpacing + LINE_STEP)}
        />

        <div>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[13px] text-white/75">Brightness</span>
            <span className="font-sans text-[12px] tabular-nums text-white/45">{brightnessPct}%</span>
          </div>
          <div className="flex items-center gap-2.5">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="3.2" />
              <path d="M12 4.5v1.6M12 17.9v1.6M19.5 12h-1.6M6.1 12H4.5" />
            </svg>
            <input
              type="range"
              min={70}
              max={100}
              value={brightnessPct}
              aria-label="Page brightness"
              onChange={(event) => setBrightness(Number(event.target.value) / 100)}
              className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-white/15 accent-ember-glow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/60"
            />
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.7)" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="4.4" />
              <path d="M12 2v2.4M12 19.6V22M22 12h-2.4M4.4 12H2M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7M19.1 19.1l-1.7-1.7M6.6 6.6 4.9 4.9" />
            </svg>
          </div>
        </div>
      </div>
    </div>
  );
}
