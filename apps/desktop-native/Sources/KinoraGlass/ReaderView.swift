import SwiftUI
import AVKit

/// The reading room: a vertical AI film beside the page text. Every control is
/// real Liquid Glass; the film is media (rounded, not frosted) for legibility.
struct ReaderView: View {
    let book: KBook
    var onClose: () -> Void

    @State private var page = 0
    @State private var player = AVPlayer()

    private let pages = [
        "In the old days when wishing still helped, a king's daughter went out into the forest and sat by the cool well. She had a golden ball, her favourite plaything, and she tossed it high and caught it in the warm afternoon light.",
        "But the ball slipped past her fingers and rolled into the dark water, down and down until it vanished. The princess began to cry — and a thick, ugly frog stretched its head from the water and asked why she wept so.",
        "\"I will fetch your ball,\" said the frog, \"but you must love me and let me be your companion — eat from your plate and sleep upon your pillow.\" In her grief she promised, never thinking a frog could leave the well.",
        "Yet a promise is a promise. When the frog came knocking that evening, the king bade her keep her word — and in keeping it, the enchantment broke, and the frog became a prince with kind and laughing eyes.",
    ]

    var body: some View {
        ZStack {
            Rectangle()
                .fill(.black.opacity(0.5))
                .background(.ultraThinMaterial)
                .ignoresSafeArea()
                .onTapGesture { onClose() }

            VStack(spacing: 0) {
                // Top bar — glass Back button
                HStack(spacing: 12) {
                    Button { onClose() } label: {
                        Label("Back", systemImage: "chevron.left").font(.system(size: 13, weight: .semibold))
                    }
                    .buttonStyle(.glass)
                    Text(book.title).font(.system(.headline, design: .serif)).foregroundStyle(.white)
                    Text("· \(book.author)").font(.subheadline).foregroundStyle(.white.opacity(0.55))
                    Spacer()
                }
                .padding(.horizontal, 26)
                .padding(.top, 34)
                .padding(.bottom, 8)

                HStack(alignment: .top, spacing: 42) {
                    // Vertical film (media — rounded, not glass)
                    VStack(spacing: 10) {
                        VideoPlayer(player: player)
                            .frame(width: 300, height: 533)
                            .clipShape(RoundedRectangle(cornerRadius: 24))
                            .overlay(RoundedRectangle(cornerRadius: 24).stroke(.white.opacity(0.16), lineWidth: 0.75))
                            .overlay(alignment: .topLeading) {
                                HStack(spacing: 5) {
                                    Circle().fill(.green).frame(width: 6, height: 6)
                                    Text("AI FILM").font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                                }
                                .padding(.horizontal, 9).padding(.vertical, 5)
                                .glassEffect(.regular, in: .capsule)
                                .padding(12)
                            }
                            .shadow(color: .black.opacity(0.6), radius: 32, y: 20)
                        Text("Generated with Wan · vertical short film")
                            .font(.system(size: 10)).foregroundStyle(.white.opacity(0.5))
                    }

                    // Reading text — glass card + glass page-nav
                    VStack(alignment: .leading, spacing: 16) {
                        Text("NOW READING").font(.system(size: 10, weight: .bold)).tracking(2).foregroundStyle(.white.opacity(0.5))
                        Text(book.title).font(.system(.largeTitle, design: .serif).weight(.semibold)).foregroundStyle(.white)
                        Text("by \(book.author)").font(.callout).foregroundStyle(.white.opacity(0.6))

                        Text(pages[page % pages.count])
                            .font(.system(.body, design: .serif))
                            .foregroundStyle(.white.opacity(0.9))
                            .lineSpacing(7)
                            .padding(20)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))

                        HStack {
                            Button { withAnimation(.smooth) { page = max(0, page - 1) } } label: {
                                Label("Previous", systemImage: "chevron.left").font(.system(size: 13, weight: .semibold))
                            }
                            .buttonStyle(.glass).disabled(page == 0)
                            Spacer()
                            Text("Page \(page + 1) of \(pages.count)").font(.caption).foregroundStyle(.white.opacity(0.55))
                            Spacer()
                            Button { withAnimation(.smooth) { page = min(pages.count - 1, page + 1) } } label: {
                                Label("Next", systemImage: "chevron.right").font(.system(size: 13, weight: .semibold))
                            }
                            .buttonStyle(.glass).disabled(page == pages.count - 1)
                        }
                    }
                    .frame(maxWidth: 470, alignment: .leading)
                }
                .padding(.horizontal, 46)
                .padding(.top, 12)

                Spacer()
            }
        }
        .onAppear {
            guard let u = book.filmURL else { return }
            player.replaceCurrentItem(with: AVPlayerItem(url: u))
            player.isMuted = true
            player.actionAtItemEnd = .none
            // Capture the player into a Sendable box so the loop-on-end observer doesn't
            // reach back into main-actor `self.player` from a Sendable closure (Swift 6).
            let loopingPlayer = player
            NotificationCenter.default.addObserver(
                forName: .AVPlayerItemDidPlayToEndTime,
                object: player.currentItem,
                queue: .main
            ) { _ in
                MainActor.assumeIsolated {
                    loopingPlayer.seek(to: .zero)
                    loopingPlayer.play()
                }
            }
            player.play()
        }
        .onDisappear { player.pause() }
    }
}
