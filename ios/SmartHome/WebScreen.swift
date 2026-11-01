import SwiftUI
import UIKit
import WebKit

final class WebModel: ObservableObject {
    @Published var error: String?
    @Published var loading = true
    weak var webView: WKWebView?

    func reload(url: URL) {
        error = nil
        loading = true
        if let wv = webView {
            if wv.url == nil {
                wv.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 15))
            } else {
                wv.reload()
            }
        }
    }
}

struct WebScreen: View {
    let url: URL
    @EnvironmentObject var settings: AppSettings
    @StateObject private var model = WebModel()

    var body: some View {
        ZStack {
            WebView(url: url, model: model, settings: settings)
                .ignoresSafeArea()
                .opacity(model.error == nil ? 1 : 0)

            if model.loading && model.error == nil {
                ProgressView().tint(.white)
            }

            if let error = model.error {
                VStack(spacing: 16) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 48))
                        .foregroundColor(.gray)
                    Text("Нет связи с сервером")
                        .font(.title2.bold())
                    Text(url.absoluteString)
                        .font(.footnote)
                        .foregroundColor(.gray)
                    Text(error)
                        .font(.footnote)
                        .foregroundColor(.gray)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal)
                    Button("Повторить") { model.reload(url: url) }
                        .buttonStyle(.borderedProminent)
                    Button("Адрес сервера") { settings.showServerSettings = true }
                }
                .padding()
            }
        }
    }
}

struct WebView: UIViewRepresentable {
    let url: URL
    let model: WebModel
    let settings: AppSettings

    func makeCoordinator() -> Coordinator {
        Coordinator(url: url, model: model, settings: settings)
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // cookie входа сохраняются между запусками
        config.allowsInlineMediaPlayback = true
        config.userContentController.add(WeakScriptHandler(context.coordinator), name: "app")

        let wv = WKWebView(frame: .zero, configuration: config)
        wv.navigationDelegate = context.coordinator
        wv.uiDelegate = context.coordinator
        wv.allowsLinkPreview = false
        wv.allowsBackForwardNavigationGestures = false
        wv.isOpaque = false
        wv.backgroundColor = UIColor(red: 0x0F / 255, green: 0x11 / 255, blue: 0x15 / 255, alpha: 1)
        wv.scrollView.backgroundColor = wv.backgroundColor
        wv.scrollView.contentInsetAdjustmentBehavior = .never

        let refresh = UIRefreshControl()
        refresh.tintColor = .lightGray
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.pullToRefresh(_:)), for: .valueChanged)
        wv.scrollView.refreshControl = refresh

        model.webView = wv
        wv.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 15))
        return wv
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        uiView.configuration.userContentController.removeScriptMessageHandler(forName: "app")
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let url: URL
        let model: WebModel
        let settings: AppSettings

        init(url: URL, model: WebModel, settings: AppSettings) {
            self.url = url
            self.model = model
            self.settings = settings
        }

        @objc func pullToRefresh(_ sender: UIRefreshControl) {
            guard let wv = model.webView else { sender.endRefreshing(); return }
            if model.error != nil || wv.url == nil {
                model.reload(url: url)
            } else {
                wv.evaluateJavaScript("window.smarthomeRefresh ? window.smarthomeRefresh() : location.reload()")
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { sender.endRefreshing() }
        }

        // MARK: навигация

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            model.loading = false
            model.error = nil
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            fail(error)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            fail(error)
        }

        private func fail(_ error: Error) {
            let ns = error as NSError
            if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
            model.loading = false
            model.error = error.localizedDescription
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            webView.reload()
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            // ссылки на другие сайты (например, вход в Яндекс) открываем в Safari
            if let target = navigationAction.request.url,
               navigationAction.navigationType == .linkActivated,
               target.host != url.host {
                UIApplication.shared.open(target)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let target = navigationAction.request.url {
                UIApplication.shared.open(target)
            }
            return nil
        }

        // MARK: диалоги confirm/alert из веб-интерфейса

        func webView(
            _ webView: WKWebView,
            runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping () -> Void
        ) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            present(alert, fallback: completionHandler)
        }

        func webView(
            _ webView: WKWebView,
            runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping (Bool) -> Void
        ) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Отмена", style: .cancel) { _ in completionHandler(false) })
            alert.addAction(UIAlertAction(title: "OK", style: .destructive) { _ in completionHandler(true) })
            present(alert) { completionHandler(false) }
        }

        private func present(_ alert: UIAlertController, fallback: @escaping () -> Void) {
            let root = UIApplication.shared.connectedScenes
                .compactMap { ($0 as? UIWindowScene)?.keyWindow }
                .first?.rootViewController
            var top = root
            while let presented = top?.presentedViewController { top = presented }
            if let top = top {
                top.present(alert, animated: true)
            } else {
                fallback()
            }
        }

        // MARK: мост JS -> приложение

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
            switch cmd {
            case "haptic":
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
            case "open":
                if let s = body["url"] as? String, let target = URL(string: s) {
                    UIApplication.shared.open(target)
                }
            case "settings":
                settings.showServerSettings = true
            default:
                break
            }
        }
    }
}

/// WKUserContentController держит обработчик сильной ссылкой — разрываем цикл.
final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?

    init(_ target: WKScriptMessageHandler) {
        self.target = target
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}
