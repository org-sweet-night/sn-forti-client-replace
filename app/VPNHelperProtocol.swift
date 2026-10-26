import Foundation

@objc(VPNHelperProtocol) protocol VPNHelperProtocol {
    func perform(_ operation: String, payload: Data?, reply: @escaping (Data) -> Void)
}

enum VPNHelperContract {
    static let serviceName = Brand.helperLabel
    static let installedBase = "/Library/Application Support/Local VPN"
    static let helperFile = "vpn-helper"
    static let maxPayload = 4096

    static func validRequest(_ operation: String, payload: Data?) -> Bool {
        switch operation {
        case "ping", "stop": return payload == nil
        case "connect-profile", "forget-profile":
            guard let payload, payload.count <= maxPayload,
                  let object = try? JSONSerialization.jsonObject(with: payload),
                  object is [String: Any] else { return false }
            return true
        default: return false
        }
    }

    static func failure(_ message: String) -> Data {
        try! JSONSerialization.data(withJSONObject: ["ok": false, "message": message])
    }
}
