import SwiftUI

struct ServerSettingsView: View {
    let isInitial: Bool
    @EnvironmentObject var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    @State private var address = ""
    @State private var checking = false
    @State private var result: String?
    @State private var ok = false

    var body: some View {
        NavigationView {
            Form {
                Section {
                    TextField("http://192.168.1.10:8000", text: $address)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Адрес сервера")
                } footer: {
                    Text("IP или домен сервера умного дома и порт. Можно без http:// — он добавится сам. Работает по обычному HTTP.")
                }

                Section {
                    Button {
                        Task { await check() }
                    } label: {
                        HStack {
                            Text("Проверить и сохранить")
                            Spacer()
                            if checking { ProgressView() }
                        }
                    }
                    .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty || checking)

                    if result != nil && !ok {
                        Button("Сохранить без проверки") { save() }
                    }
                }

                if let result = result {
                    Section {
                        Label(result, systemImage: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .foregroundColor(ok ? .green : .orange)
                    }
                }
            }
            .navigationTitle(isInitial ? "Умный дом" : "Сервер")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    if !isInitial {
                        Button("Закрыть") { dismiss() }
                    }
                }
            }
        }
        .navigationViewStyle(.stack)
        .onAppear { address = settings.serverURL }
    }

    private func save() {
        settings.save(address)
        if !isInitial { dismiss() }
    }

    @MainActor
    private func check() async {
        let normalized = AppSettings.normalize(address)
        guard let url = URL(string: normalized + "/api/ping") else {
            result = "Неверный адрес"
            ok = false
            return
        }
        checking = true
        defer { checking = false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 6
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if status == 200, json?["ok"] as? Bool == true {
                ok = true
                result = "Сервер доступен, версия \(json?["version"] as? String ?? "?")"
                save()
            } else {
                ok = false
                result = "Сервер ответил, но это не умный дом (код \(status))"
            }
        } catch {
            ok = false
            result = error.localizedDescription
        }
    }
}
