import SwiftUI

/// A grid of generated films, each a glass card that opens the reading room.
struct WatchView: View {
    @Binding var openBook: KBook?
    private let cols = [GridItem(.adaptive(minimum: 168), spacing: 20)]

    var body: some View {
        ScrollView {
            LazyVGrid(columns: cols, spacing: 20) {
                ForEach(kBooks) { b in
                    BookCard(book: b) { withAnimation(.smooth(duration: 0.4)) { openBook = b } }
                }
            }
            .padding(.horizontal, 28)
            .padding(.top, 6)
            .padding(.bottom, 130)
        }
    }
}

/// The user's library — a glass-card grid of every book.
struct LibraryView: View {
    @Binding var openBook: KBook?
    private let cols = [GridItem(.adaptive(minimum: 162), spacing: 18)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Your Library").font(.system(.title, design: .serif).weight(.semibold)).foregroundStyle(.white)
                LazyVGrid(columns: cols, spacing: 18) {
                    ForEach(kBooks) { b in
                        BookCard(book: b) { withAnimation(.smooth(duration: 0.4)) { openBook = b } }
                    }
                }
            }
            .padding(.horizontal, 28)
            .padding(.top, 6)
            .padding(.bottom, 130)
        }
    }
}
