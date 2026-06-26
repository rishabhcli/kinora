import { useState } from "react";
import { Icon, ICON_NAMES, type IconName, type SymbolWeight } from "./index";

// A dev tool / symbol picker: every glyph in the registry, plus weight, size and
// rendering-mode demos. Not shipped in the production build (dev-only entry).
const WEIGHTS: SymbolWeight[] = ["ultralight", "light", "regular", "medium", "semibold", "bold"];

export default function IconGallery() {
  const [copied, setCopied] = useState<IconName | null>(null);
  const copy = (name: IconName) => {
    try {
      navigator.clipboard?.writeText(`<Icon name="${name}" />`);
      setCopied(name);
      window.setTimeout(() => setCopied(null), 1200);
    } catch {
      /* clipboard blocked */
    }
  };

  return (
    <div className="min-h-screen text-kinora-text" style={{ background: "#15120e" }}>
      <div className="max-w-[1100px] mx-auto px-8 py-10">
        <header className="mb-8">
          <h1 className="font-serif text-3xl font-semibold">Kinora SF-Symbols icon set</h1>
          <p className="text-kinora-muted text-sm mt-1">
            {ICON_NAMES.length} glyphs · one <code className="text-kinora-gold">&lt;Icon&gt;</code> API · currentColor-driven
          </p>
        </header>

        {/* Weight demo */}
        <section className="mb-8 p-5 rounded-2xl" style={{ background: "rgba(255,255,255,0.04)", border: "0.5px solid rgba(255,255,255,0.08)" }}>
          <p className="text-[11px] uppercase tracking-wide text-kinora-subtle mb-3">Weight · gearshape</p>
          <div className="flex items-end gap-7">
            {WEIGHTS.map((w) => (
              <div key={w} className="flex flex-col items-center gap-2">
                <Icon name="gearshape" size={32} weight={w} />
                <span className="text-[10px] text-kinora-muted">{w}</span>
              </div>
            ))}
          </div>
        </section>

        {/* Size + mode demo */}
        <section className="mb-8 grid grid-cols-2 gap-4">
          <div className="p-5 rounded-2xl" style={{ background: "rgba(255,255,255,0.04)", border: "0.5px solid rgba(255,255,255,0.08)" }}>
            <p className="text-[11px] uppercase tracking-wide text-kinora-subtle mb-3">Crisp at every size · book.fill</p>
            <div className="flex items-end gap-5 text-kinora-gold">
              {[16, 20, 24, 32, 48].map((s) => (
                <div key={s} className="flex flex-col items-center gap-2">
                  <Icon name="book.fill" size={s} />
                  <span className="text-[10px] text-kinora-muted">{s}px</span>
                </div>
              ))}
            </div>
          </div>
          <div className="p-5 rounded-2xl" style={{ background: "rgba(255,255,255,0.04)", border: "0.5px solid rgba(255,255,255,0.08)" }}>
            <p className="text-[11px] uppercase tracking-wide text-kinora-subtle mb-3">Hierarchical rendering</p>
            <div className="flex items-end gap-7">
              {(["moon.stars", "sparkles", "person.crop.circle"] as IconName[]).map((n) => (
                <div key={n} className="flex flex-col items-center gap-2 text-kinora-gold-light">
                  <Icon name={n} size={32} mode="hierarchical" />
                  <span className="text-[10px] text-kinora-muted">{n}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Full registry */}
        <section
          className="grid gap-2"
          style={{ gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))" }}
        >
          {ICON_NAMES.map((name) => (
            <button
              key={name}
              onClick={() => copy(name)}
              title={`Copy <Icon name="${name}" />`}
              className="flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-left hover:bg-white/[0.05] transition-colors"
              style={{ border: "0.5px solid rgba(255,255,255,0.06)" }}
            >
              <Icon name={name} size={20} />
              <span className="text-[11px] text-kinora-muted truncate">{copied === name ? "copied!" : name}</span>
            </button>
          ))}
        </section>
      </div>
    </div>
  );
}
