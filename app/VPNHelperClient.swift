import Foundation

enum VPNHelperClient {
    static func validRequirement(_ value: String) -> Bool {
        value.range(of: #"\Aidentifier "local\.snvpn\.helper" and cdhash H"[0-9a-fA-F]{40}"\z"#,
                    options: .regularExpression) != nil
    }

    static func request(_ operation: String, payload: Data?, completion: @escaping (Result<Data, Error>) -> Void) {
        guard VPNHelperContract.validRequest(operation, payload: payload) else {
            completion(.failure(AppError.failed("VPN 操作参数无效。"))); return
        }
        guard let url = Bundle.main.url(forResource: "helper-requirement", withExtension: "txt"),
              let requirement = try? String(contentsOf: url, encoding: .utf8), validRequirement(requirement) else {
            completion(.failure(AppError.failed("VPN 助手版本无效，请重新安装新版 App 和助手。"))); return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let connection = NSXPCConnection(machServiceName: VPNHelperContract.serviceName, options: .privileged)
            connection.setCodeSigningRequirement(requirement)
            connection.remoteObjectInterface = NSXPCInterface(with: VPNHelperProtocol.self)
            let completed = DispatchSemaphore(value: 0), lock = NSLock()
            var result: Result<Data, Error>?
            let finish: (Result<Data, Error>) -> Void = { value in
                lock.lock()
                let first = result == nil
                if first { result = value }
                lock.unlock()
                if first { completed.signal() }
            }
            let unavailable = AppError.failed("无法连接 VPN 助手，请确认 App 与助手已一同安装更新。")
            connection.invalidationHandler = { finish(.failure(unavailable)) }
            connection.interruptionHandler = { finish(.failure(unavailable)) }
            connection.resume()
            let proxy = connection.remoteObjectProxyWithErrorHandler { _ in finish(.failure(unavailable)) }
            if let service = proxy as? VPNHelperProtocol {
                service.perform(operation, payload: payload) { data in
                    guard data.count <= 65536 else { finish(.failure(AppError.invalidReply)); return }
                    finish(.success(data))
                }
            } else { finish(.failure(unavailable)) }
            if completed.wait(timeout: .now() + 970) == .timedOut { finish(.failure(AppError.timeout)) }
            connection.invalidate()
            lock.lock(); let value = result ?? .failure(AppError.timeout); lock.unlock()
            completion(value)
        }
    }
}
