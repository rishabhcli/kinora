import { useState, useEffect } from "react";
import { motion } from "framer-motion";
import { useMotion } from "../motion";
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

export const GeometricAvatar = ({ size = 34, ring = false }: { size?: number; ring?: boolean }) => (
  <svg width={size} height={size} viewBox="0 0 40 40" fill="none">
    <defs>
      <linearGradient id="avatarGrad" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stopColor="#d4c5a9" />
        <stop offset="100%" stopColor="#8b7355" />
      </linearGradient>
      {ring && (
        <linearGradient id="avatarRing" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="rgba(212,164,78,0.6)" />
          <stop offset="100%" stopColor="rgba(212,164,78,0.15)" />
        </linearGradient>
      )}
    </defs>
    <circle cx="20" cy="20" r="19" fill="url(#avatarGrad)" stroke={ring ? "url(#avatarRing)" : "rgba(255,255,255,0.12)"} strokeWidth={ring ? 1.5 : 0.5} />
    <path d="M20 7L33 20L20 33L7 20z" fill="rgba(255,255,255,0.08)" stroke="rgba(255,255,255,0.1)" strokeWidth="0.5" />
    <circle cx="20" cy="16" r="4" fill="rgba(255,255,255,0.55)" />
    <path d="M12 28c0-4.5 3.5-7 8-7s8 2.5 8 7" fill="rgba(255,255,255,0.35)" />
  </svg>
);

export const navItems = [
  { icon: HomeIcon, label: "Home" },
  { icon: LibraryIcon, label: "Library" },
  { icon: WatchIcon, label: "Watch" },
  { icon: HeartIcon, label: "Favorites" },
  { icon: NotesIcon, label: "Notes" },
];

/* The nav tabs share one glass "pill" that glides to whichever tab is active
   (framer-motion shared layout). Each nav instance gets its own `pillId` so the
   header bar and the floating dock animate independently and never fly across
   the screen when one fades out and the other takes over. */
function NavButtons({
  active,
  onNavigate,
  pillId,
  padding,
}: {
  active: string;
  onNavigate: (page: string) => void;
  pillId: string;
  padding: string;
}) {
  const { spring } = useMotion();
  return (
    <>
      {navItems.map((item) => {
        const isActive = active === item.label;
        return (
          <button
            key={item.label}
            onClick={() => onNavigate(item.label)}
            className={`relative flex items-center font-medium transition-colors duration-200 ${
              isActive ? "text-white" : "nav-btn-hover text-white/70 hover:text-white"
            }`}
            style={{
              padding,
              borderRadius: "999px",
              fontSize: 11,
              textShadow: isActive
                ? "0 1px 2px rgba(0,0,0,0.45)"
                : "none",
            }}
          >
            {isActive && (
              <motion.span
                layoutId={pillId}
                aria-hidden="true"
                className="nav-btn-active"
                style={{ position: "absolute", inset: 0, borderRadius: "999px", zIndex: 0 }}
                transition={spring("snappy")}
              />
            )}
            <span className="relative z-10 inline-flex items-center gap-1.5">
              <item.icon size={15} />
              <span>{item.label}</span>
            </span>
          </button>
        );
      })}
    </>
  );
}

export default function Navbar({ active, onNavigate, onLogout }: { active: string; onNavigate: (page: string) => void; onLogout?: () => void }) {
  const { reduced } = useMotion();
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
        background: "rgba(15, 14, 12, 0.92)",
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
              transition: "opacity var(--mo-t-base) var(--mo-ease-glide)",
              pointerEvents: buttonsInHeader ? "auto" : "none",
            }}
          >
            <NavButtons active={active} onNavigate={onNavigate} pillId="nav-pill-header" padding="6px 12px" />
          </nav>

          {/* Right: Search + Profile */}
          <div className="flex items-center gap-2.5">
            <GooeySearch />
            <button
              onClick={() => setProfileOpen(!profileOpen)}
              aria-label="Open profile menu"
              className={`w-7 h-7 rounded-full overflow-hidden border border-white/10 flex items-center justify-center ${reduced ? "" : "transition-transform hover:scale-105"}`}
            >
              <GeometricAvatar size={28} />
            </button>
          </div>
        </div>

        {/* Profile dropdown */}
        {profileOpen && (
          <motion.div
            initial={{ opacity: 0, y: -8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            className="absolute top-12 right-6 w-64 rounded-2xl overflow-hidden z-[60]"
            style={{
              background: "rgba(20, 18, 16, 0.96)",
              border: "1px solid rgba(212, 164, 78, 0.12)",
              boxShadow: "0 16px 48px -12px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(255,255,255,0.03)",
            }}
          >
            {/* Header with gradient */}
            <div
              className="relative px-4 py-3.5"
              style={{
                background: "linear-gradient(135deg, rgba(212,164,78,0.08) 0%, transparent 100%)",
                borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
              }}
            >
              <div className="flex items-center gap-3">
                <div className="relative" style={{ filter: "drop-shadow(0 2px 8px rgba(212,164,78,0.2))" }}>
                  <GeometricAvatar size={36} ring />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-[13px] font-semibold text-kinora-text truncate">User</p>
                  <p className="text-[10px] text-kinora-muted truncate">user@kinora.app</p>
                </div>
              </div>
            </div>

            {/* Menu items */}
            <div className="py-1.5">
              {[
                { label: "Edit Profile", icon: <path d="M12 12.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z" />, icon2: <path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6" />, action: () => { onNavigate("Edit Profile"); setProfileOpen(false); } },
                { label: "Settings", icon: <circle cx="12" cy="12" r="3" />, icon2: <path d="M12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" />, action: () => { onNavigate("Settings"); setProfileOpen(false); } },
                { label: "Pricing", icon: <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />, icon2: null, action: () => { onNavigate("Pricing"); setProfileOpen(false); } },
              ].map((item) => (
                <button
                  key={item.label}
                  onClick={item.action}
                  className="w-full flex items-center gap-3 px-4 py-2.5 text-[12px] text-kinora-muted transition-all duration-200 group"
                  style={{ borderRadius: 0 }}
                >
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" className="transition-colors duration-200 group-hover:text-[rgba(212,164,78,0.9)]">
                    {item.icon}
                    {item.icon2}
                  </svg>
                  <span className="flex-1 text-left transition-colors duration-200 group-hover:text-kinora-text">{item.label}</span>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="opacity-0 group-hover:opacity-40 transition-opacity duration-200">
                    <path d="M9 18l6-6-6-6" />
                  </svg>
                </button>
              ))}
            </div>

            {/* Divider */}
            <div className="h-px mx-4" style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.06), transparent)" }} />

            {/* Log Out */}
            <div className="py-1.5">
              <button
                onClick={() => { setProfileOpen(false); onLogout?.(); }}
                className="w-full flex items-center gap-3 px-4 py-2.5 text-[12px] text-red-400/60 hover:text-red-400 transition-all duration-200 group"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" className="transition-transform duration-200 group-hover:translate-x-[-1px]">
                  <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                  <path d="M16 17l5-5-5-5M21 12H9" />
                </svg>
                <span className="flex-1 text-left">Log Out</span>
              </button>
            </div>
          </motion.div>
        )}
      </header>

      {/* Floating dock — appears at bottom when scrolling through middle of page */}
      {showDock && (
      <div
        className="fixed z-50"
        style={{
          position: "fixed",
          bottom: 28,
          left: "50%",
          transform: `translateX(-50%) translateY(${dockVisible ? 0 : 120}px)`,
          borderRadius: "999px",
          padding: "5px 7px",
          background: "rgba(15, 14, 12, 0.92)",
          border: "1px solid rgba(255, 255, 255, 0.06)",
          boxShadow: "0 8px 32px -8px rgba(0, 0, 0, 0.6)",
          opacity: dockVisible ? 1 : 0,
          transition: "transform var(--mo-t-slow) var(--mo-ease-emphasized), opacity var(--mo-t-base) var(--mo-ease-glide)",
          pointerEvents: dockVisible ? "auto" : "none",
        }}
      >
        <nav className="flex items-center" style={{ gap: 2 }}>
          <NavButtons active={active} onNavigate={onNavigate} pillId="nav-pill-dock" padding="7px 12px" />
        </nav>
      </div>
      )}
    </>
  );
}
