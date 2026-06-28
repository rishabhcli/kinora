import SwiftUI
import KinoraGlassKit

/// The native glass chrome that frames the web shell: a left **sidebar** of library
/// sections, a top **toolbar** (traffic-light inset + title + search + account), and a
/// floating **command bar** at the bottom (the Director controls). Every surface is real
/// `.glassEffect`. When the renderer sees `__KINORA_NATIVE__` it suppresses its own
/// equivalents and lets these float over its content.

private let kGold = Color(red: 0.83, green: 0.64, blue: 0.31)

// MARK: - Sidebar

/// Library navigation rail. Selection drives a `kinora://route` deep link into the
/// renderer (or the showcase screen when offline).
struct GlassSidebar: View {
    @Binding var selection: SidebarItem
    var onSelect: (SidebarItem) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("KINORA")
                .font(.system(size: 11, weight: .bold, design: .serif))
                .tracking(2)
                .foregroundStyle(kGold)
                .padding(.horizontal, 14)
                .padding(.top, 14)
                .padding(.bottom, 10)

            ForEach(SidebarItem.allCases) { item in
                sidebarRow(item)
            }
            Spacer()
            sidebarRow(.settings)
                .padding(.bottom, 12)
        }
        .frame(width: 196)
        .frame(maxHeight: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22))
        .padding(10)
    }

    @ViewBuilder
    private func sidebarRow(_ item: SidebarItem) -> some View {
        let selected = item == selection
        Button {
            selection = item
            onSelect(item)
        } label: {
            HStack(spacing: 11) {
                Image(systemName: item.icon)
                    .font(.system(size: 14, weight: .semibold))
                    .frame(width: 20)
                Text(item.title).font(.system(size: 13, weight: .medium))
                Spacer()
            }
            .foregroundStyle(selected ? .white : .white.opacity(0.6))
            .padding(.horizontal, 12)
            .frame(height: 38)
            .background {
                if selected {
                    RoundedRectangle(cornerRadius: 11).fill(.white.opacity(0.16))
                }
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 8)
    }
}

enum SidebarItem: String, CaseIterable, Identifiable {
    case home, library, watch, favorites, settings
    var id: String { rawValue }
    var title: String {
        switch self {
        case .home: return "Home"
        case .library: return "Library"
        case .watch: return "Watch"
        case .favorites: return "Favorites"
        case .settings: return "Settings"
        }
    }
    var icon: String {
        switch self {
        case .home: return "house.fill"
        case .library: return "books.vertical.fill"
        case .watch: return "play.circle.fill"
        case .favorites: return "heart.fill"
        case .settings: return "gearshape.fill"
        }
    }
    /// The renderer route this sidebar item maps to (for the deep-link bridge).
    var route: String {
        switch self {
        case .home: return "/"
        case .library: return "/library"
        case .watch: return "/watch"
        case .favorites: return "/favorites"
        case .settings: return "/settings"
        }
    }
}

// MARK: - Toolbar

/// The top glass toolbar. Leaves room on the left for the traffic-light buttons (the
/// window uses `.hiddenTitleBar`, so we inset content past the controls), and carries
/// search + an account button on the right — all glass.
struct GlassToolbar: View {
    @Binding var query: String
    var onImport: () -> Void
    var onSearchSubmit: (String) -> Void

    var body: some View {
        HStack(spacing: 12) {
            // Traffic-light inset: ~72pt keeps content clear of the close/min/zoom buttons.
            Spacer().frame(width: 72)

            Image(systemName: "book.pages.fill").foregroundStyle(kGold)
            Text("Kinora")
                .font(.system(.title3, design: .serif).weight(.semibold))
                .foregroundStyle(.white)

            Spacer()

            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.6))
                TextField("Search your library", text: $query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12.5))
                    .foregroundStyle(.white)
                    .frame(width: 180)
                    .onSubmit { onSearchSubmit(query) }
            }
            .padding(.horizontal, 13)
            .frame(height: 32)
            .glassEffect(.regular, in: .capsule)

            Button(action: onImport) {
                Image(systemName: "plus").font(.system(size: 14, weight: .bold)).frame(width: 32, height: 32)
            }
            .buttonStyle(.glass).buttonBorderShape(.circle)
            .help("Import a PDF or EPUB")

            Button {} label: {
                Image(systemName: "person.crop.circle").font(.system(size: 15, weight: .semibold)).frame(width: 32, height: 32)
            }
            .buttonStyle(.glass).buttonBorderShape(.circle)
        }
        .padding(.horizontal, 18)
        .padding(.top, 12)
        .padding(.bottom, 10)
    }
}

// MARK: - Command bar (Director controls)

/// A floating glass command bar — the §5.4 Director controls surfaced natively. Pure
/// chrome that posts intents back through the bridge; the renderer owns the actual
/// session round-trips.
struct GlassCommandBar: View {
    @Binding var mode: WindowState.Mode
    var onComment: () -> Void
    var onTimeline: () -> Void
    var onCanon: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            modeSwitch
            Divider().frame(height: 22).overlay(.white.opacity(0.16))
            commandButton("Comment", "text.bubble", onComment)
            commandButton("Timeline", "film.stack", onTimeline)
            commandButton("Canon", "books.vertical", onCanon)
        }
        .padding(8)
        .glassEffect(.regular, in: .capsule)
        .shadow(color: .black.opacity(0.4), radius: 24, y: 12)
    }

    private var modeSwitch: some View {
        HStack(spacing: 2) {
            ForEach([WindowState.Mode.viewer, .director], id: \.self) { m in
                let selected = m == mode
                Button { withAnimation(.bouncy(duration: 0.35)) { mode = m } } label: {
                    Text(m == .viewer ? "Viewer" : "Director")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(selected ? .black : .white.opacity(0.7))
                        .padding(.horizontal, 14)
                        .frame(height: 30)
                        .background {
                            if selected { Capsule().fill(kGold) }
                        }
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func commandButton(_ title: String, _ icon: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.system(size: 12, weight: .semibold))
                .labelStyle(.iconOnly)
                .frame(width: 34, height: 30)
        }
        .buttonStyle(.glass)
        .help(title)
    }
}
