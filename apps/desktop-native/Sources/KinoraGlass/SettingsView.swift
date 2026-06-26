import SwiftUI

/// Glass settings — toggles, slider, segmented picker and a prominent glass
/// button. Every control is real Liquid Glass; text stays white for legibility.
struct SettingsView: View {
    @State private var liveVideo = true
    @State private var autoplay = true
    @State private var reduceMotion = false
    @State private var quality = 1
    @State private var volume = 0.7
    private let gold = Color(red: 0.83, green: 0.64, blue: 0.31)

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Settings").font(.system(.title, design: .serif).weight(.semibold)).foregroundStyle(.white)

                VStack(spacing: 4) {
                    Toggle("Live AI video generation", isOn: $liveVideo)
                    Divider().overlay(.white.opacity(0.08))
                    Toggle("Autoplay films", isOn: $autoplay)
                    Divider().overlay(.white.opacity(0.08))
                    Toggle("Reduce motion", isOn: $reduceMotion)
                }
                .tint(gold)
                .foregroundStyle(.white)
                .padding(20)
                .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22))

                VStack(alignment: .leading, spacing: 16) {
                    Text("Render quality").foregroundStyle(.white)
                    Picker("Render quality", selection: $quality) {
                        Text("Draft").tag(0)
                        Text("Standard").tag(1)
                        Text("Cinematic").tag(2)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()

                    Text("Film volume").foregroundStyle(.white).padding(.top, 6)
                    Slider(value: $volume).tint(gold)

                    Button { } label: {
                        Text("Save changes").font(.system(size: 14, weight: .semibold)).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.glassProminent)
                    .tint(gold)
                    .controlSize(.large)
                    .padding(.top, 4)
                }
                .foregroundStyle(.white)
                .padding(20)
                .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22))
            }
            .frame(maxWidth: 560, alignment: .leading)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 28)
            .padding(.top, 6)
            .padding(.bottom, 130)
        }
    }
}
