import { useEffect, useId, useState, type CSSProperties, type ReactNode } from "react";
import {
  READING_THEMES,
  READING_SPACINGS,
  READING_FONTS,
  READING_BOUNDS,
  type ReadingPrefs,
  type ReadingTheme,
  type ReadingFontFamily,
  type ReadingSpacing,
} from "@/a11y/readingPrefs";

// The Apple-Books reading-controls panel. Controlled: the host (ReadingRoom via
// useReadingPrefs) passes `prefs` + `onChange`. App-wide display a11y toggles
// live in Settings; every control here is book-text/read-aloud specific.

const s: Record<string, CSSProperties> = {
  panel: { display: "flex", flexDirection: "column", gap: "1.1rem", minWidth: 280, color: "inherit" },
  section: { border: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: "0.5rem" },
  legend: {
    padding: 0,
    fontSize: "0.7rem",
    textTransform: "uppercase",
    letterSpacing: "0.08em",
    opacity: 0.6,
  },
  row: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem" },
  rowLabel: { fontSize: "0.9rem" },
  segments: { display: "flex", gap: "0.35rem", flexWrap: "wrap" },
  value: { fontSize: "0.8rem", opacity: 0.7, minWidth: 44, textAlign: "right", fontVariantNumeric: "tabular-nums" },
  range: { flex: 1, accentColor: "var(--kinora-a11y-focus, #f4c97a)" },
  swatch: { width: 22, height: 22, borderRadius: "50%", border: "1px solid rgba(255,255,255,0.25)", display: "inline-block" },
};

/** Visually-styled segmented radio group backed by real radio inputs. */
function SegmentedRadio<T extends string>(props: {
  legend: string;
  value: T;
  options: { value: T; label: string; swatch?: string }[];
  onChange: (v: T) => void;
}) {
  const name = useId();
  return (
    <fieldset style={s.section}>
      <legend style={s.legend}>{props.legend}</legend>
      <div style={s.segments} role="presentation">
        {props.options.map((o) => {
          const id = `${name}-${o.value}`;
          const selected = props.value === o.value;
          return (
            <span key={o.value} style={{ position: "relative" }}>
              <input
                type="radio"
                id={id}
                name={name}
                className="sr-only"
                checked={selected}
                onChange={() => props.onChange(o.value)}
              />
              <label
                htmlFor={id}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.4rem",
                  padding: "0.35rem 0.7rem",
                  borderRadius: 10,
                  cursor: "pointer",
                  fontSize: "0.85rem",
                  border: `1px solid ${selected ? "var(--kinora-a11y-focus, #f4c97a)" : "rgba(255,255,255,0.16)"}`,
                  background: selected ? "rgba(244,201,122,0.14)" : "transparent",
                }}
              >
                {o.swatch && <span aria-hidden="true" style={{ ...s.swatch, background: o.swatch }} />}
                {o.label}
              </label>
            </span>
          );
        })}
      </div>
    </fieldset>
  );
}

/** Accessible range slider with a visible, SR-suppressed value read-out. */
function Slider(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format: (v: number) => string;
  onChange: (v: number) => void;
}) {
  const id = useId();
  return (
    <div style={s.row}>
      <label htmlFor={id} style={s.rowLabel}>
        {props.label}
      </label>
      <input
        id={id}
        type="range"
        style={s.range}
        min={props.min}
        max={props.max}
        step={props.step}
        value={props.value}
        aria-valuetext={props.format(props.value)}
        onChange={(e) => props.onChange(parseFloat(e.target.value))}
      />
      <span style={s.value} aria-hidden="true">
        {props.format(props.value)}
      </span>
    </div>
  );
}

/** Switch backed by a real checkbox (role="switch" announces correctly). */
function Switch(props: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void }) {
  const id = useId();
  return (
    <div style={s.row}>
      <label htmlFor={id} style={s.rowLabel}>
        {props.label}
      </label>
      <input
        id={id}
        type="checkbox"
        role="switch"
        checked={props.checked}
        onChange={(e) => props.onChange(e.target.checked)}
      />
    </div>
  );
}

function useVoices(provided?: SpeechSynthesisVoice[]): SpeechSynthesisVoice[] {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>(provided ?? []);
  useEffect(() => {
    if (provided) return;
    if (typeof window === "undefined" || !window.speechSynthesis) return;
    const read = () => setVoices(window.speechSynthesis.getVoices());
    read();
    window.speechSynthesis.addEventListener?.("voiceschanged", read);
    return () => window.speechSynthesis.removeEventListener?.("voiceschanged", read);
  }, [provided]);
  return voices;
}

export interface ReadingControlsProps {
  prefs: ReadingPrefs;
  onChange: (partial: Partial<ReadingPrefs>) => void;
  voices?: SpeechSynthesisVoice[];
}

export function ReadingControls({ prefs, onChange, voices: providedVoices }: ReadingControlsProps) {
  const voices = useVoices(providedVoices);
  const voiceId = useId();

  const themeOptions = (Object.keys(READING_THEMES) as ReadingTheme[]).map((t) => ({
    value: t,
    label: READING_THEMES[t].label,
    swatch: READING_THEMES[t].swatch,
  }));
  const fontOptions = (Object.keys(READING_FONTS) as ReadingFontFamily[]).map((f) => ({
    value: f,
    label: READING_FONTS[f].label,
  }));
  const spacingOptions = (Object.keys(READING_SPACINGS) as ReadingSpacing[]).map((sp) => ({
    value: sp,
    label: READING_SPACINGS[sp].label,
  }));

  const b = READING_BOUNDS;
  const pct = (v: number) => `${Math.round(v * 100)}%`;

  return (
    <div role="group" aria-label="Reading settings" style={s.panel}>
      <SegmentedRadio legend="Theme" value={prefs.theme} options={themeOptions} onChange={(theme) => onChange({ theme })} />
      <Switch label="Auto Night (dim after dark)" checked={prefs.autoNight} onChange={(autoNight) => onChange({ autoNight })} />

      <SegmentedRadio legend="Font" value={prefs.fontFamily} options={fontOptions} onChange={(fontFamily) => onChange({ fontFamily })} />

      <fieldset style={s.section}>
        <legend style={s.legend}>Text</legend>
        <Slider label="Text size" value={prefs.fontScale} min={b.fontScale.min} max={b.fontScale.max} step={b.fontScale.step} format={pct} onChange={(fontScale) => onChange({ fontScale })} />
        <Slider label="Line spacing" value={prefs.leading} min={b.leading.min} max={b.leading.max} step={b.leading.step} format={(v) => v.toFixed(1)} onChange={(leading) => onChange({ leading })} />
        <Slider label="Line width" value={prefs.measure} min={b.measure.min} max={b.measure.max} step={b.measure.step} format={(v) => `${Math.round(v)} ch`} onChange={(measure) => onChange({ measure })} />
      </fieldset>

      <SegmentedRadio legend="Letter & word spacing" value={prefs.spacing} options={spacingOptions} onChange={(spacing) => onChange({ spacing })} />

      <fieldset style={s.section}>
        <legend style={s.legend}>Display</legend>
        <Slider label="Brightness" value={prefs.brightness} min={b.brightness.min} max={b.brightness.max} step={b.brightness.step} format={pct} onChange={(brightness) => onChange({ brightness })} />
      </fieldset>

      <SegmentedRadio
        legend="Reading mode"
        value={prefs.readingMode}
        options={[
          { value: "scroll", label: "Scroll" },
          { value: "paged", label: "Paged" },
        ]}
        onChange={(readingMode) => onChange({ readingMode })}
      />

      <fieldset style={s.section}>
        <legend style={s.legend}>Read aloud</legend>
        <div style={s.row}>
          <label htmlFor={voiceId} style={s.rowLabel}>
            Voice
          </label>
          <select
            id={voiceId}
            value={prefs.ttsVoiceURI ?? ""}
            onChange={(e) => onChange({ ttsVoiceURI: e.target.value || null })}
            style={{
              maxWidth: 160,
              // Explicit colours so light text never lands on the native white
              // default (fails contrast); matches the dark panel.
              background: "#1f1b16",
              color: "#e8e2d8",
              border: "1px solid rgba(255,255,255,0.18)",
              borderRadius: 8,
              padding: "0.3rem 0.45rem",
            }}
          >
            <option value="">System default</option>
            {voices.map((v) => (
              <option key={v.voiceURI} value={v.voiceURI}>
                {v.name}
              </option>
            ))}
          </select>
        </div>
        <Slider label="Read-aloud speed" value={prefs.ttsRate} min={b.ttsRate.min} max={b.ttsRate.max} step={b.ttsRate.step} format={(v) => `${v.toFixed(1)}×`} onChange={(ttsRate) => onChange({ ttsRate })} />
      </fieldset>
    </div>
  );
}
