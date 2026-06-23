import { parsePaletteValue } from "@kinora/core";

interface StyleTokensEditorProps {
  palette: string[];
  lens: string;
  artDirection: string;
  onChange: (next: { palette?: string[]; lens?: string; artDirection?: string }) => void;
}

function Label({ children }: { children: string }) {
  return (
    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-white/40">
      {children}
    </p>
  );
}

/**
 * Retune a Style node's tokens (§5.4 / §8.1): the palette, lens, and
 * art-direction every shot in the scene is conditioned on. Editing any of them
 * bumps the Style entity's version, so only the shots that cite it re-render.
 */
export function StyleTokensEditor({ palette, lens, artDirection, onChange }: StyleTokensEditorProps) {
  return (
    <div className="space-y-3.5">
      <div>
        <Label>Palette</Label>
        <input
          value={palette.join(", ")}
          onChange={(e) => onChange({ palette: parsePaletteValue(e.target.value) })}
          placeholder="#1b2a4a, #c97b4a, ivory"
          spellCheck={false}
          className="glass-input w-full rounded-lg px-3 py-2 text-[13px]"
        />
        {palette.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {palette.map((color, i) => (
              <span
                key={`${color}-${i}`}
                className="flex items-center gap-1.5 rounded-full bg-white/8 py-0.5 pl-1 pr-2"
              >
                <span
                  className="h-4 w-4 rounded-full ring-1 ring-white/25"
                  style={{ background: color }}
                  aria-hidden
                />
                <span className="font-mono text-[10.5px] text-white/70">{color}</span>
              </span>
            ))}
          </div>
        )}
      </div>

      <div>
        <Label>Lens</Label>
        <input
          value={lens}
          onChange={(e) => onChange({ lens: e.target.value })}
          placeholder="35mm anamorphic, shallow depth"
          className="glass-input w-full rounded-lg px-3 py-2 text-[13px]"
        />
      </div>

      <div>
        <Label>Art direction</Label>
        <textarea
          value={artDirection}
          onChange={(e) => onChange({ artDirection: e.target.value })}
          placeholder="painterly storybook, warm key light, soft grain"
          rows={2}
          className="glass-input w-full resize-y rounded-lg px-3 py-2 text-[13px] leading-snug"
        />
      </div>
    </div>
  );
}
