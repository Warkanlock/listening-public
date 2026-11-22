import AppKit
import WebKit

/// The web app exchanges the incoming pairing token for an HttpOnly cookie.
/// Remember the origin for later launches. WebKit owns the cookie jar.
final class RoomApp: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private let address = NSTextField(string: "")
    private let invitation = NSSecureTextField(string: "")
    private let message = NSTextField(wrappingLabelWithString: "")
    private var room: RoomAddress?
    private var retryRoom: RoomAddress?
    private var pairingServer: RoomAddress?
    private lazy var connectButton = NSButton(title: "Connect", target: self, action: #selector(connect))
    private let savedRoom = "roomOrigin"
    private var openedPlayer = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let actions = NSMenu()
        appItem.submenu = actions
        for (name, action, key) in [
            ("Connect to a room…", #selector(connectScreen), "l"),
            ("Install terminal command…", #selector(installCommand), ""),
            ("Quit Listening Room", #selector(NSApplication.terminate(_:)), "q")
        ] {
            let item = actions.addItem(withTitle: name, action: action, keyEquivalent: key)
            item.target = action == #selector(NSApplication.terminate(_:)) ? NSApp : self
        }
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        menu.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        editItem.submenu = edit
        for (label, key, action) in [("Cut", "x", "cut:"), ("Copy", "c", "copy:"),
                                     ("Paste", "v", "paste:"), ("Select All", "a", "selectAll:")] {
            edit.addItem(withTitle: label, action: Selector(action), keyEquivalent: key)
        }
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 320),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Listening Room"
        window.contentMinSize = NSSize(width: 400, height: 280)
        window.isReleasedWhenClosed = false
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if let saved = UserDefaults.standard.string(forKey: savedRoom),
           let location = RoomAddress(saved) {
            openRoom(location)
        } else {
            connectScreen()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    @objc private func connectScreen() {
        let previous = room?.origin ?? pairingServer?.origin
        showForm(address: previous?.absoluteString ?? UserDefaults.standard.string(forKey: savedRoom) ?? "")
    }

    private func showForm(address value: String = "", pairing server: RoomAddress? = nil,
                          problem: String = "", retry: RoomAddress? = nil) {
        web?.stopLoading()
        web?.navigationDelegate = nil
        web = nil
        room = nil
        pairingServer = server
        retryRoom = retry
        window.title = "Listening Room"
        message.stringValue = problem
        connectButton.title = server != nil ? "Pair device" : retry != nil ? "Retry" : "Connect"
        message.textColor = .systemRed
        let field: NSTextField = server == nil ? address : invitation
        let label = server == nil ? "Server URL" : "Pairing link"
        field.stringValue = value
        field.placeholderString = server == nil ? "http://your-server:8765" : nil
        field.setAccessibilityLabel(label)
        field.controlSize = .large
        field.target = self
        field.action = #selector(connect)

        let heading = NSTextField(labelWithString: server == nil ? "Connect to a room" : "Pair this device")
        heading.font = .systemFont(ofSize: 27, weight: .semibold)
        let hint = NSTextField(wrappingLabelWithString: server.map {
            "Paste a pairing link for \($0.origin.absoluteString) from Settings → Devices."
        } ?? "Enter the URL you use to reach Listening Room on Wi-Fi or Tailscale. You can also paste a pairing link.")
        hint.textColor = .secondaryLabelColor
        let fieldLabel = NSTextField(labelWithString: label)
        fieldLabel.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .medium)
        connectButton.bezelStyle = .rounded
        connectButton.controlSize = .large
        connectButton.keyEquivalent = "\r"
        let stack = NSStackView(views: [heading, hint, fieldLabel, field, connectButton, message])
        if server != nil {
            stack.addArrangedSubview(NSButton(title: "Change server URL", target: self, action: #selector(connectScreen)))
        }
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.setCustomSpacing(22, after: hint)
        stack.setCustomSpacing(14, after: field)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let view = NSView()
        view.addSubview(stack)
        let preferred = stack.widthAnchor.constraint(equalToConstant: 450)
        preferred.priority = .defaultHigh
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 25),
            preferred,
            field.widthAnchor.constraint(equalTo: stack.widthAnchor),
            hint.widthAnchor.constraint(equalTo: stack.widthAnchor),
            message.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        window.contentView = view
        window.makeFirstResponder(field)
    }

    @objc private func connect() {
        // Replacing the form ends editing and can send the text field action again.
        guard web == nil else { return }
        let value = pairingServer == nil ? address.stringValue : invitation.stringValue
        guard let newRoom = RoomAddress(value) else {
            message.stringValue = pairingServer == nil ? "Enter a full server URL starting with http:// or https://."
                : "Paste the full pairing link from Devices settings."
            return
        }
        if let pairingServer, !newRoom.hasInvitation || !pairingServer.contains(newRoom.origin) {
            message.stringValue = "Use a pairing link for this server, or change the server URL."
            return
        }
        if let retryRoom, !newRoom.hasInvitation, retryRoom.contains(newRoom.origin) {
            openRoom(retryRoom)
        } else {
            openRoom(newRoom)
        }
    }

    private func openRoom(_ location: RoomAddress) {
        window.title = "Connecting to Listening Room…"
        room = location
        retryRoom = location
        pairingServer = nil
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = "ListeningRoomMac"
        config.mediaTypesRequiringUserActionForPlayback = []
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        window.contentView = web
        web.load(URLRequest(url: location.invitation, timeoutInterval: 12))
        address.stringValue = ""
        invitation.stringValue = ""
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard navigationAction.targetFrame?.isMainFrame != false else {
            decisionHandler(.allow)
            return
        }
        if navigationAction.navigationType == .linkActivated, navigationAction.sourceFrame.isMainFrame,
           room?.contains(navigationAction.sourceFrame.request.url) == true,
           let url = navigationAction.request.url, let scheme = url.scheme?.lowercased(),
           ["https", "http"].contains(scheme), url.host != nil, url.user == nil, url.password == nil,
           navigationAction.targetFrame == nil || room?.contains(url) != true {
            decisionHandler(.cancel)
            NSWorkspace.shared.open(url)
            return
        }
        if room?.contains(navigationAction.request.url) == true {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if response.isForMainFrame, room?.contains(response.response.url) != true {
            decisionHandler(.cancel)
            return
        }
        if response.isForMainFrame, let http = response.response as? HTTPURLResponse, http.statusCode >= 400 {
            decisionHandler(.cancel)
            if http.statusCode == 401 {
                showForm(pairing: room, problem: room?.hasInvitation == true ? "This pairing link is invalid or expired." : "")
            } else {
                recovery("The room returned HTTP \(http.statusCode). Try again.")
            }
        } else {
            decisionHandler(.allow)
        }
    }

    private func recovery(_ problem: String) {
        showForm(address: room?.origin.absoluteString ?? "", problem: problem, retry: retryRoom)
    }

    private func failed(_ error: Error) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        recovery("Room unreachable. Check your private network connection, then try again.")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) { failed(error) }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!,
                 withError error: Error) { failed(error) }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        window.title = "Listening Room"
        if !openedPlayer {
            window.setContentSize(NSSize(width: 1040, height: 720))
            window.center()
            openedPlayer = true
        }
        if let room, room.contains(webView.url), webView.url?.query == nil {
            retryRoom = RoomAddress(room.origin.absoluteString)
            UserDefaults.standard.set(room.origin.absoluteString, forKey: savedRoom)
        }
    }

    @objc private func installCommand() {
        let alert = NSAlert()
        do {
            let fm = FileManager.default
            let app = Bundle.main.bundleURL
            let volume = try app.resourceValues(forKeys: [.volumeIsReadOnlyKey])
            guard volume.volumeIsReadOnly == false, !app.path.contains("/AppTranslocation/") else {
                throw NSError(domain: "ListeningRoom", code: 1, userInfo: [NSLocalizedDescriptionKey:
                    "Drag Listening Room.app to Applications and open it there before installing the command."])
            }
            let executable = app.appendingPathComponent("Contents/Resources/bin/listening")
            let directory = fm.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin")
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            let link = directory.appendingPathComponent("listening")
            if (try? fm.destinationOfSymbolicLink(atPath: link.path)) != executable.path {
                try fm.createSymbolicLink(at: link, withDestinationURL: executable)
            }
            alert.messageText = "Terminal command installed"
            alert.informativeText = "Run listening --help. If your shell cannot find it, add ~/.local/bin to your PATH."
        } catch {
            alert.messageText = "Couldn’t install the command"
            alert.informativeText = error.localizedDescription
        }
        alert.beginSheetModal(for: window)
    }
}

let application = NSApplication.shared
let delegate = RoomApp()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
