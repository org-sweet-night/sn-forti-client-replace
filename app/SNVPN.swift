import AppKit
import Foundation
import CoreFoundation
import Darwin
import ServiceManagement

struct Profile: Codable, Equatable, Identifiable {
    var id: String
    var name: String
    var server: String
    var port: Int
    var username: String
}
struct ProfileStore: Codable, Equatable {
    var version = 1
    var selectedId: String?
    var profiles: [Profile] = []
}
private struct LegacyProfile: Decodable { var name: String; var server: String; var port: Int; var username: String }

enum VPNState: String { case connected, connecting, reconnecting, stopped, degraded, error }
struct StatusReply {
    let state: VPNState
    let reachable: Bool
    let autostart: Bool
    let message: String
    let profileID: String?
    var title: String {
        switch state {
        case .connected: return reachable ? "已连接" : "连接异常"
        case .connecting: return "连接中"
        case .reconnecting: return "正在恢复连接"
        case .stopped: return "已关闭"
        case .degraded: return "连接异常"
        case .error: return "状态不可用"
        }
    }
    var color: NSColor {
        switch state {
        case .connected where reachable: return .systemGreen
        case .connecting, .reconnecting: return .systemOrange
        case .stopped: return .secondaryLabelColor
        case .connected, .degraded, .error: return .systemRed
        }
    }
    static func parse(_ data: Data) throws -> StatusReply {
        guard let o = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let raw = o["state"] as? String, let state = VPNState(rawValue: raw),
              let reachable = o["reachable"] as? Bool, let autostart = o["autostart"] as? Bool,
              let message = o["message"] as? String else { throw AppError.invalidReply }
        if let id = o["profile_id"], !(id is NSNull), !(id is String) { throw AppError.invalidReply }
        return StatusReply(state: state, reachable: reachable, autostart: autostart, message: message,
                           profileID: o["profile_id"] as? String)
    }
}

enum AppError: LocalizedError {
    case invalidReply, invalidProfile, invalidStore, failed(String), timeout
    var errorDescription: String? {
        switch self {
        case .invalidReply: return "服务返回了无法识别的结果，请刷新后重试。"
        case .invalidProfile: return "请检查连接名称、服务器、端口和用户名。"
        case .invalidStore: return "本地连接列表无效，未覆盖原配置。请检查配置文件。"
        case .failed(let message): return message
        case .timeout: return "操作超时，请刷新连接状态后重试。"
        }
    }
}

enum ProfileStorage {
    static let directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/SN VPN")
    static let url = directory.appendingPathComponent("profiles.json")
    static let legacyURL = directory.appendingPathComponent("profile.json")

    static func validate(_ p: Profile) throws {
        func clean(_ s: String, _ max: Int) -> Bool {
            !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && s.count <= max &&
            !s.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
        }
        let validID = p.id == "primary" || (UUID(uuidString: p.id) != nil && p.id == p.id.lowercased())
        guard validID, clean(p.name, 128), clean(p.server, 253), clean(p.username, 256),
              (1...65535).contains(p.port), !p.username.hasPrefix("-"),
              p.server.range(of: #"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"#, options: .regularExpression) != nil,
              p.server.split(separator: ".", omittingEmptySubsequences: false).allSatisfy({ !$0.isEmpty && $0.count <= 63 && !$0.hasPrefix("-") && !$0.hasSuffix("-") }) else { throw AppError.invalidProfile }
    }
    static func validate(_ store: ProfileStore) throws {
        guard store.version == 1 else { throw AppError.invalidStore }
        var ids = Set<String>()
        var names = Set<String>()
        for p in store.profiles {
            try validate(p)
            guard ids.insert(p.id).inserted else { throw AppError.invalidStore }
            let normalized = p.name.trimmingCharacters(in: .whitespacesAndNewlines).folding(options: [.caseInsensitive, .diacriticInsensitive], locale: Locale(identifier: "en_US_POSIX"))
            guard names.insert(normalized).inserted else { throw AppError.failed("连接名称已存在，请使用其他名称。") }
        }
        if let selected = store.selectedId, !ids.contains(selected) { throw AppError.invalidStore }
    }
    static func load(profilesURL: URL = url, legacyURL: URL = legacyURL) throws -> ProfileStore {
        let fm = FileManager.default
        if fm.fileExists(atPath: profilesURL.path) {
            do { let value = try JSONDecoder().decode(ProfileStore.self, from: Data(contentsOf: profilesURL)); try validate(value); return value }
            catch { throw AppError.invalidStore }
        }
        guard fm.fileExists(atPath: legacyURL.path) else { return ProfileStore() }
        do {
            let old = try JSONDecoder().decode(LegacyProfile.self, from: Data(contentsOf: legacyURL))
            let profile = Profile(id: "primary", name: old.name, server: old.server, port: old.port, username: old.username)
            try validate(profile)
            let migrated = ProfileStore(selectedId: profile.id, profiles: [profile])
            try save(migrated, to: profilesURL)
            return migrated
        } catch { throw AppError.invalidStore }
    }
    static func save(_ value: ProfileStore, to target: URL = url) throws {
        try validate(value)
        let fm = FileManager.default, dir = target.deletingLastPathComponent()
        try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
        let temp = dir.appendingPathComponent(".profiles-\(UUID().uuidString).tmp")
        let data = try JSONEncoder().encode(value)
        guard fm.createFile(atPath: temp.path, contents: data, attributes: [.posixPermissions: 0o600]) else { throw AppError.failed("无法保存连接列表。") }
        defer { try? fm.removeItem(at: temp) }
        let fd = Darwin.open(temp.path, O_RDONLY)
        guard fd >= 0 else { throw AppError.failed("无法安全保存连接列表。") }
        defer { Darwin.close(fd) }
        guard Darwin.fchmod(fd, S_IRUSR | S_IWUSR) == 0, Darwin.fsync(fd) == 0 else { throw AppError.failed("无法安全保存连接列表。") }
        let result = temp.path.withCString { source in target.path.withCString { destination in Darwin.rename(source, destination) } }
        guard result == 0 else { throw AppError.failed("无法保存连接列表。") }
    }
    static func selected(in store: ProfileStore) -> Profile? { store.profiles.first { $0.id == store.selectedId } }
    static func add(_ profile: Profile, to original: ProfileStore) throws -> ProfileStore {
        try validate(profile); var next = original
        guard !next.profiles.contains(where: { $0.id == profile.id }) else { throw AppError.invalidStore }
        next.profiles.append(profile); next.selectedId = profile.id; try validate(next); return next
    }
    static func replace(_ profile: Profile, in original: ProfileStore) throws -> ProfileStore {
        try validate(profile); var next = original
        guard let i = next.profiles.firstIndex(where: { $0.id == profile.id }) else { throw AppError.invalidStore }
        next.profiles[i] = profile; try validate(next); return next
    }
    static func select(_ id: String?, in original: ProfileStore) throws -> ProfileStore {
        var next = original; next.selectedId = id; try validate(next); return next
    }
    static func remove(_ id: String, from original: ProfileStore) throws -> ProfileStore {
        var next = original; next.profiles.removeAll { $0.id == id }
        if next.selectedId == id { next.selectedId = next.profiles.first?.id }
        try validate(next); return next
    }
}

enum FortiImport {
    static let url = URL(fileURLWithPath: "/Library/Application Support/Fortinet/FortiClient/conf/vpn.plist")
    struct Result { var profiles: [Profile]; var skipped: Int }
    static func load(from source: URL = url) throws -> Result {
        guard FileManager.default.fileExists(atPath: source.path) else {
            throw AppError.failed("未找到 FortiClient 配置。可直接新建连接，无需安装 FortiClient。")
        }
        do { return try parse(Data(contentsOf: source)) }
        catch let error as AppError { throw error }
        catch { throw AppError.failed("无法读取 FortiClient 配置。可直接新建连接。") }
    }
    static func parse(_ data: Data) throws -> Result {
        guard let root = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              let entries = root["Profiles"] as? [String: Any] else {
            throw AppError.failed("FortiClient 配置格式无效。可直接新建连接。")
        }
        var result = Result(profiles: [], skipped: 0)
        for name in entries.keys.sorted() {
            guard let entry = entries[name] as? [String: Any],
                  let kind = entry["VpnType"] as? NSNumber, CFGetTypeID(kind) != CFBooleanGetTypeID(), kind.doubleValue == 0,
                  let server = entry["Server"] as? String, let user = entry["User"] as? String,
                  let rawPort = entry["ServerPort"],
                  !((rawPort as? NSNumber).map { CFGetTypeID($0) == CFBooleanGetTypeID() } ?? false),
                  let port = Int(String(describing: rawPort)) else { result.skipped += 1; continue }
            let profile = Profile(id: UUID().uuidString.lowercased(), name: name, server: server, port: port, username: user)
            guard (try? ProfileStorage.validate(profile)) != nil else { result.skipped += 1; continue }
            result.profiles.append(profile)
        }
        return result
    }
}

enum Service {
    static var actionCallCount = 0
    static let control = "/Library/Application Support/Local VPN/control.py"
    static let python = "/usr/bin/python3"
    static func run(_ args: [String], timeout: TimeInterval, completion: @escaping (Result<Data, Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(), output = Pipe()
            process.executableURL = URL(fileURLWithPath: args[0]); process.arguments = Array(args.dropFirst())
            process.standardOutput = output; process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                let group = DispatchGroup(), lock = NSLock(); var captured = Data(); group.enter()
                DispatchQueue.global().async { let d = output.fileHandleForReading.readDataToEndOfFile(); lock.lock(); captured = d; lock.unlock(); group.leave() }
                if group.wait(timeout: .now() + timeout) == .timedOut { process.terminate(); process.waitUntilExit(); completion(.failure(AppError.timeout)); return }
                process.waitUntilExit(); lock.lock(); let data = captured; lock.unlock()
                guard process.terminationStatus == 0 else { completion(.failure(AppError.failed("操作未完成，请检查配置或稍后重试。"))); return }
                completion(.success(data))
            } catch { completion(.failure(AppError.failed("无法启动本机 VPN 服务。"))) }
        }
    }
    static func status(_ done: @escaping (Result<StatusReply, Error>) -> Void) {
        run([python, "-I", control, "status"], timeout: 6) { result in done(result.flatMap { data in Result { try StatusReply.parse(data) } }) }
    }
    static func action(_ operation: String, payload: [String: Any]?, done: @escaping (Result<String, Error>) -> Void) {
        actionCallCount += 1
        let data: Data?
        do {
            data = try payload.map { try JSONSerialization.data(withJSONObject: $0) }
        } catch { done(.failure(error)); return }
        VPNHelperClient.request(operation, payload: data) { result in
            done(result.flatMap { data in
                do {
                    guard let o = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                          let ok = o["ok"] as? Bool, let message = o["message"] as? String else { throw AppError.invalidReply }
                    guard ok else { throw AppError.failed(message) }; return .success(message)
                } catch { return .failure(error) }
            })
        }
    }
}

final class DotView: NSView {
    var color: NSColor = .secondaryLabelColor { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) { color.setFill(); NSBezierPath(ovalIn: bounds.insetBy(dx: 2, dy: 2)).fill() }
}
final class SheetBackgroundView: NSView {
    override var isOpaque: Bool { true }
    override func draw(_ dirtyRect: NSRect) { NSColor.white.setFill(); bounds.fill() }
}

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTextFieldDelegate {
    static let navy = NSColor(srgbRed: 31 / 255.0, green: 44 / 255.0, blue: 61 / 255.0, alpha: 1)
    static let blue = NSColor(srgbRed: 36 / 255.0, green: 115 / 255.0, blue: 200 / 255.0, alpha: 1)
    static let canvas = NSColor(srgbRed: 245 / 255.0, green: 247 / 255.0, blue: 250 / 255.0, alpha: 1)
    static let gold = NSColor(srgbRed: 212 / 255.0, green: 175 / 255.0, blue: 55 / 255.0, alpha: 1)

    var window: NSWindow!, statusItem: NSStatusItem!, timer: Timer?
    var store = ProfileStore(), storeError: Error?, lastStatus: StatusReply?
    var busy = false, preview = false, testMode = false, operationMessage = "", renderScenario: String?
    var autoConnectOnOpen = false, launchConnectionPending = false
    static let autoConnectKey = "autoConnectOnOpen"
    var connectMenuItem: NSMenuItem?, disconnectMenuItem: NSMenuItem?
    let listStack = NSStackView(), detailRows = NSStackView(), connectionTitle = NSTextField(labelWithString: "远程访问")
    let createButton = NSButton(title: "+  新建连接", target: nil, action: nil)
    let activeLabel = NSTextField(wrappingLabelWithString: ""), statusDot = DotView(frame: NSRect(x: 0, y: 0, width: 10, height: 10))
    let detailMessage = NSTextField(wrappingLabelWithString: "")
    let launchAtLoginButton = NSButton(checkboxWithTitle: "开机启动（登录 macOS 时打开 App）", target: nil, action: nil)
    let autoConnectButton = NSButton(checkboxWithTitle: "自动连接（打开 App 时连接所选 VPN）", target: nil, action: nil)
    let connectButton = NSButton(title: "连接", target: nil, action: nil), reauthButton = NSButton(title: "重新验证并连接…", target: nil, action: nil)
    let editButton = NSButton(title: "编辑", target: nil, action: nil), moreButton = NSButton(title: "⋯", target: nil, action: nil)
    var sheetWindow: NSWindow?, editorName = NSTextField(), editorServer = NSTextField(), editorPort = NSTextField(), editorUser = NSTextField()
    var editorError = NSTextField(labelWithString: ""), editingID: String?

    var selected: Profile? { ProfileStorage.selected(in: store) }
    var isBusyConnection: Bool { busy }
    var hasActiveConnection: Bool { Self.canStop(status: lastStatus) }
    static func canStop(status: StatusReply?) -> Bool {
        guard let status else { return false }
        return status.state != .stopped || status.autostart || status.state == .error
    }
    var selectedIsActive: Bool { selected.map { Self.isActive($0.id, status: lastStatus) } ?? false }
    static func isActive(_ id: String, status: StatusReply?) -> Bool {
        guard let status, status.state != .stopped || status.autostart else { return false }
        return status.profileID == id
    }
    static func actionTitle(selectedID: String?, status: StatusReply?) -> String {
        if let selectedID, isActive(selectedID, status: status) { return "断开" }
        if let status, status.state != .stopped || status.autostart { return status.profileID == nil ? "断开当前连接" : "切换并连接" }
        return "连接"
    }
    static func shouldConnectOnOpen(enabled: Bool, selectedID: String?, status: StatusReply) -> Bool {
        guard enabled, selectedID != nil else { return false }
        return status.state == .stopped || (status.state == .error && status.profileID != nil)
    }
    func connectOnOpen(status: StatusReply) {
        guard launchConnectionPending, !busy else { return }
        launchConnectionPending = false
        if Self.shouldConnectOnOpen(enabled: autoConnectOnOpen, selectedID: selected?.id, status: status) { connectSelected() }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .aqua)
        if CommandLine.arguments.contains("--self-test") { Self.selfTest(); NSApp.terminate(nil); return }
        if CommandLine.arguments.contains("--helper-check") {
            Service.action("ping", payload: nil) { result in
                switch result {
                case .success: print("VPN helper: OK"); exit(0)
                case .failure(let error): fputs("VPN helper: \(error.localizedDescription)\n", stderr); exit(1)
                }
            }
            return
        }
        if let i = CommandLine.arguments.firstIndex(of: "--make-icon"), i + 1 < CommandLine.arguments.count { Self.makeIcon(at: CommandLine.arguments[i + 1]); NSApp.terminate(nil); return }
        if let i = CommandLine.arguments.firstIndex(of: "--render-lock-preview"), i + 1 < CommandLine.arguments.count { Self.renderLockPreview(at: CommandLine.arguments[i + 1]); NSApp.terminate(nil); return }

        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--render-preview"), i + 2 < args.count {
            preview = true; renderScenario = args[i + 1]; store = renderScenario!.hasPrefix("welcome") ? ProfileStore() : Self.previewStore(); lastStatus = Self.previewStatus(renderScenario!)
        } else if args.contains("--preview") {
            preview = true; renderScenario = "disconnected"; store = Self.previewStore(); lastStatus = Self.previewStatus("disconnected")
        } else {
            do { store = try ProfileStorage.load() }
            catch { store = ProfileStore(); storeError = error }
            autoConnectOnOpen = UserDefaults.standard.bool(forKey: Self.autoConnectKey)
            launchConnectionPending = autoConnectOnOpen && !store.profiles.isEmpty && storeError == nil
        }
        buildWindow(); installMenu(); refreshList(); updateDetails(); showWindow()
        if !preview { refreshStatus() }
        if renderScenario != nil, let i = args.firstIndex(of: "--render-preview"), i + 2 < args.count {
            if renderScenario == "sheet" { presentEditor(selected) }
            if renderScenario == "welcome" { presentWelcome(availableProfiles: []) }
            if renderScenario == "welcome-import" { presentWelcome(availableProfiles: Self.previewStore().profiles) }
            let outputPath = args[i + 2]
            DispatchQueue.main.asyncAfter(deadline: .now() + (renderScenario?.hasPrefix("welcome") == true ? 0.7 : 0.25)) {
                if self.renderScenario?.hasPrefix("welcome") == true, let sheet = self.window.attachedSheet {
                    // Native alerts use composited materials that cacheDisplay cannot capture.
                    // Capture only this preview's sheet window, with a bounded process lifetime.
                    Service.run(["/usr/sbin/screencapture", "-x", "-o", "-l", String(sheet.windowNumber), outputPath], timeout: 3) { result in
                        DispatchQueue.main.async {
                            self.window.endSheet(sheet, returnCode: .cancel)
                            switch result {
                            case .success:
                                guard let image = NSImage(contentsOfFile: outputPath), image.size.width > 0 else {
                                    fputs("Welcome preview capture failed; check screen recording permission.\n", stderr); exit(1)
                                }
                                exit(0)
                            case .failure:
                                fputs("Welcome preview capture failed; check screen recording permission.\n", stderr); exit(1)
                            }
                        }
                    }
                } else { self.renderPreview(at: outputPath); NSApp.terminate(nil) }
            }
            return
        }
        if !preview, storeError == nil, store.profiles.isEmpty { presentWelcome() }
        if let storeError { alert("连接列表无法读取，原文件已保留。\n\(storeError.localizedDescription)") }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refreshStatus() }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow(); launchConnectionPending = autoConnectOnOpen && !store.profiles.isEmpty && storeError == nil; refreshStatus()
        if !preview, storeError == nil, store.profiles.isEmpty, window.attachedSheet == nil { presentWelcome() }
        return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func showWindow() { window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 550), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "SN VPN"; window.center(); window.delegate = self
        let root = NSStackView(); root.orientation = .horizontal; root.spacing = 0; root.alignment = .top; root.translatesAutoresizingMaskIntoConstraints = false
        let sidebar = makeSidebar(), detail = makeDetailPanel()
        root.addArrangedSubview(sidebar); root.addArrangedSubview(detail)
        sidebar.widthAnchor.constraint(equalToConstant: 210).isActive = true
        detail.widthAnchor.constraint(equalToConstant: 610).isActive = true
        for view in [sidebar, detail] { view.heightAnchor.constraint(equalTo: root.heightAnchor).isActive = true }
        window.contentView?.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor), root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor), root.topAnchor.constraint(equalTo: window.contentView!.topAnchor), root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor)])
    }
    func colored(_ view: NSView, _ color: NSColor) { view.wantsLayer = true; view.layer?.backgroundColor = color.cgColor }
    func stack(_ views: [NSView], spacing: CGFloat, padding: NSEdgeInsets = NSEdgeInsetsZero) -> NSStackView {
        let value = NSStackView(views: views); value.orientation = .vertical; value.alignment = .leading; value.spacing = spacing; value.edgeInsets = padding; value.translatesAutoresizingMaskIntoConstraints = false; return value
    }
    func makeSidebar() -> NSView {
        let panel = NSView(); colored(panel, Self.navy)
        let brand = NSTextField(labelWithString: "SN VPN"); brand.font = .systemFont(ofSize: 20, weight: .bold); brand.textColor = .white
        let nav = NSTextField(labelWithString: "远程访问"); nav.textColor = NSColor(calibratedWhite: 0.78, alpha: 1); nav.font = .systemFont(ofSize: 12, weight: .semibold)
        listStack.orientation = .vertical; listStack.alignment = .leading; listStack.spacing = 7; listStack.translatesAutoresizingMaskIntoConstraints = false
        createButton.target = self; createButton.action = #selector(newProfile); createButton.isBordered = false; createButton.alignment = .left
        createButton.contentTintColor = .white; createButton.font = .systemFont(ofSize: 13, weight: .medium)
        let column = stack([brand, nav, listStack, NSView(), createButton], spacing: 22)
        panel.addSubview(column)
        NSLayoutConstraint.activate([column.leadingAnchor.constraint(equalTo: panel.leadingAnchor, constant: 16), column.trailingAnchor.constraint(equalTo: panel.trailingAnchor, constant: -16), column.topAnchor.constraint(equalTo: panel.topAnchor, constant: 28), column.bottomAnchor.constraint(equalTo: panel.bottomAnchor, constant: -20), nav.widthAnchor.constraint(equalTo: column.widthAnchor), listStack.widthAnchor.constraint(equalTo: column.widthAnchor), createButton.widthAnchor.constraint(equalTo: column.widthAnchor)])
        return panel
    }
    func makeListPanel() -> NSView {
        let panel = NSView(); colored(panel, Self.canvas); return panel
    }
    func makeDetailPanel() -> NSView {
        let panel = NSView(); colored(panel, NSColor(calibratedWhite: 1, alpha: 1))
        connectionTitle.font = .systemFont(ofSize: 22, weight: .semibold)
        let subtitle = NSTextField(labelWithString: "Fortinet SSL VPN"); subtitle.font = .systemFont(ofSize: 12); subtitle.textColor = .secondaryLabelColor
        let titleGroup = stack([connectionTitle, subtitle], spacing: 4)
        let top = NSStackView(views: [titleGroup, NSView(), editButton, moreButton]); top.orientation = .horizontal; top.alignment = .centerY; top.spacing = 10
        editButton.target = self; editButton.action = #selector(editSelected); editButton.bezelStyle = .rounded; editButton.toolTip = "活动连接时请先断开，再编辑配置"
        moreButton.target = self; moreButton.action = #selector(showMore); moreButton.bezelStyle = .rounded
        let activeRow = NSStackView(views: [statusDot, activeLabel]); activeRow.orientation = .horizontal; activeRow.spacing = 9
        activeLabel.font = .systemFont(ofSize: 13, weight: .medium); activeLabel.maximumNumberOfLines = 2
        detailRows.orientation = .vertical; detailRows.spacing = 2; detailRows.alignment = .leading
        detailRows.identifier = NSUserInterfaceItemIdentifier("profileDetails")
        let details: NSView = detailRows
        let buttonColumn = stack([connectButton, reauthButton], spacing: 10)
        connectButton.target = self; connectButton.action = #selector(primaryAction); connectButton.isBordered = false; connectButton.font = .systemFont(ofSize: 15, weight: .semibold)
        connectButton.attributedTitle = NSAttributedString(string: "连接", attributes: [.foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 15, weight: .semibold)])
        connectButton.wantsLayer = true; connectButton.layer?.backgroundColor = Self.blue.cgColor; connectButton.layer?.cornerRadius = 7
        reauthButton.target = self; reauthButton.action = #selector(updateLogin); reauthButton.isBordered = false; reauthButton.contentTintColor = Self.blue
        detailMessage.textColor = .secondaryLabelColor; detailMessage.maximumNumberOfLines = 2
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .vertical)
        launchAtLoginButton.target = self; launchAtLoginButton.action = #selector(toggleLaunchAtLogin)
        autoConnectButton.target = self; autoConnectButton.action = #selector(toggleAutoConnect)
        for button in [launchAtLoginButton, autoConnectButton] { button.font = .systemFont(ofSize: 12) }
        let footerMessage = NSTextField(labelWithString: "已连接时，关闭窗口或退出 App 不会断开 VPN")
        footerMessage.textColor = .secondaryLabelColor; footerMessage.font = .systemFont(ofSize: 12)
        let footer = NSStackView(views: [launchAtLoginButton, autoConnectButton, footerMessage]); footer.orientation = .vertical; footer.spacing = 6; footer.alignment = .leading
        let column = stack([top, activeRow, details, buttonColumn, detailMessage, spacer, footer], spacing: 20)
        panel.addSubview(column)
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: panel.leadingAnchor, constant: 30), column.trailingAnchor.constraint(equalTo: panel.trailingAnchor, constant: -30), column.topAnchor.constraint(equalTo: panel.topAnchor, constant: 28), column.bottomAnchor.constraint(equalTo: panel.bottomAnchor, constant: -22),
            top.widthAnchor.constraint(equalTo: column.widthAnchor), activeRow.widthAnchor.constraint(equalTo: column.widthAnchor), details.widthAnchor.constraint(equalTo: column.widthAnchor), buttonColumn.widthAnchor.constraint(equalTo: column.widthAnchor), footer.widthAnchor.constraint(equalTo: column.widthAnchor),
            connectButton.widthAnchor.constraint(equalToConstant: 220), connectButton.heightAnchor.constraint(equalToConstant: 42), reauthButton.widthAnchor.constraint(equalToConstant: 220),
            statusDot.widthAnchor.constraint(equalToConstant: 10), statusDot.heightAnchor.constraint(equalToConstant: 10)
        ])
        return panel
    }
    func infoRow(_ title: String, _ value: String) -> NSView {
        let key = NSTextField(labelWithString: title); key.textColor = .secondaryLabelColor; key.font = .systemFont(ofSize: 12)
        let val = NSTextField(labelWithString: value); val.font = .systemFont(ofSize: 14); val.lineBreakMode = .byTruncatingTail
        let row = NSStackView(views: [key, val]); row.orientation = .horizontal; row.spacing = 14; row.alignment = .centerY
        key.widthAnchor.constraint(equalToConstant: 100).isActive = true; val.widthAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true
        row.widthAnchor.constraint(equalToConstant: 530).isActive = true; row.heightAnchor.constraint(equalToConstant: 40).isActive = true
        return row
    }
    func refreshList() {
        for row in listStack.arrangedSubviews { listStack.removeArrangedSubview(row); row.removeFromSuperview() }
        for (index, profile) in store.profiles.enumerated() {
            let selected = profile.id == store.selectedId
            let title = NSMutableAttributedString(string: "   \(profile.name)", attributes: [.font: NSFont.systemFont(ofSize: 14, weight: .medium), .foregroundColor: NSColor.white])
            title.append(NSAttributedString(string: "\n   SSL VPN", attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor(calibratedWhite: 0.9, alpha: 1)]))
            let row = NSButton(title: "", target: self, action: #selector(selectProfile(_:))); row.attributedTitle = title; row.alignment = .left; row.isBordered = false; row.tag = index
            row.wantsLayer = true; row.layer?.cornerRadius = 7; row.layer?.backgroundColor = selected ? Self.blue.cgColor : NSColor.clear.cgColor
            row.heightAnchor.constraint(equalToConstant: 58).isActive = true; row.widthAnchor.constraint(equalToConstant: 178).isActive = true
            listStack.addArrangedSubview(row)
        }
        let placeholder = NSTextField(wrappingLabelWithString: storeError == nil ? "暂无连接" : "连接列表无法读取")
        placeholder.textColor = NSColor(calibratedWhite: 0.76, alpha: 1); placeholder.maximumNumberOfLines = 2
        if store.profiles.isEmpty { listStack.addArrangedSubview(placeholder); placeholder.widthAnchor.constraint(equalToConstant: 178).isActive = true }
        updateControls()
    }
    func updateDetails() {
        guard let selected else {
            connectionTitle.stringValue = "远程访问"; activeLabel.stringValue = storeError == nil ? "选择连接或新建 VPN 配置" : "本地配置文件无效"
            statusDot.color = storeError == nil ? .secondaryLabelColor : .systemRed
            detailRows.setViews([], in: .top); updateControls(); return
        }
        connectionTitle.stringValue = selected.name
        updateActiveBanner()
        detailRows.setViews([infoRow("远程网关", selected.server), infoRow("端口", String(selected.port)), infoRow("用户名", selected.username)], in: .top)
        updateControls()
    }
    func updateActiveBanner() {
        let title = lastStatus.map { $0.title } ?? "状态未知"
        statusDot.color = lastStatus?.color ?? .secondaryLabelColor
        if hasActiveConnection {
            let activeName = store.profiles.first(where: { $0.id == lastStatus?.profileID })?.name ?? (lastStatus?.profileID == nil ? "配置未知" : "其他配置")
            activeLabel.stringValue = "当前连接：\(activeName)  ·  \(title)"
        } else { activeLabel.stringValue = lastStatus?.title ?? "状态未知" }
    }
    func updateControls() {
        let canMutate = !busy && storeError == nil
        let title = Self.actionTitle(selectedID: selected?.id, status: lastStatus)
        let buttonTitle = busy ? "正在处理 VPN 操作…" : title
        connectButton.attributedTitle = NSAttributedString(string: buttonTitle, attributes: [.foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 15, weight: .semibold)])
        connectButton.isEnabled = canMutate && selected != nil && (!title.hasPrefix("断开") || hasActiveConnection)
        reauthButton.isEnabled = canMutate && selected != nil
        editButton.isEnabled = canMutate && selected != nil && !selectedIsActive
        createButton.isEnabled = canMutate
        moreButton.isEnabled = canMutate && selected != nil
        connectMenuItem?.isEnabled = canMutate && selected != nil && !title.hasPrefix("断开")
        connectMenuItem?.title = selected.map { "连接：\($0.name)" } ?? "连接当前配置"
        disconnectMenuItem?.isEnabled = !busy && hasActiveConnection
        let activeName = store.profiles.first(where: { $0.id == lastStatus?.profileID })?.name
        disconnectMenuItem?.title = activeName.map { "断开当前连接：\($0)" } ?? "断开当前连接"
        autoConnectButton.state = autoConnectOnOpen ? .on : .off
        autoConnectButton.isEnabled = !busy
        launchAtLoginButton.isEnabled = !busy
        if !preview {
            let status = SMAppService.mainApp.status
            launchAtLoginButton.state = status == .enabled || status == .requiresApproval ? .on : .off
            launchAtLoginButton.title = status == .requiresApproval ? "开机启动（等待系统允许）" : "开机启动（登录 macOS 时打开 App）"
        }
    }
    @objc func toggleAutoConnect() {
        autoConnectOnOpen = autoConnectButton.state == .on
        if !preview { UserDefaults.standard.set(autoConnectOnOpen, forKey: Self.autoConnectKey) }
        if !autoConnectOnOpen { launchConnectionPending = false }
        updateControls()
    }
    @objc func toggleLaunchAtLogin() {
        if preview { return }
        do {
            if launchAtLoginButton.state == .on { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
        } catch {
            if SMAppService.mainApp.status != .requiresApproval { alert("无法更改开机启动设置。\n\(error.localizedDescription)") }
        }
        if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
        updateControls()
    }
    func updateStatusAccessibility() {
        let text = busy ? operationMessage : (lastStatus.map { "\($0.title)，\($0.message)" } ?? "正在读取状态")
        statusItem?.button?.toolTip = "SN VPN：\(text)"
        statusItem?.button?.setAccessibilityLabel("SN VPN：\(text)")
        statusItem?.button?.setAccessibilityHelp("当前 VPN 状态：\(text)")
        let isConnected = !busy && lastStatus?.state == .connected && lastStatus?.reachable == true
        let tint = isConnected ? Self.gold : NSColor(calibratedWhite: 0.5, alpha: 1)
        statusItem?.button?.image = Self.lockIcon(tint, size: NSSize(width: 18, height: 18), description: "SN VPN：\(text)")
    }
    func installMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(), appMenu = NSMenu(title: "SN VPN")
        appMenu.addItem(withTitle: "从 FortiClient 导入连接…", action: #selector(importFortiProfiles), keyEquivalent: "").target = self
        appMenu.addItem(withTitle: "退出 SN VPN", action: #selector(quit), keyEquivalent: "q").target = self
        appItem.submenu = appMenu; main.addItem(appItem)
        let editItem = NSMenuItem(), editMenu = Self.editMenu()
        editMenu.title = "编辑"
        editItem.submenu = editMenu; main.addItem(editItem); NSApp.mainMenu = main
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.addItem(withTitle: "从 FortiClient 导入连接…", action: #selector(importFortiProfiles), keyEquivalent: "").target = self
        menu.addItem(withTitle: "打开 SN VPN", action: #selector(openWindow), keyEquivalent: "o").target = self
        menu.addItem(NSMenuItem.separator())
        connectMenuItem = menu.addItem(withTitle: "连接当前配置", action: #selector(connectMenuAction), keyEquivalent: ""); connectMenuItem?.target = self
        disconnectMenuItem = menu.addItem(withTitle: "断开当前连接", action: #selector(disconnect), keyEquivalent: ""); disconnectMenuItem?.target = self
        menu.addItem(NSMenuItem.separator())
        menu.addItem(Self.versionMenuItem())
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "退出（不会主动断开）", action: #selector(quit), keyEquivalent: "q").target = self
        statusItem.menu = menu; updateStatusAccessibility()
    }
    static func versionMenuItem() -> NSMenuItem {
        let item = NSMenuItem(title: "版本：\(BuildVersion.display)", action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }
    static func editMenu() -> NSMenu {
        let menu = NSMenu(title: "编辑")
        menu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        menu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        menu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        return menu
    }
    func refreshStatus() {
        if preview { return }
        Service.status { [weak self] result in DispatchQueue.main.async {
            guard let self else { return }
            switch result {
            case .success(let status): self.lastStatus = status; self.connectOnOpen(status: status)
            case .failure:
                self.launchConnectionPending = false
                self.lastStatus = StatusReply(state: .error, reachable: false, autostart: false, message: "无法读取 VPN 状态", profileID: nil)
            }
            self.updateActiveBanner(); self.updateControls(); self.updateStatusAccessibility()
        } }
    }
    func commit(_ candidate: ProfileStore) throws {
        if !preview { try ProfileStorage.save(candidate) }
        store = candidate; storeError = nil; refreshList(); updateDetails()
    }
    @objc func selectProfile(_ sender: NSButton) {
        guard store.profiles.indices.contains(sender.tag), let next = try? ProfileStorage.select(store.profiles[sender.tag].id, in: store) else { return }
        do { try commit(next) } catch { alert("未能保存所选连接，原配置保持不变。\n\(error.localizedDescription)") }
    }
    func presentWelcome(availableProfiles: [Profile]? = nil) {
        guard storeError == nil, store.profiles.isEmpty, sheetWindow == nil, window.attachedSheet == nil else { return }
        launchConnectionPending = false
        let available = availableProfiles ?? (preview ? [] : ((try? FortiImport.load())?.profiles ?? []))
        let hasImport = !available.isEmpty
        let welcome = NSAlert(); welcome.messageText = "欢迎使用 SN VPN"
        welcome.informativeText = hasImport
            ? "发现可用的 FortiClient SSL VPN 配置。可导入连接或手动新建；密码不会导入，保存后由你发起连接。"
            : "直接填写服务器、端口和用户名即可新建连接，无需安装 FortiClient。已有 FortiClient 配置时，也可从 App 菜单导入。"
        if hasImport { welcome.addButton(withTitle: "导入连接…") }
        welcome.addButton(withTitle: "新建连接")
        welcome.addButton(withTitle: "稍后")
        welcome.beginSheetModal(for: window) { response in
            if hasImport && response == .alertFirstButtonReturn { self.importFortiProfiles() }
            else if response == (hasImport ? .alertSecondButtonReturn : .alertFirstButtonReturn) { self.presentEditor(nil) }
        }
    }
    @objc func importFortiProfiles() {
        guard !busy, sheetWindow == nil, window.attachedSheet == nil else { return }
        guard storeError == nil else { alert("本地连接列表无法读取，未导入或覆盖配置。请先修复本地配置文件。"); return }
        showWindow()
        do {
            let result = try FortiImport.load()
            guard !result.profiles.isEmpty else { throw AppError.failed("未发现可导入的有效 SSL VPN 配置（已跳过 \(result.skipped) 项）。可直接新建连接。") }
            let prompt = NSAlert(); prompt.messageText = "导入 FortiClient 连接"
            prompt.informativeText = "选择一项或全部导入。仅导入名称、网关、端口和用户名，不导入密码，也不会自动连接。已跳过无效或不支持的配置 \(result.skipped) 项。"
            let choices = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 340, height: 28))
            choices.addItem(withTitle: "全部有效连接（\(result.profiles.count) 项）")
            for profile in result.profiles { choices.addItem(withTitle: profile.name) }
            prompt.accessoryView = choices
            prompt.addButton(withTitle: "导入"); prompt.addButton(withTitle: "新建连接"); prompt.addButton(withTitle: "取消")
            prompt.beginSheetModal(for: window) { response in
                if response == .alertSecondButtonReturn { self.presentEditor(nil); return }
                guard response == .alertFirstButtonReturn else { return }
                let picked = choices.indexOfSelectedItem == 0 ? result.profiles : [result.profiles[choices.indexOfSelectedItem - 1]]
                do {
                    var next = self.store
                    for profile in picked { next = try ProfileStorage.add(profile, to: next) }
                    self.launchConnectionPending = false
                    try self.commit(next)
                } catch { self.alert("未导入，原连接列表已保留。\n\(error.localizedDescription)") }
            }
        } catch {
            let prompt = NSAlert(); prompt.messageText = "无法导入连接"; prompt.informativeText = error.localizedDescription
            prompt.addButton(withTitle: "新建连接"); prompt.addButton(withTitle: "取消")
            prompt.beginSheetModal(for: window) { response in if response == .alertFirstButtonReturn { self.presentEditor(nil) } }
        }
    }
    @objc func newProfile() { guard storeError == nil, !busy else { return }; presentEditor(nil) }
    @objc func editSelected() {
        guard !busy, let selected else { return }
        guard !Self.isActive(selected.id, status: lastStatus) else { alert("请先断开此连接，再编辑配置。"); return }
        presentEditor(selected)
    }
    @objc func showMore() {
        guard !busy, let profile = selected else { return }
        let menu = NSMenu()
        menu.addItem(withTitle: "重新验证并连接…", action: #selector(updateLogin), keyEquivalent: "").target = self
        let remove = menu.addItem(withTitle: "删除连接…", action: #selector(deleteSelected), keyEquivalent: ""); remove.target = self
        remove.isEnabled = storeError == nil && !Self.isActive(profile.id, status: lastStatus)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: moreButton.bounds.height), in: moreButton)
    }
    func presentEditor(_ profile: Profile?) {
        editingID = profile?.id
        let sheet = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 500, height: 430), styleMask: [.titled], backing: .buffered, defer: false)
        sheet.title = profile == nil ? "新增连接" : "编辑连接"
        sheet.appearance = NSAppearance(named: .aqua); sheet.backgroundColor = .white
        sheet.contentView = SheetBackgroundView(frame: NSRect(x: 0, y: 0, width: 500, height: 430))
        editorName = NSTextField(); editorName.placeholderString = "例如：SN IDC"; editorName.stringValue = profile?.name ?? ""
        editorServer = NSTextField(); editorServer.placeholderString = "vpn.example.com"; editorServer.stringValue = profile?.server ?? ""
        editorPort = NSTextField(); editorPort.stringValue = String(profile?.port ?? 443)
        editorUser = NSTextField(); editorUser.placeholderString = "用户名"; editorUser.stringValue = profile?.username ?? ""
        let kind = NSTextField(labelWithString: "Fortinet SSL VPN"); kind.textColor = .darkGray
        let rows = NSGridView(views: [
            [NSTextField(labelWithString: "连接名称"), editorName],
            [NSTextField(labelWithString: "VPN 类型"), kind],
            [NSTextField(labelWithString: "远程网关"), editorServer],
            [NSTextField(labelWithString: "端口"), editorPort],
            [NSTextField(labelWithString: "用户名"), editorUser]
        ])
        for row in 0..<rows.numberOfRows {
            if let field = rows.cell(atColumnIndex: 0, rowIndex: row).contentView as? NSTextField { field.textColor = .darkGray }
        }
        rows.rowSpacing = 16; rows.columnSpacing = 18; rows.xPlacement = .leading; rows.column(at: 0).width = 92; rows.column(at: 1).width = 270
        editorError.textColor = .systemRed; editorError.stringValue = ""
        let cancel = NSButton(title: "取消", target: self, action: #selector(cancelEditor)); cancel.bezelStyle = .rounded
        let save = NSButton(title: "保存", target: self, action: #selector(saveEditor)); save.bezelStyle = .rounded; save.keyEquivalent = "\r"
        let buttons = NSStackView(views: [NSView(), cancel, save]); buttons.orientation = .horizontal; buttons.spacing = 10
        let title = NSTextField(labelWithString: profile == nil ? "新增 VPN 连接" : "编辑 VPN 连接"); title.font = .systemFont(ofSize: 20, weight: .semibold)
        let column = stack([title, rows, editorError, buttons], spacing: 22)
        column.setFrameSize(NSSize(width: 500, height: 420))
        sheet.contentView?.addSubview(column)
        NSLayoutConstraint.activate([column.leadingAnchor.constraint(equalTo: sheet.contentView!.leadingAnchor, constant: 24), column.trailingAnchor.constraint(equalTo: sheet.contentView!.trailingAnchor, constant: -24), column.topAnchor.constraint(equalTo: sheet.contentView!.topAnchor, constant: 24), column.bottomAnchor.constraint(lessThanOrEqualTo: sheet.contentView!.bottomAnchor, constant: -20), rows.widthAnchor.constraint(equalTo: column.widthAnchor), buttons.widthAnchor.constraint(equalTo: column.widthAnchor)])
        sheetWindow = sheet
        if preview { if !testMode { sheet.makeKeyAndOrderFront(nil) } }
        else { window.beginSheet(sheet); sheet.makeKeyAndOrderFront(nil) }
    }
    func closeEditor(_ code: NSApplication.ModalResponse) {
        guard let sheetWindow else { return }
        if preview { sheetWindow.close(); self.sheetWindow = nil }
        else { window.endSheet(sheetWindow, returnCode: code); self.sheetWindow = nil }
    }
    @objc func cancelEditor() { closeEditor(.cancel) }
    @objc func saveEditor() {
        let port = Int(editorPort.stringValue) ?? 0
        let profile = Profile(id: editingID ?? UUID().uuidString.lowercased(), name: editorName.stringValue, server: editorServer.stringValue, port: port, username: editorUser.stringValue)
        do {
            try ProfileStorage.validate(profile)
            let candidate = editingID == nil ? try ProfileStorage.add(profile, to: store) : try ProfileStorage.replace(profile, in: store)
            try commit(candidate)
            let oldSheet = sheetWindow
            if oldSheet != nil { closeEditor(.OK) }
        } catch { editorError.stringValue = error.localizedDescription }
    }
    @objc func deleteSelected() {
        guard !busy, let profile = selected else { return }
        guard !Self.isActive(profile.id, status: lastStatus) else { alert("请先断开此配置，再删除。") ; return }
        let alert = NSAlert(); alert.messageText = "删除连接配置？"; alert.informativeText = "“\(profile.name)”将从此设备移除。"; alert.addButton(withTitle: "删除"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { response in if response == .alertFirstButtonReturn { self.forget(profile) } }
    }
    func forget(_ profile: Profile) {
        guard !Self.isActive(profile.id, status: lastStatus) else { return }
        if preview {
            guard let next = try? ProfileStorage.remove(profile.id, from: store) else { return }
            store = next; refreshList(); updateDetails(); return
        }
        guard !busy else { return }; busy = true; operationMessage = "正在删除连接配置…"; updateControls()
        Service.action("forget-profile", payload: ["id": profile.id]) { [weak self] result in DispatchQueue.main.async {
            guard let self else { return }; self.busy = false; self.operationMessage = ""
            switch result {
            case .success:
                do {
                    let candidate = try ProfileStorage.remove(profile.id, from: self.store)
                    try self.commit(candidate)
                } catch { self.alert("服务端已移除此配置，但本地列表尚未同步；可重试以完成同步。\n\(error.localizedDescription)") }
            case .failure(let error): self.alert(error.localizedDescription)
            }
            self.updateControls()
        } }
    }
    @objc func primaryAction() {
        if selectedIsActive || Self.actionTitle(selectedID: selected?.id, status: lastStatus) == "断开当前连接" { disconnect() }
        else { connectSelected() }
    }
    @objc func connectSelected() { connectSelected(reauth: false) }
    @objc func connectMenuAction() { connectSelected(reauth: false) }
    func connectSelected(reauth: Bool) {
        guard let profile = selected, !busy, storeError == nil else { return }
        if preview {
            lastStatus = StatusReply(state: .connected, reachable: true, autostart: true, message: "预览：连接状态", profileID: profile.id)
            updateActiveBanner(); updateControls(); updateStatusAccessibility(); return
        }
        busy = true; operationMessage = "正在连接 VPN…"; updateControls()
        var payload: [String: Any] = ["id": profile.id, "name": profile.name, "server": profile.server, "port": profile.port, "username": profile.username]
        if reauth { payload["reauth"] = true }
        Service.action("connect-profile", payload: payload) { [weak self] result in DispatchQueue.main.async {
            guard let self else { return }; self.busy = false; self.operationMessage = ""
            if case .failure(let error) = result { self.alert(error.localizedDescription) }
            self.refreshStatus(); self.updateControls()
        } }
    }
    @objc func updateLogin() { connectSelected(reauth: true) }
    @objc func disconnect() {
        guard !busy, lastStatus != nil, hasActiveConnection || lastStatus?.state == .error else { return }
        if preview {
            lastStatus = StatusReply(state: .stopped, reachable: false, autostart: false, message: "预览：当前未连接", profileID: nil)
            updateActiveBanner(); updateControls(); updateStatusAccessibility(); return
        }
        busy = true; operationMessage = "正在断开 VPN…"; updateControls()
        Service.action("stop", payload: nil) { [weak self] result in DispatchQueue.main.async {
            guard let self else { return }; self.busy = false; self.operationMessage = ""
            if case .failure(let error) = result { self.alert(error.localizedDescription) }
            self.refreshStatus(); self.updateControls()
        } }
    }
    func alert(_ message: String) {
        let alert = NSAlert(); alert.messageText = "SN VPN"; alert.informativeText = message; alert.alertStyle = .warning
        if let window, window.isVisible { alert.beginSheetModal(for: window) } else { showWindow(); alert.beginSheetModal(for: window) }
    }
    @objc func openWindow() { showWindow() }
    @objc func quit() { NSApp.terminate(nil) }
    func renderPreview(at path: String) {
        if let renderedSheet = sheetWindow ?? window.attachedSheet, let view = renderedSheet.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.layoutSubtreeIfNeeded(); renderedSheet.displayIfNeeded(); view.cacheDisplay(in: view.bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) { try? png.write(to: URL(fileURLWithPath: path)) }
        } else if let view = window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            window.displayIfNeeded(); view.cacheDisplay(in: view.bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) { try? png.write(to: URL(fileURLWithPath: path)) }
        }
    }
    static func previewStore() -> ProfileStore {
        let a = Profile(id: "11111111-1111-4111-8111-111111111111", name: "SN IDC", server: "vpn.example.test", port: 443, username: "demo")
        let b = Profile(id: "22222222-2222-4222-8222-222222222222", name: "办公网络", server: "remote.example.test", port: 10443, username: "alex")
        let c = Profile(id: "33333333-3333-4333-8333-333333333333", name: "测试环境", server: "qa.example.test", port: 443, username: "qa-user")
        return ProfileStore(selectedId: a.id, profiles: [a, b, c])
    }
    static func previewStatus(_ scenario: String) -> StatusReply {
        if scenario == "connected" { return StatusReply(state: .connected, reachable: true, autostart: true, message: "VPN 已连接，内网可达", profileID: "22222222-2222-4222-8222-222222222222") }
        return StatusReply(state: .stopped, reachable: false, autostart: false, message: "当前未连接", profileID: nil)
    }
    static func selfTest() {
        let versionParts = BuildVersion.display.split(separator: "-")
        precondition((versionParts.count == 2 || (versionParts.count == 3 && versionParts[2] == "dirty")) && versionParts[1].count == 8)
        let versionItem = versionMenuItem()
        precondition(versionItem.title == "版本：\(BuildVersion.display)" && !versionItem.isEnabled)
        let a = Profile(id: "primary", name: "旧连接", server: "old.example.test", port: 443, username: "old-user")
        let b = Profile(id: "11111111-1111-4111-8111-111111111111", name: "办公", server: "vpn.example.test", port: 443, username: "demo")
        let base = ProfileStore(selectedId: a.id, profiles: [a])
        let added = try! ProfileStorage.add(b, to: base); precondition(added.profiles.count == 2 && added.selectedId == b.id)
        var edited = b; edited.name = "办公新版"; let replaced = try! ProfileStorage.replace(edited, in: added); precondition(replaced.profiles[1].name == "办公新版")
        var rejectedDuplicate = false
        do {
            let duplicate = Profile(id: "44444444-4444-4444-8444-444444444444", name: " 办公新版 ", server: "other.example.test", port: 443, username: "other")
            _ = try ProfileStorage.add(duplicate, to: replaced)
        } catch { rejectedDuplicate = true }
        precondition(rejectedDuplicate)
        let selected = try! ProfileStorage.select(a.id, in: replaced); precondition(selected.selectedId == a.id)
        let removed = try! ProfileStorage.remove(a.id, from: selected); precondition(removed.profiles.count == 1 && removed.selectedId == b.id)
        let active = StatusReply(state: .connected, reachable: true, autostart: true, message: "已连接", profileID: a.id)
        precondition(actionTitle(selectedID: a.id, status: active) == "断开")
        precondition(actionTitle(selectedID: b.id, status: active) == "切换并连接")
        precondition(!isActive(b.id, status: active))
        let authError = StatusReply(state: .error, reachable: false, autostart: true, message: "认证失败", profileID: a.id)
        precondition(canStop(status: authError) && actionTitle(selectedID: a.id, status: authError) == "断开")
        let unreadable = StatusReply(state: .error, reachable: false, autostart: false, message: "无法读取 VPN 状态", profileID: nil)
        precondition(canStop(status: unreadable) && actionTitle(selectedID: b.id, status: unreadable) == "断开当前连接")
        let stopped = previewStatus("disconnected")
        precondition(shouldConnectOnOpen(enabled: true, selectedID: a.id, status: stopped))
        precondition(!shouldConnectOnOpen(enabled: false, selectedID: a.id, status: stopped))
        precondition(!shouldConnectOnOpen(enabled: true, selectedID: nil, status: stopped))
        precondition(!shouldConnectOnOpen(enabled: true, selectedID: a.id, status: active))
        precondition(!shouldConnectOnOpen(enabled: true, selectedID: b.id, status: active))
        precondition(!shouldConnectOnOpen(enabled: true, selectedID: a.id, status: unreadable))
        precondition(shouldConnectOnOpen(enabled: true, selectedID: a.id, status: authError))
        for state in [VPNState.connecting, .reconnecting, .degraded] {
            let status = StatusReply(state: state, reachable: false, autostart: true, message: "", profileID: a.id)
            precondition(!shouldConnectOnOpen(enabled: true, selectedID: a.id, status: status))
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("sn-vpn-selftest-\(UUID().uuidString)")
        try! FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let fortiFile = root.appendingPathComponent("vpn.plist")
        precondition((try? FortiImport.load(from: fortiFile)) == nil)
        precondition((try? FortiImport.parse(Data("malformed".utf8))) == nil)
        let validForti: [String: Any] = ["VpnType": 0, "Server": "vpn.example.test", "ServerPort": 443, "User": "demo"]
        var unsupportedForti = validForti; unsupportedForti["VpnType"] = 1
        var invalidForti = validForti; invalidForti["Server"] = "bad host !"
        let fixture: [String: Any] = ["Profiles": ["办公": validForti, "不支持": unsupportedForti, "无效": invalidForti]]
        let fortiData = try! PropertyListSerialization.data(fromPropertyList: fixture, format: .xml, options: 0)
        try! fortiData.write(to: fortiFile)
        let imported = try! FortiImport.load(from: fortiFile)
        precondition(imported.profiles.count == 1 && imported.skipped == 2)
        precondition(imported.profiles[0].name == "办公" && imported.profiles[0].server == "vpn.example.test" && imported.profiles[0].port == 443 && imported.profiles[0].username == "demo")
        let validData = try! PropertyListSerialization.data(fromPropertyList: ["Profiles": ["办公": validForti]], format: .binary, options: 0)
        precondition((try! FortiImport.parse(validData)).profiles.count == 1)
        for invalidPort in [true, 443.5] as [Any] {
            var invalid = validForti; invalid["ServerPort"] = invalidPort
            let data = try! PropertyListSerialization.data(fromPropertyList: ["Profiles": ["无效端口": invalid]], format: .xml, options: 0)
            precondition((try! FortiImport.parse(data)).profiles.isEmpty)
        }
        var duplicateImportRejected = false
        do {
            var candidate = base
            let duplicate = Profile(id: UUID().uuidString.lowercased(), name: a.name, server: "vpn.example.test", port: 443, username: "demo")
            candidate = try ProfileStorage.add(imported.profiles[0], to: candidate)
            candidate = try ProfileStorage.add(duplicate, to: candidate)
        } catch { duplicateImportRejected = true }
        precondition(duplicateImportRejected && base.profiles == [a])
        let emptyData = try! PropertyListSerialization.data(fromPropertyList: ["Profiles": [:]], format: .xml, options: 0)
        precondition((try! FortiImport.parse(emptyData)).profiles.isEmpty)
        let newFile = root.appendingPathComponent("profiles.json"), oldFile = root.appendingPathComponent("profile.json")
        try! Data(#"{"name":"legacy","server":"legacy.example.test","port":443,"username":"legacy-user"}"#.utf8).write(to: oldFile)
        let migrated = try! ProfileStorage.load(profilesURL: newFile, legacyURL: oldFile)
        precondition(migrated.selectedId == "primary" && migrated.profiles.first?.id == "primary")
        precondition(FileManager.default.fileExists(atPath: oldFile.path) && FileManager.default.fileExists(atPath: newFile.path))
        let corrupt = Data("{bad json".utf8); try! corrupt.write(to: newFile)
        var rejectedCorrupt = false
        do { _ = try ProfileStorage.load(profilesURL: newFile, legacyURL: oldFile) } catch { rejectedCorrupt = true }
        precondition(rejectedCorrupt)
        precondition((try! Data(contentsOf: newFile)) == corrupt)
        let parsed = try! StatusReply.parse(Data(#"{"state":"connected","reachable":true,"autostart":true,"message":"ok","profile_id":"primary"}"#.utf8))
        precondition(parsed.profileID == "primary")
        let requirement = "identifier \"local.snvpn.helper\" and cdhash H\"" + String(repeating: "a", count: 40) + "\""
        precondition(VPNHelperClient.validRequirement(requirement))
        precondition(!VPNHelperClient.validRequirement(requirement + "\n"))
        precondition(!VPNHelperClient.validRequirement("identifier \"local.snvpn.helper\""))
        precondition(!VPNHelperClient.validRequirement(requirement.replacingOccurrences(of: "helper", with: "desktop")))
        let activePreview = previewStore(), activePreviewStatus = previewStatus("connected")
        precondition(activePreview.selectedId == "11111111-1111-4111-8111-111111111111")
        precondition(actionTitle(selectedID: activePreview.selectedId, status: activePreviewStatus) == "切换并连接")
        precondition(activePreview.profiles.first(where: { $0.id == activePreviewStatus.profileID })?.name == "办公网络")
        let previewApp = App(); previewApp.preview = true; previewApp.testMode = true
        previewApp.store = previewStore(); previewApp.lastStatus = previewStatus("disconnected")
        previewApp.launchAtLoginButton.state = .off
        previewApp.autoConnectButton.state = .on; previewApp.toggleAutoConnect()
        precondition(previewApp.autoConnectOnOpen && previewApp.launchAtLoginButton.state == .off)
        previewApp.launchAtLoginButton.state = .on; previewApp.toggleLaunchAtLogin()
        precondition(previewApp.autoConnectOnOpen)
        previewApp.launchConnectionPending = true
        previewApp.connectOnOpen(status: stopped)
        precondition(previewApp.lastStatus?.state == .connected && !previewApp.launchConnectionPending)
        previewApp.disconnect()
        previewApp.connectOnOpen(status: stopped)
        precondition(previewApp.lastStatus?.state == .stopped && previewApp.autoConnectOnOpen)
        previewApp.autoConnectButton.state = .off; previewApp.toggleAutoConnect()
        precondition(!previewApp.autoConnectOnOpen && previewApp.launchAtLoginButton.state == .on)
        let draft = Profile(id: "44444444-4444-4444-8444-444444444444", name: "预览草稿", server: "draft.example.test", port: 443, username: "draft")
        previewApp.presentEditor(nil); previewApp.editorName.stringValue = draft.name; previewApp.editorServer.stringValue = draft.server
        previewApp.editorPort.stringValue = "443"; previewApp.editorUser.stringValue = draft.username
        previewApp.saveEditor(); precondition(previewApp.sheetWindow == nil && previewApp.store.profiles.count == 4)
        previewApp.presentEditor(previewApp.selected); previewApp.editorName.stringValue = "预览编辑"
        previewApp.saveEditor(); precondition(previewApp.sheetWindow == nil && previewApp.selected?.name == "预览编辑")
        previewApp.presentEditor(nil); previewApp.editorServer.stringValue = "not a valid host !"; previewApp.saveEditor()
        precondition(previewApp.sheetWindow != nil && !previewApp.editorError.stringValue.isEmpty)
        previewApp.cancelEditor(); precondition(previewApp.sheetWindow == nil)
        let countBeforeCancel = previewApp.store.profiles.count
        previewApp.presentEditor(nil); previewApp.cancelEditor(); precondition(previewApp.store.profiles.count == countBeforeCancel)
        let changed = previewApp.store.profiles.first(where: { $0.name == "预览编辑" })!
        try! previewApp.commit(ProfileStorage.select("22222222-2222-4222-8222-222222222222", in: previewApp.store))
        previewApp.connectSelected(); precondition(previewApp.lastStatus?.profileID == "22222222-2222-4222-8222-222222222222")
        previewApp.disconnect(); precondition(previewApp.lastStatus?.state == .stopped)
        previewApp.forget(changed); precondition(previewApp.store.profiles.count == 3)
        precondition(Service.actionCallCount == 0)
        precondition(Self.editMenu().items.compactMap(\.keyEquivalent) == ["x", "c", "v", "", "a"])
        let lock = lockIcon(Self.gold, size: NSSize(width: 18, height: 18)); precondition(lock?.isTemplate == false)
        print("self-test: OK")
    }
    static func lockIcon(_ color: NSColor, size: NSSize, description: String = "SN VPN") -> NSImage? {
        guard let symbol = NSImage(systemSymbolName: "lock.fill", accessibilityDescription: description),
              let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 36, pixelsHigh: 36, bitsPerSample: 8,
                                            samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        bitmap.size = size
        guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return nil }
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = context
        let rect = NSRect(origin: .zero, size: size); symbol.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1)
        context.compositingOperation = .sourceIn; color.setFill(); rect.fill(); NSGraphicsContext.restoreGraphicsState()
        let result = NSImage(size: size); result.addRepresentation(bitmap); result.isTemplate = false; return result
    }
    static func renderLockPreview(at path: String) {
        let gray = NSColor(calibratedWhite: 0.5, alpha: 1)
        guard let goldIcon = lockIcon(Self.gold, size: NSSize(width: 28, height: 28)), let grayIcon = lockIcon(gray, size: NSSize(width: 28, height: 28)),
              let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 224, pixelsHigh: 224, bitsPerSample: 8,
                                            samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                            bytesPerRow: 0, bitsPerPixel: 0) else { return }
        bitmap.size = NSSize(width: 112, height: 112); guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return }
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = context
        NSColor(calibratedWhite: 0.95, alpha: 1).setFill(); NSRect(x: 0, y: 56, width: 112, height: 56).fill()
        NSColor(calibratedWhite: 0.12, alpha: 1).setFill(); NSRect(x: 0, y: 0, width: 112, height: 56).fill()
        goldIcon.draw(in: NSRect(x: 14, y: 70, width: 28, height: 28)); grayIcon.draw(in: NSRect(x: 70, y: 70, width: 28, height: 28))
        goldIcon.draw(in: NSRect(x: 14, y: 14, width: 28, height: 28)); grayIcon.draw(in: NSRect(x: 70, y: 14, width: 28, height: 28))
        NSGraphicsContext.restoreGraphicsState()
        if let png = bitmap.representation(using: .png, properties: [:]) { try? png.write(to: URL(fileURLWithPath: path)) }
    }
    static func makeIcon(at path: String) {
        let image = NSImage(size: NSSize(width: 128, height: 128)); image.lockFocus()
        NSColor(calibratedRed: 31/255, green: 44/255, blue: 61/255, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 8, y: 8, width: 112, height: 112), xRadius: 26, yRadius: 26).fill()
        Self.gold.setStroke(); Self.gold.setFill()
        let shackle = NSBezierPath(roundedRect: NSRect(x: 40, y: 62, width: 48, height: 40), xRadius: 23, yRadius: 23)
        shackle.lineWidth = 10; shackle.stroke()
        NSBezierPath(roundedRect: NSRect(x: 31, y: 28, width: 66, height: 54), xRadius: 10, yRadius: 10).fill()
        NSColor(calibratedRed: 31/255, green: 44/255, blue: 61/255, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: 60, y: 47, width: 8, height: 8)).fill()
        NSBezierPath(roundedRect: NSRect(x: 62.5, y: 38, width: 3, height: 12), xRadius: 1, yRadius: 1).fill()
        image.unlockFocus()
        if let data = image.tiffRepresentation, let rep = NSBitmapImageRep(data: data), let png = rep.representation(using: .png, properties: [:]) { try? png.write(to: URL(fileURLWithPath: path)) }
    }
}

@main
enum SNVPNMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = App(); app.delegate = delegate; app.setActivationPolicy(.accessory)
        withExtendedLifetime(delegate) { app.run() }
    }
}
