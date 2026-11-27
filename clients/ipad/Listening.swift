import AVFAudio
import SwiftUI
import WebKit

private let savedRoom = "roomOrigin"
private let switchRoom = Notification.Name("SwitchListening")

final class RoomScene: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options: UIScene.ConnectionOptions) {
        guard let scene = scene as? UIWindowScene else { return }
        let startAtForm = options.shortcutItem?.type == "changeRoom"
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UIHostingController(rootView: RoomScreen(startAtForm: startAtForm))
        window.makeKeyAndVisible()
        self.window = window
    }

    func windowScene(_ windowScene: UIWindowScene, performActionFor shortcutItem: UIApplicationShortcutItem,
                     completionHandler: @escaping (Bool) -> Void) {
        if shortcutItem.type == "changeRoom" { NotificationCenter.default.post(name: switchRoom, object: nil) }
        completionHandler(shortcutItem.type == "changeRoom")
    }
}

@main
final class RoomApp: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        try? AVAudioSession.sharedInstance().setCategory(.playback)
        return true
    }

    func application(_ application: UIApplication, configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: nil, sessionRole: session.role)
        config.delegateClass = RoomScene.self
        return config
    }
}

private struct RoomScreen: View {
    @State private var room: RoomAddress?
    @State private var address = ""
    @State private var problem = ""
    @State private var retryRoom: RoomAddress?
    @State private var pairingServer: RoomAddress?
    private let accent = Color(red: 0.15, green: 0.42, blue: 0.37)

    init(startAtForm: Bool) {
        let saved = UserDefaults.standard.string(forKey: savedRoom) ?? ""
        _room = State(initialValue: startAtForm ? nil : RoomAddress(saved))
        _address = State(initialValue: startAtForm ? saved : "")
    }

    var body: some View {
        Group {
            if let room {
                RoomPlayer(room: room) { problem, retry, expired in
                    openForm(problem, retry: retry, expired: expired)
                }
                .ignoresSafeArea(.container, edges: UIDevice.current.userInterfaceIdiom == .pad ? .bottom : [])
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        Image(systemName: "waveform").font(.largeTitle).foregroundStyle(accent)
                            .accessibilityHidden(true)
                        Text(pairingServer == nil ? "Connect to a room" : "Pair this device").font(.largeTitle.bold())
                        if let pairingServer {
                            Text(pairingServer.origin.absoluteString)
                            Text("Paste a pairing link from Settings → Devices on this server.")
                            SecureField("Pairing link", text: $address)
                                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder).submitLabel(.go).onSubmit(connect)
                        } else {
                            Text("Enter the URL you use to reach Listening on Wi-Fi or Tailscale. You can also paste a pairing link.")
                            Text("Server URL").font(.headline)
                            TextField("http://your-server:8765", text: $address)
                                .accessibilityLabel("Server URL")
                                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder).submitLabel(.go).onSubmit(connect)
                        }
                        if !problem.isEmpty { Text(problem).foregroundStyle(.red) }
                        Button(pairingServer != nil ? "Pair device" : retryRoom == nil ? "Connect" : "Retry",
                               action: connect).buttonStyle(.borderedProminent)
                            .controlSize(.large).tint(accent)
                        if let pairingServer {
                            Button("Change server URL") {
                                address = pairingServer.origin.absoluteString
                                self.pairingServer = nil
                                problem = ""
                            }
                        }
                    }.frame(maxWidth: 440, alignment: .leading).padding(24)
                }.frame(maxWidth: .infinity).defaultScrollAnchor(.center)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: switchRoom)) { _ in openForm("") }
    }

    private func connect() {
        guard let newRoom = RoomAddress(address) else {
            problem = pairingServer == nil ? "Enter a full server URL starting with http:// or https://."
                : "Paste the full pairing link from Devices settings."
            return
        }
        if let pairingServer, !newRoom.hasInvitation || !pairingServer.contains(newRoom.origin) {
            problem = "Use a pairing link for this server, or change the server URL."
            return
        }
        room = if let retryRoom, !newRoom.hasInvitation, retryRoom.contains(newRoom.origin) { retryRoom } else { newRoom }
        address = ""
        problem = ""
        retryRoom = nil
        pairingServer = nil
    }

    private func openForm(_ message: String, retry: RoomAddress? = nil, expired: Bool = false) {
        pairingServer = expired ? room : nil
        address = expired ? "" : (room?.origin.absoluteString ?? UserDefaults.standard.string(forKey: savedRoom) ?? "")
        room = nil
        problem = message
        retryRoom = retry
    }
}

private struct RoomPlayer: UIViewRepresentable {
    let room: RoomAddress
    let failed: (String, RoomAddress?, Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(room: room, failed: failed) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--isolated-pairing-test") {
            config.websiteDataStore = .nonPersistent()
        }
        #endif
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        if UIDevice.current.userInterfaceIdiom == .pad {
            // The native room keeps its top and side insets. Let the web dock paint its bottom inset.
            let fitViewport = """
                const viewport = document.querySelector('meta[name="viewport"]');
                if (viewport && !viewport.content.includes('viewport-fit')) {
                    viewport.content += ', viewport-fit=cover';
                }
                """
            config.userContentController.addUserScript(WKUserScript(
                source: fitViewport, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        }
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.load(URLRequest(url: room.invitation, timeoutInterval: 12))
        return web
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    static func dismantleUIView(_ web: WKWebView, coordinator: Coordinator) {
        web.navigationDelegate = nil
        web.stopLoading()
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        let room: RoomAddress
        let failed: (String, RoomAddress?, Bool) -> Void
        private var retryAddress: RoomAddress

        init(room: RoomAddress, failed: @escaping (String, RoomAddress?, Bool) -> Void) {
            self.room = room
            self.failed = failed
            retryAddress = room
        }

        private func rememberOrigin() {
            if let origin = RoomAddress(room.origin.absoluteString) {
                retryAddress = origin
            }
        }

        func webView(_ web: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let url = action.request.url
            let leavesRoom = action.targetFrame == nil || !room.contains(url)
            if action.navigationType == .linkActivated, action.sourceFrame.isMainFrame,
               room.contains(action.sourceFrame.request.url), leavesRoom,
               let url, ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
               url.user == nil, url.password == nil {
                decisionHandler(.cancel)
                UIApplication.shared.open(url)
                return
            }
            let embedded = action.targetFrame?.isMainFrame == false
            if !embedded && room.contains(url) && url?.query == nil { rememberOrigin() }
            decisionHandler(embedded || (action.targetFrame != nil && room.contains(url)) ? .allow : .cancel)
        }

        func webView(_ web: WKWebView, decidePolicyFor response: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            guard response.isForMainFrame else { decisionHandler(.allow); return }
            guard room.contains(response.response.url) else { decisionHandler(.cancel); return }
            if let http = response.response as? HTTPURLResponse, http.statusCode >= 400 {
                decisionHandler(.cancel)
                let expired = http.statusCode == 401
                failed(expired ? (room.hasInvitation ? "This pairing link is invalid or expired." : "") :
                        "The room returned HTTP \(http.statusCode). Try again.",
                       expired ? nil : retryAddress, expired)
            } else {
                decisionHandler(.allow)
            }
        }

        func webView(_ web: WKWebView, didFinish navigation: WKNavigation!) {
            if room.contains(web.url), web.url?.query == nil {
                rememberOrigin()
                UserDefaults.standard.set(room.origin.absoluteString, forKey: savedRoom)
            }
        }

        private func offline(_ error: Error) {
            guard (error as NSError).code != NSURLErrorCancelled else { return }
            failed("Room unreachable. Check your private network connection, then try again.", retryAddress, false)
        }

        func webView(_ web: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { offline(error) }
        func webView(_ web: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { offline(error) }
        func webViewWebContentProcessDidTerminate(_ web: WKWebView) { web.reload() }
    }
}
