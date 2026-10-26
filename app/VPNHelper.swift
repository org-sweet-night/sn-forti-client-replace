import Foundation
import Darwin

private struct HelperPolicy: Decodable {
    let version: Int
    let userID: UInt32
    let clientRequirement: String

    var valid: Bool {
        version == 1 && userID > 0 && userID < UInt32.max &&
        clientRequirement.range(of: #"\Aidentifier ""# + NSRegularExpression.escapedPattern(for: Brand.bundleID) + #"" and cdhash H"[0-9a-fA-F]{40}"\z"#,
                                options: .regularExpression) != nil
    }
}

private func trustedPath(_ path: String, directory: Bool = false) -> Bool {
    var info = stat()
    guard lstat(path, &info) == 0, info.st_uid == 0, info.st_mode & 0o022 == 0 else { return false }
    return info.st_mode & S_IFMT == (directory ? S_IFDIR : S_IFREG)
}

private final class HelperOutput: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    private var overflow = false
    private var stopped = false
    var cancelled: Bool { lock.lock(); defer { lock.unlock() }; return stopped }
    func cancel() { lock.lock(); stopped = true; lock.unlock() }
    func append(_ chunk: Data) {
        lock.lock(); defer { lock.unlock() }
        if data.count + chunk.count <= 65536 { data.append(chunk) } else { overflow = true }
    }
    func snapshot() -> (Data, Bool) { lock.lock(); defer { lock.unlock() }; return (data, overflow) }
}

private final class Helper: NSObject, NSXPCListenerDelegate, VPNHelperProtocol {
    private let policy: HelperPolicy
    private let queue = DispatchQueue(label: Brand.helperLabel + ".operations")
    private let state = NSLock()
    private var busy = false
    private var lastActivity = Date()
    private var timer: DispatchSourceTimer?

    init(policy: HelperPolicy) {
        self.policy = policy
        super.init()
        let timer = DispatchSource.makeTimerSource(queue: .global())
        timer.schedule(deadline: .now() + 60, repeating: 10)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.state.lock()
            let idle = !self.busy && Date().timeIntervalSince(self.lastActivity) >= 60
            self.state.unlock()
            if idle { exit(0) }
        }
        self.timer = timer
        timer.resume()
    }

    func listener(_ listener: NSXPCListener, shouldAcceptNewConnection connection: NSXPCConnection) -> Bool {
        guard connection.effectiveUserIdentifier == policy.userID else { return false }
        connection.setCodeSigningRequirement(policy.clientRequirement)
        connection.exportedInterface = NSXPCInterface(with: VPNHelperProtocol.self)
        connection.exportedObject = self
        connection.resume()
        return true
    }

    func perform(_ operation: String, payload: Data?, reply: @escaping (Data) -> Void) {
        guard VPNHelperContract.validRequest(operation, payload: payload) else {
            reply(VPNHelperContract.failure("VPN 请求无效")); return
        }
        state.lock()
        lastActivity = Date()
        guard !busy else {
            state.unlock()
            reply(VPNHelperContract.failure("另一个 VPN 操作正在进行")); return
        }
        busy = true
        state.unlock()
        queue.async {
            let response = self.run(operation == "ping" ? "status" : operation, payload: payload)
            let result: Data
            if operation == "ping" {
                let status = (try? JSONSerialization.jsonObject(with: response)) as? [String: Any]
                if status?["state"] is String, status?["reachable"] is Bool, status?["message"] is String {
                    result = Data(#"{"ok":true,"protocol":1,"message":"VPN 助手已就绪"}"#.utf8)
                } else if status?["ok"] as? Bool == false, status?["message"] is String {
                    result = response
                } else {
                    result = VPNHelperContract.failure("VPN 助手状态校验失败")
                }
            } else {
                result = response
            }
            self.state.lock()
            self.busy = false
            self.lastActivity = Date()
            self.state.unlock()
            reply(result)
        }
    }

    private func run(_ operation: String, payload: Data?) -> Data {
        let base = VPNHelperContract.installedBase
        guard trustedPath(base, directory: true), trustedPath(base + "/control.py") else {
            return VPNHelperContract.failure("VPN 助手安装校验失败")
        }
        var temporary: String?
        defer { if let temporary { unlink(temporary) } }
        if let payload {
            var template = Array((base + "/.request.XXXXXX").utf8CString)
            let fd = mkstemp(&template)
            guard fd >= 0 else { return VPNHelperContract.failure("无法创建 VPN 请求") }
            let path = String(cString: template)
            temporary = path
            guard fchmod(fd, 0o600) == 0 else {
                close(fd); return VPNHelperContract.failure("无法保护 VPN 请求")
            }
            do {
                let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
                try file.write(contentsOf: payload)
                try file.close()
            } catch { return VPNHelperContract.failure("无法写入 VPN 请求") }
        }
        var descriptors: [Int32] = [0, 0]
        guard pipe(&descriptors) == 0 else { return VPNHelperContract.failure("无法创建 VPN 通道") }
        let readFD = descriptors[0], writeFD = descriptors[1]
        var actions: posix_spawn_file_actions_t?
        var attributes: posix_spawnattr_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else {
            close(readFD); close(writeFD); return VPNHelperContract.failure("无法准备 VPN 操作")
        }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawnattr_init(&attributes) == 0 else {
            close(readFD); close(writeFD); return VPNHelperContract.failure("无法准备 VPN 操作")
        }
        defer { posix_spawnattr_destroy(&attributes) }
        guard posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0,
              posix_spawn_file_actions_adddup2(&actions, writeFD, STDOUT_FILENO) == 0,
              posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0) == 0,
              posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) == 0 else {
            close(readFD); close(writeFD); return VPNHelperContract.failure("无法准备 VPN 操作")
        }
        // asuser supplies the GUI bootstrap context; root is required by control.py and the System keychain.
        let arguments = ["/bin/launchctl", "asuser", String(policy.userID), "/usr/bin/python3", "-I", base + "/control.py", operation] + (temporary.map { [$0] } ?? [])
        let argv = arguments.map { strdup($0) } + [nil]
        let environmentValues: [String] = ["PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=/var/root", "LANG=en_US.UTF-8"]
        let environment = environmentValues.map { strdup($0) } + [nil]
        defer { argv.forEach { free($0) }; environment.forEach { free($0) } }
        var pid: pid_t = 0
        let spawnResult = argv.withUnsafeBufferPointer { args in
            environment.withUnsafeBufferPointer { env in
                posix_spawn(&pid, "/bin/launchctl", &actions, &attributes, args.baseAddress!, env.baseAddress!)
            }
        }
        close(writeFD)
        guard spawnResult == 0 else {
            close(readFD); return VPNHelperContract.failure("无法启动 VPN 操作")
        }
        let childPID = pid
        let completed = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            var info = siginfo_t()
            // Keep the child waitable until group cleanup so its PID cannot be reused.
            while waitid(P_PID, id_t(childPID), &info, WEXITED | WNOWAIT) == -1 && errno == EINTR {}
            completed.signal()
        }
        let output = HelperOutput()
        let reader = DispatchGroup()
        reader.enter()
        DispatchQueue.global().async {
            defer { close(readFD); reader.leave() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while !output.cancelled {
                var descriptor = pollfd(fd: readFD, events: Int16(POLLIN), revents: 0)
                let ready = poll(&descriptor, 1, 100)
                if ready < 0 { if errno == EINTR { continue }; break }
                if ready == 0 { continue }
                let count = read(readFD, &buffer, buffer.count)
                if count <= 0 { break }
                output.append(Data(buffer.prefix(count)))
            }
        }
        let timedOut = completed.wait(timeout: .now() + 960) == .timedOut
        if timedOut {
            kill(-childPID, SIGTERM)
            _ = completed.wait(timeout: .now() + 2)
        }
        // Also remove descendants that retained stdout after the direct child exited.
        kill(-childPID, SIGKILL)
        var status: Int32 = 0
        var waited: pid_t
        repeat { waited = waitpid(childPID, &status, 0) } while waited == -1 && errno == EINTR
        if reader.wait(timeout: .now() + 2) == .timedOut {
            output.cancel()
            _ = reader.wait(timeout: .now() + 1)
        }
        if timedOut { return VPNHelperContract.failure("VPN 操作超时，请检查连接状态后再重试") }
        guard waited == childPID, status == 0 else { return VPNHelperContract.failure("VPN 操作失败，请检查连接状态") }
        let (result, tooLarge) = output.snapshot()
        guard !tooLarge, let object = try? JSONSerialization.jsonObject(with: result), object is [String: Any] else {
            return VPNHelperContract.failure("VPN 操作返回无效结果")
        }
        return result
    }
}

@main
private enum VPNHelperMain {
    static func main() {
        if CommandLine.arguments == [CommandLine.arguments[0], "--self-test"] {
            precondition(VPNHelperContract.validRequest("ping", payload: nil))
            precondition(!VPNHelperContract.validRequest("start", payload: nil))
            precondition(!VPNHelperContract.validRequest("stop", payload: Data()))
            precondition(VPNHelperContract.validRequest("connect-profile", payload: Data("{}".utf8)))
            precondition(!VPNHelperContract.validRequest("connect-profile", payload: Data("[]".utf8)))
            precondition(!VPNHelperContract.validRequest("forget-profile", payload: Data(repeating: 32, count: 4097)))
            precondition(!HelperPolicy(version: 1, userID: 0, clientRequirement: "").valid)
            let requirement = "identifier \"\(Brand.bundleID)\" and cdhash H\"" + String(repeating: "a", count: 40) + "\""
            precondition(HelperPolicy(version: 1, userID: 501, clientRequirement: requirement).valid)
            precondition(!HelperPolicy(version: 1, userID: 501, clientRequirement: requirement + "\n").valid)
            print("VPN helper self-test passed")
            return
        }
        let base = VPNHelperContract.installedBase
        let policyPath = base + "/helper-policy.plist"
        guard geteuid() == 0,
              trustedPath("/Library", directory: true),
              trustedPath("/Library/Application Support", directory: true),
              trustedPath(base, directory: true), trustedPath(policyPath),
              let data = try? Data(contentsOf: URL(fileURLWithPath: policyPath)), data.count <= 4096,
              let policy = try? PropertyListDecoder().decode(HelperPolicy.self, from: data), policy.valid else { exit(1) }
        let helper = Helper(policy: policy)
        let listener = NSXPCListener(machServiceName: VPNHelperContract.serviceName)
        listener.setConnectionCodeSigningRequirement(policy.clientRequirement)
        listener.delegate = helper
        listener.resume()
        withExtendedLifetime(helper) { RunLoop.current.run() }
    }
}
