import { useState, useEffect, lazy, Suspense } from "react";
import type React from "react";
import { api, toUiBook, toBrowserUrl } from "../lib/api";
import Navbar, { navItems } from "./Navbar";
import Greeting from "./Greeting";
import BookShelf from "./BookShelf";
import HeroBanner from "./HeroBanner";
import ReadingRoom from "./ReadingRoom";
import AnimatedPageSwitch from "./AnimatedPageSwitch";
import AmbientBackground from "./AmbientBackground";
import logoImg from "../assets/logo-transparent.png";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
} from "../data/books";
import type { Book } from "../data/books";

const LibraryPage = lazy(() => import("./LibraryPage"));
const WatchPage = lazy(() => import("./WatchPage"));
const FavoritesPage = lazy(() => import("./FavoritesPage"));
const NotesPage = lazy(() => import("./NotesPage"));
const EditProfilePage = lazy(() => import("./EditProfilePage"));
const SettingsPage = lazy(() => import("./SettingsPage"));
const PricingPage = lazy(() => import("./PricingPage"));

const PageFallback = () => (
  <div className="flex items-center justify-center min-h-[60vh]">
    <div className="w-6 h-6 rounded-full border-2 border-white/10 border-t-white/30 animate-spin" />
  </div>
);

export default function HomePage({ onLogout }: { onLogout: () => void }) {
  const [activePage, setActivePageState] = useState("Home");
  const [selectedBook, setSelectedBook] = useState<Book | null>(null);
  const [myBooks, setMyBooks] = useState<Book[]>([]);

  // Pull the signed-in user's real library from the backend (cover = rendered
  // page 1). Silently no-ops in demo mode (not authed / backend down).
  useEffect(() => {
    if (!api.isAuthed()) return;
    let alive = true;
    (async () => {
      try {
        const books = await api.listBooks();
        const mapped = await Promise.all(
          // Only ready books are drivable by a reading session.
          books.filter((b) => b.status === "ready").map(async (b) => {
            let cover = "";
            try {
              cover = toBrowserUrl((await api.getPage(b.id, 1)).image_url);
            } catch {
              /* no page image yet */
            }
            return toUiBook(b, cover);
          })
        );
        if (alive) setMyBooks(mapped);
      } catch {
        /* backend down / not authed — demo catalogue still renders below */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const setActivePage = (page: string) => {
    setActivePageState(page);
    window.scrollTo({ top: 0, behavior: "instant" });
  };

  const pages: Record<string, React.ReactNode> = {
    Home: (
      <main className="pb-8 relative z-10">
        <HeroBanner />
        <div className="pt-6 px-6 max-w-[1280px] mx-auto">
          <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3 mb-4">
            <Greeting />
          </div>
          {myBooks.length > 0 && (
            <BookShelf title="Read Live · Public Domain" books={myBooks} onOpen={setSelectedBook} />
          )}
          <BookShelf title="Continue Reading" books={continueReading} onOpen={setSelectedBook} />
          <BookShelf title="Recently Added" books={recentlyAdded} onOpen={setSelectedBook} />
          <BookShelf title="Popular on Kinora" books={popularOnKinora} onOpen={setSelectedBook} />
          <BookShelf title="Recommended for You" books={recommended} onOpen={setSelectedBook} />
        </div>
      </main>
    ),
    Library: <Suspense fallback={<PageFallback />}><LibraryPage /></Suspense>,
    Watch: <Suspense fallback={<PageFallback />}><WatchPage /></Suspense>,
    Favorites: <Suspense fallback={<PageFallback />}><FavoritesPage /></Suspense>,
    Notes: <Suspense fallback={<PageFallback />}><NotesPage /></Suspense>,
    "Edit Profile": <Suspense fallback={<PageFallback />}><EditProfilePage /></Suspense>,
    Settings: <Suspense fallback={<PageFallback />}><SettingsPage /></Suspense>,
    Pricing: <Suspense fallback={<PageFallback />}><PricingPage /></Suspense>,
  };

  return (
    <div className="kinora-bg min-h-screen flex flex-col relative">
      <AmbientBackground />
      <Navbar active={activePage} onNavigate={setActivePage} onLogout={onLogout} />

      <div className="flex-1">
        <AnimatedPageSwitch active={activePage} pages={pages} />
      </div>

      {/* Footer */}
      <footer className="footer-glass relative z-10">
        <div className="max-w-[1280px] mx-auto px-6 py-4">
          <div className="flex flex-col sm:flex-row items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <img src={logoImg} alt="Kinora" width={24} height={24} style={{ objectFit: "contain" }} />
              <div>
                <p className="font-serif text-[13px] font-semibold text-kinora-text tracking-wide">Kinora</p>
                <p className="text-[9px] text-kinora-muted">Where stories come to life.</p>
              </div>
            </div>

            <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1">
              {navItems.map((item) => (
                <button
                  key={item.label}
                  onClick={() => setActivePage(item.label)}
                  className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors"
                  style={activePage === item.label ? { color: "rgba(232, 226, 216, 0.9)" } : undefined}
                >
                  {item.label}
                </button>
              ))}
              <button onClick={() => setActivePage("Pricing")} className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
                Pricing
              </button>
              <button onClick={() => setActivePage("Settings")} className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
                Settings
              </button>
              <a href="#" className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
                Privacy
              </a>
              <a href="#" className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
                Terms
              </a>
            </div>

            <p className="text-[10px] text-kinora-muted/85">
              © {new Date().getFullYear()} Kinora. All rights reserved.
            </p>
          </div>
        </div>
      </footer>

      {/* Reading room overlay — scroll-driven, generates the film as you read */}
      <ReadingRoom book={selectedBook} onClose={() => setSelectedBook(null)} />
    </div>
  );
}
