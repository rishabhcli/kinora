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
