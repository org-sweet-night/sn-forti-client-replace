import Foundation
import SystemConfiguration

let subtype = "com.fortinet.forticlient.macos.vpn"

func fail(_ message: String) -> Never {
    fputs("\(message): \(SCError()) (\(String(cString: SCErrorString(SCError()))) )\n", stderr)
    exit(1)
}

func services(_ prefs: SCPreferences) -> [(String, [String: Any])] {
    guard let all = SCPreferencesPathGetValue(prefs, "/NetworkServices" as CFString) as? [String: Any] else { return [] }
    return all.compactMap { key, value in
        guard let service = value as? [String: Any],
              let interface = service["Interface"] as? [String: Any],
              interface["SubType"] as? String == subtype else { return nil }
        return (key, service)
    }
}

func changed(_ service: [String: Any], to value: Bool) -> [String: Any]? {
    guard var vpn = service["VPN"] as? [String: Any] else { return nil }
    vpn["DisconnectOnSleep"] = value ? 1 : 0
    var result = service
    result["VPN"] = vpn
    return result
}

func exactlyOne<T>(_ values: [T]) -> T? { values.count == 1 ? values[0] : nil }

func read(_ prefs: SCPreferences) -> [(String, Bool?)] {
    services(prefs).map { key, service in
        (key, (service["VPN"] as? [String: Any])?["DisconnectOnSleep"] as? Bool)
    }
}

func runSelfTest() {
    let input: [String: Any] = ["Interface": ["SubType": subtype], "VPN": ["DisconnectOnSleep": true, "OnDemandEnabled": false, "RemoteAddress": "must-not-print"]]
    guard let output = changed(input, to: false),
          ((output["VPN"] as? [String: Any])?["DisconnectOnSleep"] as? NSNumber)?.intValue == 0,
          (output["VPN"] as? [String: Any])?["OnDemandEnabled"] as? Bool == false,
          (output["VPN"] as? [String: Any])?["RemoteAddress"] as? String == "must-not-print",
          Set(output.keys) == Set(input.keys) else { fatalError("field isolation failed") }
    let two = [input, input].filter { ($0["Interface"] as? [String: Any])?["SubType"] as? String == subtype }
    assert(exactlyOne([input]) != nil && exactlyOne([[String: Any]]()) == nil && exactlyOne(two) == nil)
    print("self-test: passed")
}

let args = Array(CommandLine.arguments.dropFirst())
if args == ["--self-test"] { runSelfTest(); exit(0) }
guard args.count == 1, ["--read", "--apply", "--restore"].contains(args[0]) else {
    fputs("usage: vpn-sleep-setting --read|--apply|--restore|--self-test\n", stderr); exit(2)
}
guard let prefs = SCPreferencesCreate(nil, "vpn-sleep-setting" as CFString, nil) else { fail("SCPreferencesCreate failed") }
let mode = args[0]
if mode == "--read" {
    let found = read(prefs)
    print("matches: \(found.count)")
    for (_, flag) in found { print("DisconnectOnSleep: \(flag.map(String.init) ?? "unset")") }
    exit(0)
}
guard geteuid() == 0 else { fputs("write operation requires root\n", stderr); exit(1) }
guard SCPreferencesLock(prefs, true) else { fail("SCPreferencesLock failed") }
defer { SCPreferencesUnlock(prefs) }
let matches = services(prefs)
guard let (key, service) = exactlyOne(matches) else { fail("expected exactly one matching service; found \(matches.count)") }
let before = (service["VPN"] as? [String: Any])?["DisconnectOnSleep"] as? Bool
let target = mode == "--apply" ? false : true
guard let updated = changed(service, to: target) else { fail("matching service has no VPN dictionary") }
let path = "/NetworkServices/\(key)"
guard SCPreferencesPathSetValue(prefs, "\(path)/VPN" as CFString, (updated["VPN"] as! [String: Any]) as CFDictionary) else { fail("SCPreferencesPathSetValue failed") }
guard SCPreferencesCommitChanges(prefs) else { fail("SCPreferencesCommitChanges failed") }
guard SCPreferencesApplyChanges(prefs) else { fail("SCPreferencesApplyChanges failed") }
guard let (_, afterService) = exactlyOne(services(prefs)),
      let afterNumber = (afterService["VPN"] as? [String: Any])?["DisconnectOnSleep"] as? NSNumber else { fail("could not read back DisconnectOnSleep") }
let after = afterNumber.intValue
guard after == (target ? 1 : 0) else { fail("DisconnectOnSleep read-back did not match requested value") }
print("DisconnectOnSleep: \(before.map(String.init) ?? "unset") -> \(after)")
