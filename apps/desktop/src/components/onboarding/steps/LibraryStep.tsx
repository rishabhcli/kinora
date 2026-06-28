// Onboarding · Library — orient the reader on adding a first book vs exploring
// the demo. Purely presentational hints; the actual upload/library lives in the
// library domain (another agent), so this step just sets expectations.
import { Upload, Sparkles } from "lucide-react";

export function LibraryStep() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="acct-card" style={{ margin: 0, display: "flex", gap: 12, alignItems: "flex-start" }}>
        <Upload size={20} strokeWidth={1.7} style={{ color: "var(--auth-gold-bright)", flex: "0 0 auto", marginTop: 2 }} />
        <div>
          <div className="acct-card-title">Add your own</div>
          <p className="acct-card-desc">Drop in a PDF or EPUB. We'll prepare it — no video yet, just analysis.</p>
        </div>
      </div>
      <div className="acct-card" style={{ margin: 0, display: "flex", gap: 12, alignItems: "flex-start" }}>
        <Sparkles size={20} strokeWidth={1.7} style={{ color: "var(--auth-gold-bright)", flex: "0 0 auto", marginTop: 2 }} />
        <div>
          <div className="acct-card-title">Explore the demo</div>
          <p className="acct-card-desc">A prepared title is waiting — open it to see a film generate as you read.</p>
        </div>
      </div>
    </div>
  );
}
