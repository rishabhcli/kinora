// Onboarding · Welcome — a short value-prop intro. Purely presentational.
import { Film, BookOpen, Wand2 } from "lucide-react";

export function WelcomeStep() {
  const points = [
    { icon: BookOpen, text: "Open any book or PDF." },
    { icon: Wand2, text: "Six AI agents storyboard it as you read." },
    { icon: Film, text: "Watch it become a film, page by page." },
  ];
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 14 }}>
      {points.map((p) => {
        const Icon = p.icon;
        return (
          <li key={p.text} style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: 36,
                height: 36,
                borderRadius: 10,
                background: "rgba(212,164,78,0.14)",
                color: "var(--auth-gold-bright)",
                flex: "0 0 auto",
              }}
            >
              <Icon size={18} strokeWidth={1.7} />
            </span>
            <span style={{ fontSize: 14, color: "var(--auth-text)" }}>{p.text}</span>
          </li>
        );
      })}
    </ul>
  );
}
