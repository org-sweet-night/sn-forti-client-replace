import CryptoKit
import Darwin
import Foundation
import Security

private let service = "local.vpn.autoconnect"
private let systemKeychainPath = "/Library/Keychains/System.keychain"
private let installedDirectory = "/Library/Application Support/Local VPN"

private struct Failure: Error {
    let message: String
    let status: OSStatus?

    init(message: String, status: OSStatus? = nil) {
        self.message = message
        self.status = status
    }
}

private func fail(_ message: String, _ status: OSStatus? = nil) -> Never {
    if let status {
        fputs("vpn-keychain-authorization: \(message) (status \(status))\n", stderr)
    } else {
        fputs("vpn-keychain-authorization: \(message)\n", stderr)
    }
    exit(1)
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String,
                     _ status: OSStatus? = nil) throws {
    if !condition() { throw Failure(message: message, status: status) }
}

private func checkedPath(_ path: String, requireExecutable: Bool) throws -> (String, stat) {
    guard path.hasPrefix("/"), !path.split(separator: "/").contains("..") else {
        throw Failure(message: "unsafe helper path")
    }
    let normalized = URL(fileURLWithPath: path).standardizedFileURL.path
    var current = "/"
    let components = normalized.split(separator: "/").map(String.init)
    var final = stat()
    for (index, component) in components.enumerated() {
        current = current == "/" ? current + component : current + "/" + component
        var info = stat()
        guard lstat(current, &info) == 0 else { throw Failure(message: "unsafe helper path") }
        let kind = info.st_mode & mode_t(S_IFMT)
        guard kind != mode_t(S_IFLNK), info.st_uid == 0 else {
            throw Failure(message: "unsafe helper path")
        }
        if info.st_mode & 0o022 != 0 {
            let allowedTemp = current == "/private/tmp" && index < components.count - 1
                && kind == mode_t(S_IFDIR) && info.st_mode & mode_t(S_ISVTX) != 0
            guard allowedTemp else { throw Failure(message: "unsafe helper path") }
        }
        final = info
    }
    guard final.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), final.st_mode & 0o111 != 0 else {
        throw Failure(message: "unsafe helper binary")
    }
    if requireExecutable {
        var attributes = stat()
        guard lstat(normalized, &attributes) == 0,
              attributes.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG) else {
            throw Failure(message: "unsafe helper binary")
        }
    }
    return (normalized, final)
}

private func sha256(_ path: String) throws -> String {
    let bytes = try Data(contentsOf: URL(fileURLWithPath: path), options: .mappedIfSafe)
    return SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
}

private func verifyCodeSignature(_ path: String) throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    process.arguments = ["--verify", "--strict", path]
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else { throw Failure(message: "helper signature invalid") }
}

private func profileAccount(_ value: String) -> Bool {
    let pattern = #"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"#
    return value.range(of: pattern, options: .regularExpression) != nil
}

private func accountValue(_ value: String) throws -> (String, String) {
    if value == "primary" { return ("primary", "vpn-keychain") }
    guard profileAccount(value) else { throw Failure(message: "invalid account selector") }
    return ("profile-" + value, "vpn-profile-keychain")
}

private func trustedApplication(_ path: String) throws -> SecTrustedApplication {
    var app: SecTrustedApplication?
    let status = path.withCString { SecTrustedApplicationCreateFromPath($0, &app) }
    guard status == errSecSuccess, let app else {
        throw Failure(message: "could not identify helper", status: status)
    }
    return app
}

private func trustedData(_ app: SecTrustedApplication) throws -> Data {
    var result: CFData?
    let status = SecTrustedApplicationCopyData(app, &result)
    guard status == errSecSuccess, let result else {
        throw Failure(message: "could not compare trusted helper", status: status)
    }
    return result as Data
}

private func keychainItem(_ account: String) throws -> SecKeychainItem {
    var keychain: SecKeychain?
    var status = SecKeychainOpen(systemKeychainPath, &keychain)
    guard status == errSecSuccess, let keychain else {
        throw Failure(message: "could not open system keychain", status: status)
    }
    var item: SecKeychainItem?
    status = service.withCString { serviceName in
        account.withCString { accountName in
            SecKeychainFindGenericPassword(keychain, UInt32(service.utf8.count), serviceName,
                                           UInt32(account.utf8.count), accountName, nil, nil, &item)
        }
    }
    guard status == errSecSuccess, let item else {
        throw Failure(message: "credential item not found", status: status)
    }
    return item
}

private struct DecryptACL {
    let acl: SecACL
    let applications: CFArray?
    let description: CFString
    let promptSelector: SecKeychainPromptSelector
}

private func decryptACLs(_ item: SecKeychainItem, access: inout SecAccess?) throws -> [DecryptACL] {
    var status = SecKeychainItemCopyAccess(item, &access)
    guard status == errSecSuccess, let access else {
        throw Failure(message: "could not read item access rules", status: status)
    }
    var list: CFArray?
    status = SecAccessCopyACLList(access, &list)
    guard status == errSecSuccess, let list, let entries = list as? [SecACL] else {
        throw Failure(message: "could not enumerate item access rules", status: status)
    }
    var result: [DecryptACL] = []
    for acl in entries {
        let authorizations = SecACLCopyAuthorizations(acl) as? [CFString] ?? []
        guard authorizations.contains(where: { CFEqual($0, kSecACLAuthorizationDecrypt) }) else { continue }
        var applications: CFArray?
        var description: CFString?
        var selector = SecKeychainPromptSelector(rawValue: 0)
        status = SecACLCopyContents(acl, &applications, &description, &selector)
        guard status == errSecSuccess, let description else {
            throw Failure(message: "could not inspect decrypt access rule", status: status)
        }
        result.append(DecryptACL(acl: acl, applications: applications,
                                 description: description, promptSelector: selector))
    }
    guard result.count == 1 else { throw Failure(message: "decrypt rule is ambiguous or missing") }
    return result
}

private func matchCount(_ applications: CFArray?, identity: Data) throws -> Int? {
    guard let applications else { return nil } // nil means all applications are trusted.
    guard let values = applications as? [SecTrustedApplication] else {
        throw Failure(message: "invalid trusted application list")
    }
    var matches = 0
    for value in values where try trustedData(value) == identity { matches += 1 }
    return matches
}

private func installedHelper(_ name: String) -> String {
    installedDirectory + "/" + name
}

private func run(mode: String, newHelperPath: String, accountSelector: String, expectedHash: String) throws {
    guard geteuid() == 0 else { throw Failure(message: "root required") }
    guard expectedHash.range(of: #"^[0-9a-fA-F]{64}$"#, options: .regularExpression) != nil else {
        throw Failure(message: "invalid expected hash")
    }
    let (account, helperName) = try accountValue(accountSelector)
    let (candidate, _) = try checkedPath(newHelperPath, requireExecutable: true)
    guard URL(fileURLWithPath: candidate).lastPathComponent == helperName else {
        throw Failure(message: "helper does not match account type")
    }
    guard try sha256(candidate).caseInsensitiveCompare(expectedHash) == .orderedSame else {
        throw Failure(message: "helper hash mismatch")
    }
    try verifyCodeSignature(candidate)
    let candidateIdentity = try trustedApplication(candidate)
    let candidateData = try trustedData(candidateIdentity)
    let oldPath = installedHelper(helperName)
    let oldIdentity: SecTrustedApplication? = {
        guard let (path, _) = try? checkedPath(oldPath, requireExecutable: true) else { return nil }
        return try? trustedApplication(path)
    }()
    let oldData = try oldIdentity.map(trustedData)

    let item = try keychainItem(account)
    var access: SecAccess?
    let acl = try decryptACLs(item, access: &access)[0]
    let newMatches = try matchCount(acl.applications, identity: candidateData)
    let oldMatches: Int? = try oldData.flatMap { try matchCount(acl.applications, identity: $0) }
    if mode == "audit" {
        let old = oldMatches.map(String.init) ?? "unknown"
        if let newMatches {
            print("audit: decrypt_acls=1 old_helper_matches=\(old) new_helper_matches=\(newMatches)")
        } else {
            print("audit: decrypt_acls=1 old_helper_matches=\(old) new_helper_matches=all-apps")
        }
        return
    }
    guard mode == "authorize" else { throw Failure(message: "invalid operation") }
    guard let applications = acl.applications else {
        print("authorize: already authorized by all-apps ACL; unchanged")
        return
    }
    guard let list = applications as? [SecTrustedApplication] else {
        throw Failure(message: "invalid trusted application list")
    }
    // A legacy opaque entry can compare equal while the current binary still
    // needs interaction. Explicit reauthorization binds a fresh application
    // object to the installed binary and retains every existing trust entry.
    var updated = list
    updated.append(candidateIdentity)
    let setACLStatus = SecACLSetContents(acl.acl, updated as CFArray,
                                         acl.description, acl.promptSelector)
    guard setACLStatus == errSecSuccess else {
        let restoreStatus = SecACLSetContents(acl.acl, applications,
                                              acl.description, acl.promptSelector)
        if restoreStatus != errSecSuccess {
            throw Failure(message: "ACL update failed and in-memory restore failed", status: restoreStatus)
        }
        throw Failure(message: "could not update decrypt access rule", status: setACLStatus)
    }
    guard let access else { throw Failure(message: "missing access object") }
    let persistStatus = SecKeychainItemSetAccess(item, access)
    guard persistStatus == errSecSuccess else {
        _ = SecACLSetContents(acl.acl, applications, acl.description, acl.promptSelector)
        throw Failure(message: "could not save access rule; original list restored in memory", status: persistStatus)
    }
    print("authorize: helper added to decrypt ACL; credential data unchanged")
}

let args = Array(CommandLine.arguments.dropFirst())
guard args.count == 5, args[0] == "--audit" || args[0] == "--authorize",
      args[3] == "--expected-sha256" else {
    fputs("usage: vpn-keychain-authorization --audit|--authorize NEWHELPER ACCOUNT --expected-sha256 HASH\n", stderr)
    exit(2)
}
do {
    try run(mode: args[0] == "--audit" ? "audit" : "authorize",
            newHelperPath: args[1], accountSelector: args[2], expectedHash: args[4])
} catch let error as Failure {
    fail(error.message, error.status)
} catch {
    fail("operation failed")
}
