import Foundation

/// Keep the saved server origin separate from the invitation used to open it.
struct RoomAddress {
    let invitation: URL
    let origin: URL
    var hasInvitation: Bool { invitation.query != nil }

    init?(_ text: String) {
        guard var parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/" || (parts.path == "/pair" && parts.query != nil),
              (parts.queryItems ?? []).allSatisfy({ $0.name == "token" && $0.value?.isEmpty == false }),
              parts.query == nil || parts.queryItems?.count == 1,
              parts.port.map({ (1...65535).contains($0) }) ?? true else { return nil }
        parts.scheme = scheme
        parts.host = host.lowercased()
        if parts.path.isEmpty { parts.path = "/" }
        guard let invitation = parts.url else { return nil }
        parts.path = "/"
        parts.query = nil
        guard let origin = parts.url else { return nil }
        self.invitation = invitation
        self.origin = origin
    }

    func contains(_ url: URL?) -> Bool {
        guard let url, let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.user == nil, parts.password == nil,
              url.scheme?.lowercased() == origin.scheme,
              url.host?.lowercased() == origin.host else { return false }
        let defaultPort = origin.scheme == "https" ? 443 : 80
        return (url.port ?? defaultPort) == (origin.port ?? defaultPort)
    }
}
