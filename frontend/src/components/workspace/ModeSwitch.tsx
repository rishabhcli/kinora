import type { SessionMode } from "../../api/types";
import { EyeIcon, WandIcon } from "../common/icons";

interface ModeSwitchProps {
  mode: SessionMode;
  onChange: (mode: SessionMode) => void;
}

const MODES: { id: SessionMode; label: string; Icon: typeof EyeIcon }[] = [
  { id: "viewer", label: "Viewer", Icon: EyeIcon },
  { id: "director", label: "Director", Icon: WandIcon },
];

/** Liquid-glass segmented control (the Loom-style switch, kinora.md §5.2). */
export function ModeSwitch({ mode, onChange }: ModeSwitchProps) {
  return (
    <div
      role="tablist"
      aria-label="Right pane mode"
      className="glass-segment relative grid grid-cols-2 gap-1 rounded-full p-1"
    >
      <span
        aria-hidden="true"
        className="absolute inset-y-1 w-[calc(50%-0.25rem)] rounded-full bg-kinora-glow shadow-[0_4px_20px_-4px_rgba(124,92,255,0.7)] transition-transform duration-300 ease-out"
        style={{ transform: mode === "director" ? "translateX(calc(100% + 0.5rem))" : "translateX(0)" }}
      />
      {MODES.map(({ id, label, Icon }) => (
        <button
          key={id}
          role="tab"
          aria-selected={mode === id}
          type="button"
          onClick={() => onChange(id)}
          className={`relative z-10 inline-flex items-center justify-center gap-2 rounded-full px-4 py-1.5 text-sm font-medium transition-colors ${
            mode === id ? "text-white" : "text-kinora-muted hover:text-kinora-mist"
          }`}
        >
          <Icon className="h-4 w-4" />
          {label}
        </button>
      ))}
    </div>
  );
}
