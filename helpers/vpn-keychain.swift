import Foundation
import Security

let service = "local.vpn.autoconnect"

func status(_ code: OSStatus) -> Never {
    fputs("OSStatus: \(code)\n", stderr)
    exit(1)
}

func parsePassword(_ data: Data) -> Data? {
    var bytes = [UInt8](data)
    if bytes.last == 10 {
        bytes.removeLast()
        if bytes.last == 13 { bytes.removeLast() }
    }
    guard !bytes.isEmpty, bytes.count <= 4096,
          !bytes.contains(10), !bytes.contains(13),
          String(bytes: bytes, encoding: .utf8) != nil else { return nil }
    return Data(bytes)
}

func profileAccount(_ value: String) -> Bool {
    guard value.hasPrefix("profile-") else { return false }
    let parts = value.dropFirst("profile-".count).split(separator: "-", omittingEmptySubsequences: false)
    let lengths = [8, 4, 4, 4, 12]
    return parts.count == lengths.count && zip(parts, lengths).allSatisfy { part, length in
        part.count == length && part.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

func command(_ args: [String]) -> (operation: String, account: String)? {
    guard (1...2).contains(args.count), ["--store", "--read", "--delete"].contains(args[0]) else { return nil }
    if args.count == 1 { return (args[0], "primary") }
    guard profileAccount(args[1]) else { return nil }
    return (args[0], args[1])
}

func selfTest() {
    assert(parsePassword(Data("secret\n".utf8)) == Data("secret".utf8))
    assert(parsePassword(Data("secret\r\n".utf8)) == Data("secret".utf8))
    assert(parsePassword(Data()) == nil)
    assert(parsePassword(Data("a\nb".utf8)) == nil)
    assert(parsePassword(Data("a\rb".utf8)) == nil)
    assert(parsePassword(Data(repeating: 65, count: 4097)) == nil)
    assert(parsePassword(Data([0xff])) == nil)
    assert(command(["--store"])?.account == "primary")
    assert(command(["--read", "profile-01234567-89ab-cdef-0123-456789abcdef"])?.account == "profile-01234567-89ab-cdef-0123-456789abcdef")
    assert(command(["--delete", "profile-ffffffff-ffff-ffff-ffff-ffffffffffff"]) != nil)
    assert(command(["--read", "primary"]) == nil)
    assert(command(["--read", "anything"]) == nil)
    assert(command(["--read", "profile-01234567-89AB-cdef-0123-456789abcdef"]) == nil)
    assert(command(["--read", "profile-01234567-89ab-cdef-0123-456789abcde"]) == nil)
    assert(command(["--read", "profile-01234567-89ab-cdef-0123-456789abcdef\n"]) == nil)
    assert(command(["--bad"]) == nil && command(["--read", "a", "b"]) == nil)
    print("self-test: passed")
}

func rootOnly() {
    guard geteuid() == 0 else { fputs("root required\n", stderr); exit(1) }
}

func openKeychain() -> SecKeychain {
    var keychain: SecKeychain?
    let result = SecKeychainOpen("/Library/Keychains/System.keychain", &keychain)
    guard result == errSecSuccess, let keychain else { status(result) }
    return keychain
}

func itemAttributes(_ account: String) -> [String: Any] {
    [kSecClass as String: kSecClassGenericPassword,
     kSecAttrService as String: service,
     kSecAttrAccount as String: account]
}

func accessForThisTool() -> SecAccess {
    guard let executable = Bundle.main.executableURL?.path else { status(errSecParam) }
    var trusted: SecTrustedApplication?
    var result = SecTrustedApplicationCreateFromPath(executable, &trusted)
    guard result == errSecSuccess, let trusted else { status(result) }
    var access: SecAccess?
    result = SecAccessCreate("VPN autoconnect credential" as CFString, [trusted] as CFArray, &access)
    guard result == errSecSuccess, let access else { status(result) }
    return access
}

let args = Array(CommandLine.arguments.dropFirst())
if args == ["--self-test"] { selfTest(); exit(0) }
guard let parsed = command(args) else {
    fputs("usage: vpn-profile-keychain --store|--read|--delete [profile-UUID]|--self-test\n", stderr); exit(2)
}
rootOnly()
let keychain = openKeychain()
let item = itemAttributes(parsed.account)
var search = item
search[kSecMatchSearchList as String] = [keychain]

switch parsed.operation {
case "--store":
    var input = Data()
    while true {
        let chunk: Data
        do {
            guard let read = try FileHandle.standardInput.read(upToCount: 4099) else { break }
            chunk = read
        } catch {
            fputs("input error\n", stderr); exit(1)
        }
        if chunk.isEmpty { break }
        input.append(chunk)
        if input.count > 4098 { fputs("invalid input\n", stderr); exit(1) }
    }
    guard let password = parsePassword(input) else { fputs("invalid input\n", stderr); exit(1) }
    let access = accessForThisTool()
    let updateAttributes: [String: Any] = [kSecValueData as String: password, kSecAttrAccess as String: access]
    let update = SecItemUpdate(search as CFDictionary, updateAttributes as CFDictionary)
    if update == errSecItemNotFound {
        var add = item
        add[kSecValueData as String] = password
        add[kSecAttrAccess as String] = access
        add[kSecUseKeychain as String] = keychain
        let result = SecItemAdd(add as CFDictionary, nil)
        guard result == errSecSuccess else { status(result) }
    } else if update != errSecSuccess {
        status(update)
    }
case "--read":
    var match = search
    match[kSecReturnData as String] = true
    var result: CFTypeRef?
    let code = SecItemCopyMatching(match as CFDictionary, &result)
    guard code == errSecSuccess, let data = result as? Data else { status(code) }
    FileHandle.standardOutput.write(data)
case "--delete":
    let code = SecItemDelete(search as CFDictionary)
    guard code == errSecSuccess else { status(code) }
default:
    exit(2)
}
