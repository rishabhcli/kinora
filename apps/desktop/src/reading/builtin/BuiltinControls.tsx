// Built-in stand-in for Agent 6's <ReadingControls prefs onChange /> (slot in
// slots.ts). Controlled by the shell's single useReadingPrefs() instance.
// Agent 12 swaps in the real component at integration.
import { useState } from "react";
import { READING_THEMES, READING_SPACINGS, clampPref, type ReadingTheme } from "../../lib/readingPrefs";
import type { ReadingControlsProps } from "../slots";

export function BuiltinReadingControls({ prefs, onChange }: ReadingControlsProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="Reading settings"
        aria-expanded={open}
        className="glass-control flex items-baseline gap-0.5 rounded-lg px-2.5 py-1.5 font-serif text-kinora-text"
      >
        <span className="text-[11px]">A</span>
        <span className="text-[15px] leading-none">A</span>
      </button>
      {open && (
        <div className="glass-card absolute right-0 top-11 z-30 w-60 rounded-xl p-3.5" style={{ background: "rgba(20,18,16,0.97)" }}>
          <p className="mb-1.5 text-[10px] uppercase tracking-wider text-kinora-muted">Theme</p>
          <div className="mb-3 flex gap-2.5">
            {(Object.keys(READING_THEMES) as ReadingTheme[]).map((t) => (
              <button
                key={t}
                onClick={() => onChange({ theme: t })}
                title={READING_THEMES[t].label}
                aria-label={READING_THEMES[t].label}
                className="h-7 w-7 rounded-full transition-transform hover:scale-110"
                style={{
                  background: READING_THEMES[t].swatch,
                  border: `2px solid ${prefs.theme === t ? "rgba(212,164,78,0.95)" : "rgba(255,255,255,0.18)"}`,
                }}
              />
            ))}
          </div>
          <label className="mb-3 flex items-center justify-between text-[11px] text-kinora-text/85">
            <span>Auto night</span>
            <input type="checkbox" checked={prefs.autoNight} onChange={(e) => onChange({ autoNight: e.target.checked })} />
          </label>
          <div style={{ height: 1, background: "rgba(255,255,255,0.08)", marginBottom: 10 }} />
          <PrefStepper
            label="Text size"
            value={`${Math.round(prefs.fontScale * 100)}%`}
            onMinus={() => onChange({ fontScale: clampPref(+(prefs.fontScale - 0.1).toFixed(2), 0.85, 1.5) })}
            onPlus={() => onChange({ fontScale: clampPref(+(prefs.fontScale + 0.1).toFixed(2), 0.85, 1.5) })}
          />
          <PrefStepper
            label="Line spacing"
            value={prefs.leading.toFixed(1)}
            onMinus={() => onChange({ leading: clampPref(+(prefs.leading - 0.1).toFixed(1), 1.4, 2.2) })}
            onPlus={() => onChange({ leading: clampPref(+(prefs.leading + 0.1).toFixed(1), 1.4, 2.2) })}
          />
          <PrefStepper
            label="Width"
            value={`${prefs.measure}ch`}
            onMinus={() => onChange({ measure: clampPref(prefs.measure - 4, 48, 80) })}
            onPlus={() => onChange({ measure: clampPref(prefs.measure + 4, 48, 80) })}
          />
          <button
            onClick={() =>
              onChange({ spacing: prefs.spacing === "normal" ? "relaxed" : prefs.spacing === "relaxed" ? "loose" : "normal" })
            }
            className="mt-1 flex w-full items-center justify-between rounded-md px-2 py-1.5 text-[11px] text-kinora-text/85"
            style={{ background: "rgba(255,255,255,0.06)" }}
          >
            <span>Letter spacing</span>
            <span className="text-kinora-muted">{READING_SPACINGS[prefs.spacing].label}</span>
          </button>
        </div>
      )}
    </div>
  );
}

/** A compact −/value/+ row for the reading-settings popover. */
function PrefStepper({
  label,
  value,
  onMinus,
  onPlus,
}: {
  label: string;
  value: string;
  onMinus: () => void;
  onPlus: () => void;
}) {
  const btn = "grid h-6 w-6 place-items-center rounded-md text-kinora-text";
  return (
    <div className="mb-2 flex items-center justify-between">
      <span className="text-[11px] text-kinora-text/85">{label}</span>
      <div className="flex items-center gap-2">
        <button onClick={onMinus} aria-label={`Decrease ${label}`} className={btn} style={{ background: "rgba(255,255,255,0.08)" }}>
          −
        </button>
        <span className="w-11 text-center text-[11px] text-kinora-muted">{value}</span>
        <button onClick={onPlus} aria-label={`Increase ${label}`} className={btn} style={{ background: "rgba(255,255,255,0.08)" }}>
          +
        </button>
      </div>
    </div>
  );
}
