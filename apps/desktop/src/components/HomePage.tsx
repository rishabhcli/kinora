import { useState, useEffect, lazy, Suspense, useMemo } from "react";
import type React from "react";
import { useTranslation } from "react-i18next";
import { api, toUiBook, toBrowserUrl, ApiError, type BookResponse } from "../lib/api";
import Navbar, { navItems } from "./Navbar";
import { useGlobalNavShortcuts } from "../a11y/useNavShortcuts";
import Greeting from "./Greeting";
import HeroBanner from "./HeroBanner";
import AmbientBackground from "./AmbientBackground";
import BookShelf from "./BookShelf";
import DiscoveryHome from "./discovery/DiscoveryHome";
import DiscoverySearch from "./discovery/DiscoverySearch";
import CommandPalette from "./discovery/CommandPalette";
import { useCommandPalette } from "./discovery/useCommandPalette";
import { buildCommands } from "./discovery/commands";
import { mergeCatalog, popularityPrior } from "./discovery/buildCatalog";
import type { DiscoveryBook } from "../lib/discovery/types";
import { nextToResume } from "../lib/discovery/continueReading";
import {
  MotionProvider,
  MotionDebugOverlay,
  PageTransition,
  BookOpenTransition,
  useSharedElement,
  type Rect,
} from "../motion";
import MotionShowcase from "../motion/MotionShowcase";
import logoImg from "../assets/logo-transparent.png";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
  awardWinners,
} from "../data/books";
import type { Book } from "../data/books";

// The reading room (ScrollFilmEngine, FilmPane, …) is the heaviest screen and most
// sessions browse without opening a book — defer its chunk until the first open.
const ReadingRoom = lazy(() => import("./ReadingRoom"));
const LibraryPage = lazy(() => import("./LibraryPage"));
const WatchPage = lazy(() => import("./WatchPage"));
const FavoritesPage = lazy(() => import("./FavoritesPage"));
const NotesPage = lazy(() => import("./NotesPage"));
const EditProfilePage = lazy(() => import("./EditProfilePage"));
const SettingsPage = lazy(() => import("./SettingsPage"));
const PricingPage = lazy(() => import("./PricingPage"));

// Demo credentials (mirrors LoginPage) — used to silently recover a stale
// session so the public-domain shelf always populates in the demo experience.
const DEMO = { email: "demo@kinora.local", password: "demo-password-123" } as const;

const PageFallback = () => (
  <div className="flex items-center justify-center min-h-[60vh]">
    <div className="w-6 h-6 rounded-full border-2 border-white/10 border-t-white/30 animate-spin" />
  </div>
);

export default function HomePage({ onLogout }: { onLogout: () => void }) {
  const { t } = useTranslation();
  const [activePage, setActivePageState] = useState("Home");
  const [selectedBook, setSelectedBook] = useState<Book | null>(null);
  const [myBooks, setMyBooks] = useState<Book[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(true);

  // Book open/close orchestration (WS2). `roomOpen` drives the shared-
  // element morph; `selectedBook` stays set through the close flight so the
  // room only unmounts once the cover has flown back to its shelf slot.
  const [roomOpen, setRoomOpen] = useState(false);
  const [originRect, setOriginRect] = useState<Rect | null>(null);
  const shared = useSharedElement();

  // Discovery: a seeded query for the Search surface (set by the palette / nav).
  const [searchSeed, setSearchSeed] = useState("");

  const handleOpen = (book: Book) => {
    // The shelf cover's rect was captured on pointer-down (capture phase).
    setOriginRect(shared.takeRect());
    setSelectedBook(book);
    setRoomOpen(true);
  };
  const handleCloseRoom = () => setRoomOpen(false);

  // Reading room visible (opening, reading, or flying back on close).
  const inRoom = roomOpen || selectedBook !== null;

  // The ⌘K command palette (and "/") — suppressed while a book is open so the
  // reading room owns the keyboard.
  const palette = useCommandPalette({ isSuppressed: () => inRoom });

  // The discovery catalog: the user's real backend library first (so live books
  // win on id collision), then the demo catalogue shelves. Enriched with
  // genre/era from the bundled manifest by `mergeCatalog`. Every shelf should
  // feel like a clear route from "pick a book" to "open the film."
  const catalog: DiscoveryBook[] = useMemo(
    () =>
      mergeCatalog(
        myBooks,
        continueReading,
        recentlyAdded,
        popularOnKinora,
        recommended,
        awardWinners,
      ),
    [myBooks],
  );
  // Live backend titles get their own shelf; keep them out of discovery rails so
  // they aren't duplicated or dropped when a rail's minRowSize isn't met.
  const discoveryBooks = useMemo(
    () => catalog.filter((b) => !b.live),
    [catalog],
  );
  const popularity = useMemo(
    () => popularityPrior(catalog, popularOnKinora.map((b) => b.id)),
    [catalog],
  );

  // Pull the signed-in user's real library from the backend (cover = rendered
  // page 1) into the "Read Live · Public Domain" shelf.
  //
  // A relaunch can carry a STALE token: `isAuthed()` is true but every call
  // 401s ("invalid token"), which silently emptied this shelf and made the
  // open-source row vanish. The demo experience should always populate it, so
  // we recover: on a 401 (or no token at all), clear it and silently re-auth
  // as the demo user, then retry. The refreshed token also fixes downstream
  // calls (createSession when opening a book). Backend down → row just stays
  // empty and the demo catalogue rows below still render.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        if (!api.isAuthed()) {
          await api.loginOrRegister(DEMO.email, DEMO.password).catch(() => {});
        }
        let books: BookResponse[];
        try {
          books = await api.listBooks();
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) {
            api.logout();
            await api.loginOrRegister(DEMO.email, DEMO.password);
            books = await api.listBooks();
          } else throw e;
        }
        const mapped = await Promise.all(
          // Only ready books are drivable by a reading session.
          books.filter((b) => b.status === "ready").map(async (b) => {
            let cover = "";
            try {
              const pageImage = toBrowserUrl((await api.getPageCached(b.id, 1)).image_url);
              cover = pageImage
                ? `${pageImage}?v=cover-3-${encodeURIComponent(b.created_at ?? "1")}`
                : "";
            } catch {
              /* no page image yet */
            }
            return toUiBook(b, cover);
          })
        );
        if (alive) setMyBooks(mapped);
      } catch {
        /* backend down — demo catalogue rows below still render */
      } finally {
        if (alive) setLibraryLoading(false);
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

  const openSearch = (query = "") => {
    setSearchSeed(query);
    setActivePage("Search");
  };

  // Hidden power-user navigation (Cmd/Ctrl+1..5 tabs, arrow/Home/End scroll).
  // Deferred while the reading room owns the keyboard. Not surfaced in any UI.
  useGlobalNavShortcuts({
    tabs: navItems.map((n) => n.label),
    onSelectTab: setActivePage,
    isSuppressed: () => inRoom,
  });

  // ⌘K command list: nav targets + every catalog book + global actions. Rebuilt
  // when the catalog changes; `run` callbacks route into the existing handlers.
  // The palette keeps the ambitious parts reachable: one keystroke can move from
  // a static library to the reading-room loop.
  const commands = useMemo(
    () =>
      buildCommands({
        navTargets: [
          ...navItems.map((n) => ({ label: n.label })),
          { label: "Pricing" },
          { label: "Settings" },
        ],
        navigate: setActivePage,
        books: catalog,
        openBook: (b) => handleOpen(b),
        openSearch,
        resume: () => {
          const book = nextToResume(catalog);
          if (book) handleOpen(book);
        },
      }),
    // setActivePage/handleOpen are stable enough for this demo; rebuild on catalog.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [catalog],
  );

  const pages: Record<string, React.ReactNode> = {
    Home: (
      <main className="pb-8 relative z-10">
        <HeroBanner />
        <div className="pt-6 px-6 max-w-[1280px] mx-auto">
          <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3 mb-4">
            <Greeting />
          </div>
        </div>
        {/* Backend public-domain library — always its own shelf (not subject to
            discovery minRowSize), restored from the UI-baseline BookShelf row. */}
        {myBooks.length > 0 && (
          <div className="px-6 max-w-[1280px] mx-auto mb-2">
            <BookShelf
              title={t("library.readLivePublicDomain")}
              books={myBooks}
              onOpen={handleOpen}
            />
          </div>
        )}
        {/* Personalized discovery rails for the demo catalogue (live books excluded). */}
        <DiscoveryHome
          books={discoveryBooks}
          loading={libraryLoading}
          popularity={popularity}
          onOpenBook={handleOpen}
          onMoreLikeThis={(b) => openSearch(b.genre ?? b.title)}
        />
      </main>
    ),
    Search: (
      <main className="pb-8 relative z-10">
        <div className="pt-6 px-6 max-w-[1280px] mx-auto">
          <h1 className="font-serif text-xl font-semibold text-kinora-text mb-1">
            {t("library.search", { defaultValue: "Search" })}
          </h1>
          <p className="text-[12px] text-kinora-muted mb-2">
            Find a book by title, author, genre, era, or theme.
          </p>
        </div>
        <DiscoverySearch books={catalog} initialQuery={searchSeed} onOpen={handleOpen} />
      </main>
    ),
    Library: <Suspense fallback={<PageFallback />}><LibraryPage onOpenBook={handleOpen} /></Suspense>,
    Watch: <Suspense fallback={<PageFallback />}><WatchPage /></Suspense>,
    Favorites: <Suspense fallback={<PageFallback />}><FavoritesPage /></Suspense>,
    Notes: <Suspense fallback={<PageFallback />}><NotesPage /></Suspense>,
    "Edit Profile": <Suspense fallback={<PageFallback />}><EditProfilePage /></Suspense>,
    Settings: <Suspense fallback={<PageFallback />}><SettingsPage /></Suspense>,
    Pricing: <Suspense fallback={<PageFallback />}><PricingPage /></Suspense>,
  };

  // Dev-only motion showcase (gated behind ?motiondemo) — demonstrates the
  // primitives (ShelfScroller/Tilt) the product wires at integration.
  const motionDemo =
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).has("motiondemo");

  return (
    <MotionProvider>
    {motionDemo ? (
      <MotionShowcase />
    ) : (
    <div className="kinora-bg min-h-screen flex flex-col relative">
      <AmbientBackground />
      {/* The reading room is a full-screen modal with its own ← Back bar; the
          app nav + logo must disappear while a book is open (and stay hidden
          through the close flight — `selectedBook` lingers until onClosed).
          The room overlay lives inside the open-transition's own stacking
          context, so the fixed navbar (z-50) would otherwise paint over it. */}
      {!inRoom && <Navbar active={activePage} onNavigate={setActivePage} onLogout={onLogout} />}

      {/* Global ⌘K / "/" command palette — fuzzy nav + jump-to-book + actions.
          Suppressed while the reading room owns the keyboard. */}
      <CommandPalette open={palette.open} commands={commands} onClose={palette.hide} />

      {/* Capture the tapped cover's rect (pointer-down, capture phase) so a
          subsequent onOpen can morph it. Keys off the existing `.book-cover`
          class — no edit to Agent 5's BookCard required. */}
      <div className="flex-1" onPointerDownCapture={shared.capturePointer}>
        <PageTransition activeKey={activePage}>{pages[activePage]}</PageTransition>
      </div>

      {/* Footer — hidden while the reading room is open (it's a focused modal). */}
      {!inRoom && (
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
      )}

      {/* Reading room overlay — scroll-driven, generates the film as you read.
          Wrapped in the shared-element morph: the tapped cover flies from its
          shelf slot to the room and back. The room is mount-gated by `opened`
          so the travel lands before the room's own hinge plays. */}
      <BookOpenTransition
        open={roomOpen}
        originRect={originRect}
        cover={{ image: selectedBook?.coverImage, gradient: selectedBook?.coverGradient }}
        onClosed={() => {
          setSelectedBook(null);
          setOriginRect(null);
        }}
      >
        {(opened) =>
          selectedBook ? (
            <Suspense fallback={null}>
              <ReadingRoom book={opened ? selectedBook : null} onClose={handleCloseRoom} />
            </Suspense>
          ) : null
        }
      </BookOpenTransition>
    </div>
    )}
    <MotionDebugOverlay />
    </MotionProvider>
  );
}
