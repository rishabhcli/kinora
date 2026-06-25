import { useState } from "react";

export default function SettingsPage() {
  const [prefs, setPrefs] = useState<Record<string, boolean>>({
    autoScroll: true,
    darkMode: true,
    notifications: true,
    weeklyDigest: true,
    analytics: false,
    soundEffects: false,
  });

  const toggle = (key: string) => setPrefs((p) => ({ ...p, [key]: !p[key] }));

  const sections = [
    {
      title: "Reading",
      items: [
        { key: "autoScroll", label: "Auto-scroll", desc: "Smooth scrolling while reading" },
        { key: "darkMode", label: "Dark Mode", desc: "Always use dark theme" },
      ],
    },
    {
      title: "Notifications",
      items: [
        { key: "notifications", label: "Push Notifications", desc: "Reading reminders and updates" },
        { key: "weeklyDigest", label: "Weekly Digest", desc: "Summary of your reading activity" },
      ],
    },
    {
      title: "Privacy",
      items: [
        { key: "analytics", label: "Analytics", desc: "Help improve Kinora with usage data" },
        { key: "soundEffects", label: "Sound Effects", desc: "Page turn and UI sounds" },
      ],
    },
  ];

  return (
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-6 pt-4">
        Settings
      </h1>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {sections.map((section) => (
          <div key={section.title}>
            <p className="text-[11px] font-medium text-kinora-muted mb-3">
              {section.title}
            </p>
            <div className="space-y-4">
              {section.items.map((item) => (
                <button
                  key={item.key}
                  onClick={() => toggle(item.key)}
                  className="w-full flex items-center gap-3"
                >
                  <div className="flex-1 text-left">
                    <p className="text-[13px] font-medium text-kinora-text">{item.label}</p>
                    <p className="text-[11px] text-kinora-muted mt-0.5">{item.desc}</p>
                  </div>
                  <div
                    className="relative rounded-full shrink-0"
                    style={{
                      width: 36,
                      height: 20,
                      background: prefs[item.key] ? "rgba(255, 255, 255, 0.2)" : "rgba(255, 255, 255, 0.08)",
                      transition: "background 0.2s",
                    }}
                  >
                    <div
                      className="absolute top-[2px] rounded-full"
                      style={{
                        width: 16,
                        height: 16,
                        background: "#e8e2d8",
                        transform: prefs[item.key] ? "translateX(18px)" : "translateX(2px)",
                        transition: "transform 0.2s ease-out",
                      }}
                    />
                  </div>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
