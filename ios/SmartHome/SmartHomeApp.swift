import SwiftUI

/// Тонкая оболочка: весь интерфейс приходит с сервера умного дома.
/// Обновили интерфейс на сервере — приложение показывает новую версию без переустановки.
@main
struct SmartHomeApp: App {
    @StateObject private var settings = AppSettings()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(settings)
                .preferredColorScheme(.dark)
        }
    }
}

let appBackground = Color(red: 0x0F / 255, green: 0x11 / 255, blue: 0x15 / 255)

final class AppSettings: ObservableObject {
    private static let key = "serverURL"

    @Published var serverURL: String
    @Published var showServerSettings = false

    init() {
        serverURL = UserDefaults.standard.string(forKey: Self.key) ?? ""
    }

    var url: URL? { URL(string: serverURL) }

    func save(_ raw: String) {
        serverURL = Self.normalize(raw)
        UserDefaults.standard.set(serverURL, forKey: Self.key)
    }

    /// "192.168.1.10:8000" -> "http://192.168.1.10:8000"
    static func normalize(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return s }
        if !s.lowercased().hasPrefix("http://") && !s.lowercased().hasPrefix("https://") {
            s = "http://" + s
        }
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }
}

struct RootView: View {
    @EnvironmentObject var settings: AppSettings

    var body: some View {
        ZStack {
            appBackground.ignoresSafeArea()
            if let url = settings.url, !settings.serverURL.isEmpty {
                WebScreen(url: url)
                    .id(settings.serverURL)
            } else {
                ServerSettingsView(isInitial: true)
            }
        }
        .sheet(isPresented: $settings.showServerSettings) {
            ServerSettingsView(isInitial: false)
                .environmentObject(settings)
        }
    }
}
