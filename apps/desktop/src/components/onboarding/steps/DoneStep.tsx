// Onboarding · Done — a short celebratory close. Presentational.
import { Clapperboard } from "lucide-react";

export function DoneStep() {
  return (
    <div style={{ textAlign: "center", padding: "8px 0 4px" }}>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: 64,
          height: 64,
          borderRadius: 18,
          background: "rgba(212,164,78,0.16)",
          color: "var(--auth-gold-bright)",
          marginBottom: 6,
        }}
      >
        <Clapperboard size={30} strokeWidth={1.6} />
      </span>
      <p className="acct-card-desc" style={{ fontSize: 13.5 }}>
        Your library is ready. Open a book and watch it come to life.
      </p>
    </div>
  );
}
