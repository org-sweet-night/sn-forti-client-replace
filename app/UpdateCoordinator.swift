import AppKit

enum UpdateConfiguration {
    static func isValidPackage(_ asset: URL?, feed: String?) -> Bool {
        guard let asset, let feed, let source = URLComponents(string: feed),
              let target = URLComponents(url: asset, resolvingAgainstBaseURL: false),
              target.scheme == "https", target.host == "github.com", target.user == nil,
              target.password == nil, target.port == nil, target.query == nil, target.fragment == nil else { return false }
        let expected = source.path.split(separator: "/")
        let parts = target.path.split(separator: "/")
        return expected.count >= 2 && parts.count == 6 && parts[0] == expected[0] && parts[1] == expected[1]
            && parts[2] == "releases" && parts[3] == "download" && parts[5].hasSuffix(".pkg")
    }

    static func isValid(_ info: [String: Any]) -> Bool {
        guard let feed = info["SUFeedURL"] as? String,
              let url = URLComponents(string: feed), url.scheme == "https", url.host == "github.com",
              url.user == nil, url.password == nil, url.port == nil, url.query == nil, url.fragment == nil,
              let key = info["SUPublicEDKey"] as? String,
              let decoded = Data(base64Encoded: key), decoded.count == 32,
              decoded.base64EncodedString() == key else { return false }
        let parts = url.path.split(separator: "/", omittingEmptySubsequences: true)
        guard parts.count >= 6, parts[2] == "releases", parts.last == "appcast.xml" else { return false }
        return parts[3] == "download" || (parts.count == 6 && parts[3] == "latest" && parts[4] == "download")
    }

    static func selfTest() {
        let key = Data(repeating: 1, count: 32).base64EncodedString()
        let valid: [String: Any] = ["SUFeedURL": "https://github.com/example/vpn/releases/download/updates/appcast.xml", "SUPublicEDKey": key]
        precondition(isValid(valid))
        var latest = valid; latest["SUFeedURL"] = "https://github.com/org-sweet-night/sn-forti-client-replace/releases/latest/download/appcast.xml"
        precondition(isValid(latest))
        let feed = latest["SUFeedURL"] as? String
        precondition(isValidPackage(URL(string: "https://github.com/org-sweet-night/sn-forti-client-replace/releases/download/v1/SN-VPN.pkg"), feed: feed))
        for asset in ["https://github.com/other/sn-forti-client-replace/releases/download/v1/SN-VPN.pkg", "https://github.com/org-sweet-night/sn-forti-client-replace/releases/download/v1/SN-VPN.zip", "https://evil.test/org-sweet-night/sn-forti-client-replace/releases/download/v1/SN-VPN.pkg", "https://github.com/org-sweet-night/sn-forti-client-replace/releases/download/v1/SN-VPN.pkg?token=secret"] {
            precondition(!isValidPackage(URL(string: asset), feed: feed))
        }
        for feed in ["http://github.com/example/vpn/releases/download/updates/appcast.xml", "https://github.com.evil.test/example/vpn/releases/download/updates/appcast.xml", "https://user@github.com/example/vpn/releases/download/updates/appcast.xml", "https://github.com/example/vpn/releases/download/updates/appcast.xml?token=secret"] {
            var bad = valid; bad["SUFeedURL"] = feed; precondition(!isValid(bad))
        }
        var bad = valid; bad["SUPublicEDKey"] = "invalid"; precondition(!isValid(bad))
        precondition(!isValid([:]))
    }
}

@MainActor
final class UpdateCoordinator {
    static let shared = UpdateCoordinator()
    private var opening = false
    private var pendingManual = false

    func check(manually: Bool) {
        guard Bundle.main.bundleURL.path == "/Applications/SN VPN.app" else {
            if manually { showError("请从“应用程序”中的 SN VPN 检查更新。开发构建不执行安装更新。") }
            return
        }
        guard UpdateConfiguration.isValid(Bundle.main.infoDictionary ?? [:]) else {
            if manually { showError("自动更新尚未配置。请联系发布者提供更新源和签名公钥。") }
            return
        }
        guard !opening else { pendingManual = pendingManual || manually; return }
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/SN VPN Updater.app")
        if !manually, NSWorkspace.shared.runningApplications.contains(where: { $0.bundleURL == url }) { return }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = manually
        configuration.arguments = [manually ? "--manual" : "--background"]
        opening = true
        NSWorkspace.shared.openApplication(at: url, configuration: configuration) { _, error in
            DispatchQueue.main.async {
                self.opening = false
                if let error, manually { self.showError("无法启动更新器。\(error.localizedDescription.prefix(220))") }
                if self.pendingManual { self.pendingManual = false; self.check(manually: true) }
            }
        }
    }

    private func showError(_ message: String) {
        let alert = NSAlert(); alert.messageText = "检查更新"; alert.informativeText = message
        alert.addButton(withTitle: "好"); alert.runModal()
    }
}
