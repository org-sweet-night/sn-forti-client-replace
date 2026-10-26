import Foundation
import SystemConfiguration
import Darwin

private final class WeakStatusMonitor {
    weak var value: StatusMonitor?
    init(_ value: StatusMonitor) { self.value = value }
}

final class StatusMonitor {
    private let directory: URL
    private let watchNetwork: Bool
    private let changed: () -> Void
    private let queue = DispatchQueue(label: "StatusMonitor")
    private let lock = NSLock()
    private var fileSource: DispatchSourceFileSystemObject?
    private var fileDescriptor: Int32 = -1
    private var statusFingerprint: Data?
    private var store: SCDynamicStore?
    private var pending: DispatchWorkItem?
    private var started = false

    init(directory: URL, watchNetwork: Bool = true, changed: @escaping () -> Void) {
        self.directory = directory
        self.watchNetwork = watchNetwork
        self.changed = changed
    }

    func start() {
        lock.lock()
        started = true
        if fileSource == nil {
            let fd = open(directory.path, O_EVTONLY | O_CLOEXEC)
            if fd >= 0 {
                fileDescriptor = fd
                statusFingerprint = Self.statusFingerprint(in: directory)
                let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete], queue: queue)
                source.setEventHandler { [weak self] in self?.directoryChanged() }
                source.setCancelHandler { close(fd) }
                fileSource = source
                source.resume()
            }
        }

        guard watchNetwork, store == nil else { lock.unlock(); return }
        let weakMonitor = WeakStatusMonitor(self)
        var context = SCDynamicStoreContext(
            version: 0,
            info: Unmanaged.passUnretained(weakMonitor).toOpaque(),
            retain: { info in UnsafeRawPointer(Unmanaged<WeakStatusMonitor>.fromOpaque(info).retain().toOpaque()) },
            release: { info in Unmanaged<WeakStatusMonitor>.fromOpaque(info).release() },
            copyDescription: nil
        )
        store = SCDynamicStoreCreate(nil, "StatusMonitor" as CFString, { _, _, context in
            guard let context else { return }
            Unmanaged<WeakStatusMonitor>.fromOpaque(context).takeUnretainedValue().value?.scheduleChange()
        }, &context)
        guard let store else { lock.unlock(); return }
        let patterns = [
            "State:/Network/Global/DNS", "State:/Network/Global/IPv4", "State:/Network/Global/IPv6",
            "State:/Network/Interface/.*/IPv4", "State:/Network/Interface/.*/IPv6",
            "State:/Network/Service/.*/DNS", "State:/Network/Service/.*/IPv4", "State:/Network/Service/.*/IPv6"
        ] as CFArray
        guard SCDynamicStoreSetNotificationKeys(store, nil, patterns), SCDynamicStoreSetDispatchQueue(store, queue) else {
            self.store = nil
            lock.unlock()
            SCDynamicStoreSetDispatchQueue(store, nil)
            return
        }
        lock.unlock()
    }

    func stop() {
        lock.lock()
        started = false
        pending?.cancel()
        pending = nil
        let store = self.store
        let source = fileSource
        self.store = nil
        fileSource = nil
        fileDescriptor = -1
        statusFingerprint = nil
        lock.unlock()
        if let store { SCDynamicStoreSetDispatchQueue(store, nil) }
        source?.cancel()
    }

    deinit { stop() }

    private func directoryChanged() {
        lock.lock()
        guard started, let source = fileSource else { lock.unlock(); return }
        let events = source.data
        if events.contains(.delete) || events.contains(.rename) {
            fileSource = nil
            fileDescriptor = -1
            statusFingerprint = nil
            lock.unlock()
            source.cancel()
            return
        }
        let fingerprint = Self.statusFingerprint(in: directory)
        guard fingerprint != statusFingerprint else { lock.unlock(); return }
        statusFingerprint = fingerprint
        lock.unlock()
        scheduleChange()
    }

    private static func statusFingerprint(in directory: URL) -> Data? {
        let url = directory.appendingPathComponent("status.json")
        guard let file = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? file.close() }
        guard let data = try? file.read(upToCount: 65_537) else { return Data("unreadable".utf8) }
        guard data.count <= 65_536 else { return Data("oversize".utf8) }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return Data("invalid".utf8) }
        let relevant = object.filter { ["state", "error", "profile_id", "reachable", "autostart", "message"].contains($0.key) }
        return (try? JSONSerialization.data(withJSONObject: relevant, options: [.sortedKeys])) ?? Data("invalid".utf8)
    }

    private func scheduleChange() {
        lock.lock()
        guard started, pending == nil else { lock.unlock(); return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.lock.lock()
            guard self.started else { self.lock.unlock(); return }
            self.pending = nil
            self.lock.unlock()
            self.changed()
        }
        pending = work
        lock.unlock()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    static func selfTest() {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        var callbacks = 0
        let monitor = StatusMonitor(directory: directory, watchNetwork: false) { callbacks += 1 }
        monitor.start()
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let movedDirectory = directory.appendingPathExtension("moved")
        defer {
            try? FileManager.default.removeItem(at: directory)
            try? FileManager.default.removeItem(at: movedDirectory)
        }

        let status = directory.appendingPathComponent("status.json")
        func replace(_ value: String, time: Int) {
            try! Data("{\"state\":\"\(value)\",\"profile_id\":\"p\",\"time\":\(time)}".utf8).write(to: status, options: .atomic)
        }
        replace("stopped", time: 1)
        monitor.start() // Retry the missing directory watch with an initial snapshot.
        replace("stopped", time: 2)
        RunLoop.current.run(until: Date().addingTimeInterval(0.7))
        precondition(callbacks == 0, "time-only status changes must be ignored")
        try! Data("noise".utf8).write(to: directory.appendingPathComponent("unrelated"), options: .atomic)
        RunLoop.current.run(until: Date().addingTimeInterval(0.7))
        precondition(callbacks == 0, "unrelated files must not trigger a change")

        try! FileManager.default.moveItem(at: directory, to: movedDirectory)
        RunLoop.current.run(until: Date().addingTimeInterval(0.7))
        precondition(callbacks == 0, "directory replacement alone must not trigger a change")
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        replace("stopped", time: 3)
        monitor.start()

        for (index, state) in ["connecting", "connected", "degraded", "stopped"].enumerated() {
            replace(state, time: index + 4)
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        let deadline = Date().addingTimeInterval(3)
        while callbacks == 0 && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
        precondition(callbacks == 1, "atomic status replacements should coalesce")
        monitor.stop()
        replace("error", time: 8)
        RunLoop.current.run(until: Date().addingTimeInterval(0.7))
        precondition(callbacks == 1, "stop should prevent callbacks")
    }
}
