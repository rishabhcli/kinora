import SwiftUI
import KinoraGlassKit

/// A glass overlay shown while the shell is connecting to the renderer, and the
/// "couldn't reach the renderer" state with a Retry / Use Showcase choice. Real
/// `.glassEffect`, so even the loading state demonstrates Liquid Glass.
struct ConnectionOverlay: View {
    let phase: ShellViewModel.Phase
    var onRetry: () -> Void
    var onUseShowcase: () -> Void

    private let gold = Color(red: 0.83, green: 0.64, blue: 0.31)

    var body: some View {
        VStack(spacing: 18) {
            switch phase {
            case .connecting(let endpoint):
                ProgressView()
                    .controlSize(.large)
                    .tint(gold)
                Text("Connecting to the renderer")
                    .font(.system(.headline, design: .serif))
                    .foregroundStyle(.white)
                Text(endpoint.label)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.55))
                Text("Start it with `make app-desktop-dev`")
                    .font(.system(size: 11))
                    .foregroundStyle(.white.opacity(0.4))

            case .fallback(let reason):
                Image(systemName: "wifi.exclamationmark")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(gold)
                Text("Renderer unavailable")
                    .font(.system(.title3, design: .serif).weight(.semibold))
                    .foregroundStyle(.white)
                Text(reason)
                    .font(.system(size: 11))
                    .foregroundStyle(.white.opacity(0.5))
                    .multilineTextAlignment(.center)
                HStack(spacing: 12) {
                    Button(action: onRetry) {
                        Label("Retry", systemImage: "arrow.clockwise").font(.system(size: 13, weight: .semibold))
                    }
                    .buttonStyle(.glassProminent).tint(gold)
                    Button(action: onUseShowcase) {
                        Label("Use Showcase", systemImage: "sparkles").font(.system(size: 13, weight: .semibold))
                    }
                    .buttonStyle(.glass)
                }
                .padding(.top, 4)

            default:
                EmptyView()
            }
        }
        .padding(34)
        .frame(width: 360)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 26))
        .shadow(color: .black.opacity(0.4), radius: 30, y: 16)
    }
}
