import SwiftUI

// MARK: - Model

struct KBook: Identifiable, Hashable {
    let id: String
    let title: String
    let author: String
    let isbn: String
    let film: String
    var coverURL: URL? { URL(string: "https://covers.openlibrary.org/b/isbn/\(isbn)-L.jpg") }
    var filmURL: URL? { Bundle.module.url(forResource: film, withExtension: "mp4", subdirectory: "Resources/films") }
}

let kBooks: [KBook] = [
    .init(id: "frog", title: "The Frog-King", author: "Brothers Grimm", isbn: "9780525559474", film: "film-01"),
    .init(id: "alice", title: "Alice in Wonderland", author: "Lewis Carroll", isbn: "9780553213454", film: "film-02"),
    .init(id: "pride", title: "Pride and Prejudice", author: "Jane Austen", isbn: "9780141439518", film: "film-03"),
    .init(id: "gatsby", title: "The Great Gatsby", author: "F. Scott Fitzgerald", isbn: "9780743273565", film: "film-04"),
    .init(id: "atomic", title: "Atomic Habits", author: "James Clear", isbn: "9780735211292", film: "film-02"),
    .init(id: "sapiens", title: "Sapiens", author: "Yuval Noah Harari", isbn: "9780062316097", film: "film-03"),
    .init(id: "dune", title: "Dune", author: "Frank Herbert", isbn: "9780441172719", film: "film-04"),
]

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
    var body: some Scene {
        WindowGroup {
            RootView()
                .frame(minWidth: 1060, minHeight: 720)
                .preferredColorScheme(.dark)
        }
        .windowStyle(.hiddenTitleBar)
    }
}

struct RootView: View {
    @State private var screen: Screen = .home
    @State private var openBook: KBook?

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

            GlassTabBar(screen: $screen)
                .padding(.bottom, 22)
        }
        .ignoresSafeArea()
        .overlay {
            if let b = openBook {
                ReaderView(book: b) { withAnimation(.smooth(duration: 0.4)) { openBook = nil } }
                    .transition(.opacity.combined(with: .scale(scale: 1.03)))
                    .zIndex(20)
            }
        }
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
        .padding(.top, 34)
        .padding(.bottom, 14)
    }
}

// MARK: - Floating glass tab bar (the functional layer)

struct GlassTabBar: View {
    @Binding var screen: Screen
    @Namespace private var ns

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
