import SwiftUI
import KinoraGlassKit

/// The macOS Settings (⌘,) scene — glass preferences for the *shell* (chrome), distinct
/// from the renderer's in-page reading settings. Persisted through `ShellSettingsStore`.
struct ShellPreferencesView: View {
    @State private var settings: ShellSettings
    private let store: ShellSettingsStore
    private let gold = Color(red: 0.83, green: 0.64, blue: 0.31)

    init(store: ShellSettingsStore = ShellSettingsStore(store: UserDefaults.standard)) {
        self.store = store
        _settings = State(initialValue: store.load())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Shell Preferences")
                .font(.system(.title2, design: .serif).weight(.semibold))
                .foregroundStyle(.white)

            VStack(alignment: .leading, spacing: 16) {
                Picker("Glass intensity", selection: $settings.glassIntensity) {
                    Text("Regular").tag(ShellSettings.GlassIntensity.regular)
                    Text("Clear").tag(ShellSettings.GlassIntensity.clear)
                    Text("Prominent").tag(ShellSettings.GlassIntensity.prominent)
                }
                .pickerStyle(.segmented)

                Toggle("Reopen last book on launch", isOn: $settings.reopenLastBook)
                Toggle("Show live crew-activity strip", isOn: $settings.showActivityStrip)
                Toggle("Respect Reduce Motion", isOn: $settings.respectReduceMotion)
            }
            .tint(gold)
            .foregroundStyle(.white)
            .padding(20)
            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        }
        .padding(28)
        .frame(width: 460)
        .background(KinoraBackground())
        .onChange(of: settings) { _, new in store.save(new) }
    }
}
