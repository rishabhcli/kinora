import { useState, useEffect } from "react";
import logoImg from "../assets/logo-transparent.png";
import GooeySearch from "./GooeySearch";

/* ===== Kinora Logo ===== */
const BookLogoIcon = ({ size = 22 }: { size?: number }) => (
  <img src={logoImg} alt="Kinora" width={size} height={size} style={{ objectFit: "contain" }} />
);

const HomeIcon = ({ size = 17 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 10.5L12 3l9 7.5" />
    <path d="M5 9.5V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.5" />
    <path d="M9.5 21v-6h5v6" />
  </svg>
);

const LibraryIcon = ({ size = 17 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="4" height="16" rx="0.5" />
    <rect x="9" y="4" width="4" height="16" rx="0.5" />
    <path d="M16 4l4 1.2L18 20l-4-1.2z" />
  </svg>
);

const WatchIcon = ({ size = 17 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M10 8.5l5 3.5-5 3.5z" fill="currentColor" stroke="none" />
  </svg>
);

const HeartIcon = ({ size = 17 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20.5C12 20.5 3.5 15.5 3.5 9.5C3.5 6.5 5.8 4.5 8.5 4.5C10.2 4.5 11.5 5.5 12 6.5C12.5 5.5 13.8 4.5 15.5 4.5C18.2 4.5 20.5 6.5 20.5 9.5C20.5 15.5 12 20.5 12 20.5z" />
  </svg>
);

const NotesIcon = ({ size = 17 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <path d="M5 4.5C5 3.67 5.67 3 6.5 3H16l3 3v13.5c0 .83-.67 1.5-1.5 1.5h-11c-.83 0-1.5-.67-1.5-1.5z" />
    <path d="M16 3v3h3" />
    <path d="M8 10h8M8 13h8M8 16h5" strokeWidth={1.4} />
  </svg>
);

const SearchIcon = ({ size = 15 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="7" />
    <path d="M16.5 16.5L21 21" />
  </svg>
);

export const GeometricAvatar = ({ size = 34 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 40 40" fill="none">
    <defs>
      <linearGradient id="avatarGrad" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stopColor="#c8c4be" />
        <stop offset="100%" stopColor="#7a7570" />
      </linearGradient>
    </defs>
    <circle cx="20" cy="20" r="19" fill="url(#avatarGrad)" stroke="rgba(255,255,255,0.2)" strokeWidth="0.5" />
    <path d="M20 7L33 20L20 33L7 20z" fill="rgba(255,255,255,0.1)" stroke="rgba(255,255,255,0.15)" strokeWidth="0.5" />
    <circle cx="20" cy="16" r="4" fill="rgba(255,255,255,0.6)" />
    <path d="M12 28c0-4.5 3.5-7 8-7s8 2.5 8 7" fill="rgba(255,255,255,0.4)" />
  </svg>
);

export const navItems = [
  { icon: HomeIcon, label: "Home" },
  { icon: LibraryIcon, label: "Library" },
  { icon: WatchIcon, label: "Watch" },
  { icon: HeartIcon, label: "Favorites" },
  { icon: NotesIcon, label: "Notes" },
];

export default function Navbar({ active, onNavigate }: { active: string; onNavigate: (page: string) => void }) {
  const [profileOpen, setProfileOpen] = useState(false);
  const [scrollState, setScrollState] = useState<"top" | "middle" | "bottom">("top");

  useEffect(() => {
    const onScroll = () => {
      const y = window.scrollY;
      const scrollBottom = window.innerHeight + y;
      const docHeight = document.documentElement.scrollHeight;
      if (y < 10) setScrollState("top");
      else if (scrollBottom >= docHeight - 10) setScrollState("bottom");
      else setScrollState("middle");
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const dockHiddenPages = ["Edit Profile", "Settings", "Pricing"];
  const showDock = !dockHiddenPages.includes(active);
  // Buttons in header when: at top, at bottom, or on pages without dock
  const buttonsInHeader = scrollState !== "middle" || !showDock;
  const dockVisible = scrollState === "middle" && showDock;

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest('[data-profile-dropdown]')) {
        setProfileOpen(false);
      }
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  return (
    <>
      {/* Top bar — clean, no glass */}
      <header className="fixed top-0 left-0 right-0 z-50" data-profile-dropdown style={{
        background: "rgba(15, 14, 12, 0.6)",
        backdropFilter: "blur(12px) saturate(140%)",
        WebkitBackdropFilter: "blur(12px) saturate(140%)",
      }}>
        <div className="px-6 py-2.5 flex items-center justify-between max-w-[1280px] mx-auto">
          {/* Left: Logo */}
          <div className="flex items-center gap-2 cursor-pointer" onClick={() => onNavigate("Home")}>
            <BookLogoIcon size={36} />
            <span className="font-serif text-base font-semibold text-kinora-text tracking-wide">
              Kinora
            </span>
          </div>

          {/* Center: Nav buttons — visible when at top of page */}
          <nav
            className="flex items-center"
            style={{
              gap: 2,
              opacity: buttonsInHeader ? 1 : 0,
              transition: "opacity 0.25s ease",
              pointerEvents: buttonsInHeader ? "auto" : "none",
            }}
          >
            {navItems.map((item) => (
              <button
                key={item.label}
                onClick={() => onNavigate(item.label)}
                className={`flex items-center gap-1.5 font-medium transition-colors ${
                  active === item.label
                    ? "nav-btn-active text-white"
                    : "nav-btn-hover text-white/70 hover:text-white"
                }`}
                style={{
                  padding: "6px 12px",
                  borderRadius: "999px",
                  fontSize: 11,
                  transitionTimingFunction: "cubic-bezier(0.34, 1.56, 0.64, 1)",
                  transitionDuration: "0.35s",
                  textShadow: active === item.label
                    ? "0 0 12px rgba(255,255,255,0.4)"
                    : "0 0 8px rgba(255,255,255,0.15)",
                }}
              >
                <item.icon size={15} />
                <span>{item.label}</span>
              </button>
            ))}
          </nav>

          {/* Right: Search + Profile */}
          <div className="flex items-center gap-2.5">
            <GooeySearch />
            <button
              onClick={() => setProfileOpen(!profileOpen)}
              aria-label="Open profile menu"
              className="w-7 h-7 rounded-full overflow-hidden border border-white/10 flex items-center justify-center transition-transform hover:scale-105"
            >
              <GeometricAvatar size={28} />
            </button>
          </div>
        </div>

        {/* Profile dropdown */}
        {profileOpen && (
          <div
            className="absolute top-12 right-6 w-60 rounded-2xl overflow-hidden z-[60]"
            style={{
              background: "rgba(22, 20, 18, 0.98)",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              boxShadow: "0 12px 40px rgba(0, 0, 0, 0.5)",
              animation: "dropdownIn 0.2s cubic-bezier(0.16, 1, 0.3, 1)",
            }}
          >
            {/* Header */}
            <div
              className="flex items-center gap-3 px-4 py-3"
              style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.06)" }}
            >
              <GeometricAvatar size={32} />
              <div className="flex-1 min-w-0">
                <p className="text-[13px] font-semibold text-kinora-text truncate">User</p>
                <p className="text-[10px] text-kinora-muted truncate">user@kinora.app</p>
              </div>
            </div>

            {/* Menu items */}
            <div className="py-1">
              <button
                onClick={() => { onNavigate("Edit Profile"); setProfileOpen(false); }}
                className="w-full flex items-center gap-3 px-4 py-2 text-[12px] text-kinora-muted hover:text-kinora-text hover:bg-white/[0.03] transition-colors"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 12.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z" />
                  <path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6" />
                </svg>
                <span className="flex-1 text-left">Edit Profile</span>
              </button>
              <button
                onClick={() => { onNavigate("Settings"); setProfileOpen(false); }}
                className="w-full flex items-center gap-3 px-4 py-2 text-[12px] text-kinora-muted hover:text-kinora-text hover:bg-white/[0.03] transition-colors"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="3" />
                  <path d="M12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" />
                </svg>
                <span className="flex-1 text-left">Settings</span>
              </button>
              <button
                onClick={() => { onNavigate("Pricing"); setProfileOpen(false); }}
                className="w-full flex items-center gap-3 px-4 py-2 text-[12px] text-kinora-muted hover:text-kinora-text hover:bg-white/[0.03] transition-colors"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
                </svg>
                <span className="flex-1 text-left">Pricing</span>
              </button>
            </div>

            {/* Divider + Log Out */}
            <div className="h-px mx-3" style={{ background: "rgba(255, 255, 255, 0.06)" }} />
            <div className="py-1">
              <button className="w-full flex items-center gap-3 px-4 py-2 text-[12px] text-red-400/70 hover:text-red-400 hover:bg-white/[0.03] transition-colors">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                  <path d="M16 17l5-5-5-5M21 12H9" />
                </svg>
                <span className="flex-1 text-left">Log Out</span>
              </button>
            </div>
          </div>
        )}
      </header>

      {/* Floating dock — appears at bottom when scrolling through middle of page */}
      {showDock && (
      <div
        className="fixed z-50 liquid-glass-dock"
        style={{
          position: "fixed",
          bottom: 28,
          left: "50%",
          transform: `translateX(-50%) translateY(${dockVisible ? 0 : 120}px)`,
          borderRadius: "999px",
          padding: "5px 7px",
          opacity: dockVisible ? 1 : 0,
          transition: "transform 0.35s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.3s ease",
          pointerEvents: dockVisible ? "auto" : "none",
        }}
      >
        <nav className="flex items-center" style={{ gap: 2 }}>
          {navItems.map((item) => (
            <button
              key={item.label}
              onClick={() => onNavigate(item.label)}
              className={`flex items-center gap-1.5 font-medium transition-colors ${
                active === item.label
                  ? "nav-btn-active text-white"
                  : "nav-btn-hover text-white/70 hover:text-white"
              }`}
              style={{
                padding: "7px 12px",
                borderRadius: "999px",
                fontSize: 11,
                transitionTimingFunction: "cubic-bezier(0.34, 1.56, 0.64, 1)",
                transitionDuration: "0.35s",
                textShadow: active === item.label
                  ? "0 0 12px rgba(255,255,255,0.4)"
                  : "0 0 8px rgba(255,255,255,0.15)",
              }}
            >
              <item.icon size={15} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
      </div>
      )}
    </>
  );
}
