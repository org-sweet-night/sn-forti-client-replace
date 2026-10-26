import AppKit
import Sparkle

@MainActor
final class ConsentedUserDriver: SPUStandardUserDriver {
    // Set only by the updater's actual standard release-notes Install choice,
    // after the selected signed package passed the interruption consent gate.
    private(set) var permitsAutomaticRelaunch = false
    private var cancelDownload: (() -> Void)?
    private var expectedBytes: UInt64 = 0
    private var receivedBytes: UInt64 = 0
    private var preparing = false

    override func showDownloadInitiated(cancellation: @escaping () -> Void) {
        cancelDownload = cancellation
        expectedBytes = 0; receivedBytes = 0; preparing = false
    }

    override func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {
        expectedBytes = expectedContentLength
    }

    override func showDownloadDidReceiveData(ofLength length: UInt64) {
        let total = receivedBytes.addingReportingOverflow(length)
        receivedBytes = total.overflow ? UInt64.max : total.partialValue
    }

    override func showDownloadDidStartExtractingUpdate() {
        cancelDownload = nil
        preparing = true
    }

    override func showExtractionReceivedProgress(_ progress: Double) {}

    override func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool, retryTerminatingApplication: @escaping () -> Void) {
        preparing = false
        // Preserve Sparkle's public retry flow if the target refuses to quit.
        super.showInstallingUpdate(withApplicationTerminated: applicationTerminated,
                                   retryTerminatingApplication: retryTerminatingApplication)
    }

    override func showUpdateInFocus() {
        guard cancelDownload != nil || preparing else { super.showUpdateInFocus(); return }
        let alert = NSAlert()
        alert.messageText = preparing ? "正在准备安装更新" : "正在后台下载更新"
        if preparing {
            alert.informativeText = "下载已完成，正在验证并准备完整升级。系统可能请求管理员授权。"
            alert.addButton(withTitle: "继续后台处理")
        } else {
            let received = ByteCountFormatter.string(fromByteCount: Int64(clamping: receivedBytes), countStyle: .file)
            let expected = ByteCountFormatter.string(fromByteCount: Int64(clamping: expectedBytes), countStyle: .file)
            alert.informativeText = expectedBytes > 0 ? "已下载 \(received)，共 \(expected)。完成后将安装并重新打开 \(Brand.displayName)。" : "已下载 \(received)。完成后将安装并重新打开 \(Brand.displayName)。"
            alert.addButton(withTitle: "继续后台下载")
            alert.addButton(withTitle: "取消更新")
        }
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertSecondButtonReturn { cancelBackgroundDownload() }
    }

    func cancelBackgroundDownload() {
        guard let cancellation = cancelDownload else { return }
        cancelDownload = nil
        permitsAutomaticRelaunch = false
        cancellation()
    }

    override func dismissUpdateInstallation() {
        cancelDownload = nil; preparing = false; permitsAutomaticRelaunch = false
        super.dismissUpdateInstallation()
    }

    func recordChoice(_ choice: SPUUserUpdateChoice, interruptionConsented: Bool, packageAllowed: Bool) {
        permitsAutomaticRelaunch = choice == .install && interruptionConsented && packageAllowed
    }

    override func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        if permitsAutomaticRelaunch { reply(.install) }
        else { super.showReady(toInstallAndRelaunch: reply) }
    }
}

@MainActor
final class UpdaterApp: NSObject, NSApplicationDelegate, SPUUpdaterDelegate {
    private var updater: SPUUpdater?
    private var driver: ConsentedUserDriver?
    private var manual = CommandLine.arguments.contains("--manual")
    private var consentedVersion: String?
    private var configuredFeed: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The sidecar is nested at Main.app/Contents/Helpers/Updater.app.
        let hostURL = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        guard hostURL.path == Brand.installPath,
              Bundle.main.bundleURL.path == Brand.installPath + "/Contents/Helpers/\(Brand.updaterName).app",
              let host = Bundle(url: hostURL), host.bundleIdentifier == Brand.bundleID,
              UpdateConfiguration.isValid(host.infoDictionary ?? [:]),
              host.object(forInfoDictionaryKey: "SUAutomaticallyUpdate") as? Bool == false,
              host.object(forInfoDictionaryKey: "SUAllowsAutomaticUpdates") as? Bool == false,
              host.object(forInfoDictionaryKey: "SUVerifyUpdateBeforeExtraction") as? Bool == true,
              host.object(forInfoDictionaryKey: "SURequireSignedFeed") as? Bool == true else {
            fail("更新配置缺失或不安全，尚未下载更新。请联系发布者。")
            return
        }
        configuredFeed = host.object(forInfoDictionaryKey: "SUFeedURL") as? String
        let driver = ConsentedUserDriver(hostBundle: host, delegate: nil)
        let updater = SPUUpdater(hostBundle: host, applicationBundle: host, userDriver: driver, delegate: self)
        self.driver = driver; self.updater = updater
        do {
            try updater.start()
            if manual { updater.checkForUpdates() }
            else if updater.automaticallyChecksForUpdates { updater.checkForUpdatesInBackground() }
            else { NSApp.terminate(nil) }
        } catch { fail("更新器无法启动，尚未下载更新。请联系发布者检查更新配置。") }
    }

    func feedURLString(for updater: SPUUpdater) -> String? { configuredFeed }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        manual = true
        updater?.checkForUpdates()
        return true
    }

    func updater(_ updater: SPUUpdater, shouldProceedWithUpdate updateItem: SUAppcastItem, updateCheck: SPUUpdateCheck) throws {
        driver?.recordChoice(.dismiss, interruptionConsented: false, packageAllowed: false)
        guard updateItem.installationType == "package",
              UpdateConfiguration.isValidPackage(updateItem.fileURL, feed: configuredFeed) else {
            throw NSError(domain: Brand.updaterID, code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "更新必须是同一 GitHub 仓库通过 HTTPS 发布的完整 .pkg 安装包；已拒绝此更新。"])
        }
        guard consentedVersion != updateItem.versionString else { return }
        // This delegate runs before Sparkle offers or downloads the selected payload.
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "发现 \(Brand.displayName) 更新"
        alert.informativeText = "接下来可查看版本说明，再决定是否下载。安装将更新 App、VPN 辅助服务和运行组件，需要管理员授权，并会暂时断开 VPN。继续表示允许安装时暂时断开；下载前仍可取消。"
        alert.addButton(withTitle: "查看版本说明")
        alert.addButton(withTitle: "暂不更新")
        guard alert.runModal() == .alertFirstButtonReturn else {
            throw NSError(domain: NSCocoaErrorDomain, code: NSUserCancelledError,
                          userInfo: [NSLocalizedDescriptionKey: "已取消更新，尚未下载更新。"])
        }
        consentedVersion = updateItem.versionString
    }

    func updater(_ updater: SPUUpdater, userDidMake choice: SPUUserUpdateChoice, forUpdate updateItem: SUAppcastItem, state: SPUUserUpdateState) {
        driver?.recordChoice(choice, interruptionConsented: consentedVersion == updateItem.versionString,
                             packageAllowed: updateItem.installationType == "package"
                                && UpdateConfiguration.isValidPackage(updateItem.fileURL, feed: configuredFeed))
    }

    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        // Sparkle's standard driver presents manual errors and successful/no-update results.
        // A failed background check stays quiet; no synthetic 'up to date' result is shown.
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }

    private func fail(_ message: String) {
        if manual {
            let alert = NSAlert(); alert.messageText = "无法检查更新"; alert.informativeText = message
            alert.addButton(withTitle: "好"); NSApp.activate(ignoringOtherApps: true); alert.runModal()
        }
        NSApp.terminate(nil)
    }
}

@main
enum UpdaterMain {
    static func main() {
        if CommandLine.arguments.contains("--self-test") {
            UpdateConfiguration.selfTest()
            let driver = ConsentedUserDriver(hostBundle: Bundle.main, delegate: nil)
            precondition(!driver.permitsAutomaticRelaunch)
            for choice in [SPUUserUpdateChoice.dismiss, .skip] {
                driver.recordChoice(choice, interruptionConsented: true, packageAllowed: true)
                precondition(!driver.permitsAutomaticRelaunch)
            }
            driver.recordChoice(.install, interruptionConsented: false, packageAllowed: true)
            precondition(!driver.permitsAutomaticRelaunch)
            driver.recordChoice(.install, interruptionConsented: true, packageAllowed: false)
            precondition(!driver.permitsAutomaticRelaunch)
            driver.recordChoice(.install, interruptionConsented: true, packageAllowed: true)
            var replies = 0
            driver.showReady(toInstallAndRelaunch: { choice in
                precondition(choice == .install); replies += 1
            })
            precondition(replies == 1)
            var cancellations = 0
            driver.showDownloadInitiated { cancellations += 1 }
            driver.showDownloadDidReceiveExpectedContentLength(100)
            driver.showDownloadDidReceiveData(ofLength: 20)
            driver.cancelBackgroundDownload(); driver.cancelBackgroundDownload()
            precondition(cancellations == 1 && !driver.permitsAutomaticRelaunch)
            driver.recordChoice(.install, interruptionConsented: true, packageAllowed: true)
            driver.showDownloadInitiated { cancellations += 1 }
            driver.showDownloadDidStartExtractingUpdate()
            driver.cancelBackgroundDownload()
            precondition(cancellations == 1 && driver.permitsAutomaticRelaunch)
            print("Updater configuration self-test: OK")
            return
        }
        let app = NSApplication.shared
        let delegate = UpdaterApp()
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
