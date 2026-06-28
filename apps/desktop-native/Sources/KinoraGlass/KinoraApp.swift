import SwiftUI
import KinoraGlassKit

// MARK: - Model bridge
//
// The showcase UI is written against a local `KBook` alias of the kit's `Book` so the
// existing views read naturally. The kit owns the canonical model + demo catalogue.
typealias KBook = Book

extension Book {
    /// Bundled demo-film URL for the showcase player.
    var filmURL: URL? {
        Bundle.module.url(forResource: film, withExtension: "mp4", subdirectory: "Resources/films")
    }
}

/// The bundled demo catalogue, exposed under the showcase's historical name.
let kBooks: [KBook] = Book.demoCatalogue

enum Screen: String, CaseIterable, Identifiable {
    case home = "Home", library = "Library", watch = "Watch", favorites = "Favorites", settings = "Settings"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .home: return "house.fill"
        case .library: return "books.vertical.fill"
        case .watch: return "play.circle.fill"
        case .favorites: return "heart.fill"
        case .settings: return "gearshape.fill"
        }
    }
}

// MARK: - App

@main
struct KinoraApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var deepLinks = DeepLinkBus.shared

    var body: some Scene {
        WindowGroup(id: "shell") {
            ShellContainerView(deepLinkBus: deepLinks)
                .frame(minWidth: 1060, minHeight: 720)
                .preferredColorScheme(.dark)
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentMinSize)
        .commands { KinoraCommands(deepLinks: deepLinks) }

        // A dedicated, simpler scene for opening one book in its own window (multi-window).
        WindowGroup(id: "book", for: String.self) { $bookID in
            ShellContainerView(deepLinkBus: deepLinks)
                .frame(minWidth: 1060, minHeight: 720)
                .preferredColorScheme(.dark)
                .task(id: bookID) {
                    if let bookID { deepLinks.send(.openBook(id: bookID)) }
                }
        }
        .windowStyle(.hiddenTitleBar)

        Settings {
            ShellPreferencesView()
        }
    }
}

// MARK: - Showcase root (offline fallback; the original self-contained native UI)

/// The self-contained native showcase — used when no renderer is reachable so the app
/// is never a blank window. Every control is real Liquid Glass.
struct ShowcaseRootView: View {
    @State private var screen: Screen = .home
    @State private var openBook: KBook?
    @State private var nowPlaying: KBook?

    var body: some View {
        ZStack(alignment: .bottom) {
            KinoraBackground()

            VStack(spacing: 0) {
                GlassTopBar()
                Group {
                    switch screen {
                    case .home: HomeView(openBook: $openBook)
                    case .library: LibraryView(openBook: $openBook)
                    case .watch: WatchView(openBook: $openBook)
                    case .favorites: PlaceholderView(title: "Favorites", systemImage: "heart.fill")
                    case .settings: SettingsView()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            VStack(spacing: 12) {
                if let np = nowPlaying, openBook == nil {
                    NowPlayingPill(book: np) { withAnimation(.smooth(duration: 0.4)) { openBook = np } }
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
                GlassTabBar(screen: $screen)
            }
            .padding(.bottom, 22)
            .animation(.smooth(duration: 0.4), value: nowPlaying != nil)
        }
        .ignoresSafeArea()
        .overlay {
            if let b = openBook {
                ReaderView(book: b) { withAnimation(.smooth(duration: 0.4)) { openBook = nil } }
                    .transition(.opacity.combined(with: .scale(scale: 1.03)))
                    .zIndex(20)
            }
        }
        .onChange(of: openBook) { _, new in if let b = new { nowPlaying = b } }
    }
}

/// Find-My-style floating "Now Playing" glass accessory above the tab bar.
struct NowPlayingPill: View {
    let book: KBook
    var onTap: () -> Void
    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 11) {
                Image(systemName: "play.fill").font(.system(size: 12, weight: .bold)).foregroundStyle(.white)
                VStack(alignment: .leading, spacing: 1) {
                    Text("NOW PLAYING").font(.system(size: 8.5, weight: .bold)).tracking(1).foregroundStyle(.white.opacity(0.55))
                    Text(book.title).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(.white).lineLimit(1)
                }
                Spacer(minLength: 10)
                Image(systemName: "waveform").font(.system(size: 13, weight: .semibold)).foregroundStyle(Color(red: 0.83, green: 0.64, blue: 0.31))
            }
            .padding(.horizontal, 15).padding(.vertical, 10).frame(width: 268)
        }
        .buttonStyle(.plain)
        .glassEffect(.regular, in: .capsule)
        .shadow(color: .black.opacity(0.35), radius: 18, y: 10)
    }
}

// MARK: - Background (gives the glass something to refract)

struct KinoraBackground: View {
    var body: some View {
        ZStack {
            LinearGradient(
                colors: [Color(red: 0.11, green: 0.10, blue: 0.09), Color(red: 0.05, green: 0.045, blue: 0.04)],
                startPoint: .top, endPoint: .bottom
            )
            RadialGradient(colors: [Color(red: 0.83, green: 0.64, blue: 0.31).opacity(0.18), .clear],
                           center: .top, startRadius: 8, endRadius: 760)
            // soft floating colour blobs so the Liquid Glass has rich content to lens
            Circle().fill(Color(red: 0.5, green: 0.28, blue: 0.12).opacity(0.5)).frame(width: 520).blur(radius: 160).offset(x: -300, y: -160)
            Circle().fill(Color(red: 0.15, green: 0.22, blue: 0.4).opacity(0.45)).frame(width: 560).blur(radius: 170).offset(x: 320, y: 220)
        }
        .ignoresSafeArea()
    }
}

// MARK: - Glass top bar

struct GlassTopBar: View {
    @State private var query = ""
    var body: some View {
        HStack(spacing: 10) {
            Spacer().frame(width: 72)
            Image(systemName: "book.pages.fill").foregroundStyle(Color(red: 0.83, green: 0.64, blue: 0.31))
            Text("Kinora").font(.system(.title3, design: .serif).weight(.semibold)).foregroundStyle(.white)
            Spacer()
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.system(size: 12, weight: .semibold)).foregroundStyle(.white.opacity(0.6))
                TextField("Search", text: $query).textFieldStyle(.plain).font(.system(size: 12.5)).foregroundStyle(.white).frame(width: 130)
            }
            .padding(.horizontal, 13).frame(height: 32)
            .glassEffect(.regular, in: .capsule)
            Button { } label: { Image(systemName: "person.crop.circle").font(.system(size: 15, weight: .semibold)).frame(width: 32, height: 32) }
                .buttonStyle(.glass).buttonBorderShape(.circle)
        }
        .padding(.horizontal, 24)
        .padding(.top, 14)
        .padding(.bottom, 14)
    }
}

// MARK: - Floating glass tab bar (the functional layer)

struct GlassTabBar: View {
    @Binding var screen: Screen
    @Namespace private var ns
    @State private var hovered: Screen?

    var body: some View {
        HStack(spacing: 4) {
            ForEach(Screen.allCases) { s in
                let sel = s == screen
                Button {
                    withAnimation(.bouncy(duration: 0.42)) { screen = s }
                } label: {
                    VStack(spacing: 3) {
                        Image(systemName: s.icon).font(.system(size: 17, weight: .semibold))
                        Text(s.rawValue).font(.system(size: 9.5, weight: .semibold))
                    }
                    .foregroundStyle(sel ? .white : .white.opacity(0.55))
                    .frame(width: 70, height: 52)
                    .background {
                        if sel {
                            Capsule().fill(.white.opacity(0.18))
                                .matchedGeometryEffect(id: "tabsel", in: ns)
                        }
                    }
                }
                .buttonStyle(.plain)
                .scaleEffect(hovered == s ? 1.1 : 1)
                .animation(.smooth(duration: 0.2), value: hovered)
                .onHover { hovered = $0 ? s : nil }
            }
        }
        .padding(6)
        .glassEffect(.regular, in: .capsule)
        .shadow(color: .black.opacity(0.4), radius: 24, y: 12)
    }
}

// MARK: - Home

struct HomeView: View {
    @Binding var openBook: KBook?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
                FeaturedHero(book: kBooks[0]) { withAnimation(.smooth(duration: 0.4)) { openBook = kBooks[0] } }
                shelf("Continue Reading", kBooks)
                shelf("Recently Added", Array(kBooks.reversed()))
                shelf("Popular on Kinora", kBooks)
            }
            .padding(.horizontal, 28)
            .padding(.top, 6)
            .padding(.bottom, 130)
        }
    }

    func shelf(_ title: String, _ books: [KBook]) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(title).font(.system(.title3, design: .serif).weight(.semibold)).foregroundStyle(.white)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 18) {
                    ForEach(books) { b in
                        BookCard(book: b) { withAnimation(.smooth(duration: 0.4)) { openBook = b } }
                    }
                }
                .padding(.vertical, 4)
            }
        }
    }
}

struct FeaturedHero: View {
    let book: KBook
    var onOpen: () -> Void
    private let gold = Color(red: 0.83, green: 0.64, blue: 0.31)

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            AsyncImage(url: book.coverURL) { img in
                img.resizable().aspectRatio(contentMode: .fill)
            } placeholder: {
                Rectangle().fill(.white.opacity(0.06))
            }
            .frame(maxWidth: .infinity)
            .frame(height: 250)
            .clipped()

            // Legibility scrim under the text.
            LinearGradient(colors: [.clear, .black.opacity(0.35), .black.opacity(0.82)], startPoint: .top, endPoint: .bottom)

            VStack(alignment: .leading, spacing: 9) {
                Text("FEATURED").font(.system(size: 10, weight: .bold)).tracking(2).foregroundStyle(.white.opacity(0.7))
                Text(book.title).font(.system(.largeTitle, design: .serif).weight(.bold)).foregroundStyle(.white)
                Text("by \(book.author)").font(.callout).foregroundStyle(.white.opacity(0.8))
                HStack(spacing: 10) {
                    Button(action: onOpen) { Label("Read Now", systemImage: "book.fill").font(.system(size: 13, weight: .semibold)) }
                        .buttonStyle(.glassProminent).tint(gold)
                    Button(action: onOpen) { Label("Watch Film", systemImage: "play.fill").font(.system(size: 13, weight: .semibold)) }
                        .buttonStyle(.glass)
                }
                .padding(.top, 4)
            }
            .padding(26)
        }
        .frame(height: 250)
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .overlay(RoundedRectangle(cornerRadius: 24).stroke(.white.opacity(0.12), lineWidth: 0.5))
        .shadow(color: .black.opacity(0.4), radius: 20, y: 12)
    }
}

struct BookCard: View {
    let book: KBook
    var onOpen: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 9) {
                AsyncImage(url: book.coverURL) { img in
                    img.resizable().aspectRatio(contentMode: .fill)
                } placeholder: {
                    RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.07))
                }
                .frame(width: 150, height: 224)
                .clipShape(RoundedRectangle(cornerRadius: 9))
                .overlay(RoundedRectangle(cornerRadius: 9).stroke(.white.opacity(0.12), lineWidth: 0.5))
                .shadow(color: .black.opacity(0.5), radius: 14, y: 8)

                Text(book.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.white).lineLimit(1)
                Text(book.author).font(.system(size: 11)).foregroundStyle(.white.opacity(0.6)).lineLimit(1)
            }
            .frame(width: 150)
            .padding(11)
        }
        .buttonStyle(.plain)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 18))
        .scaleEffect(hover ? 1.035 : 1)
        .animation(.smooth(duration: 0.25), value: hover)
        .onHover { hover = $0 }
    }
}

struct PlaceholderView: View {
    let title: String
    let systemImage: String
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: systemImage).font(.system(size: 34, weight: .semibold)).foregroundStyle(.white.opacity(0.85))
            Text(title).font(.system(.title2, design: .serif).weight(.semibold)).foregroundStyle(.white)
            Text("Coming soon").font(.callout).foregroundStyle(.white.opacity(0.55))
        }
        .padding(40)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 26))
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
