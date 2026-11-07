// Native local accessibility transport. JSON payloads are private, never logs.
import AppKit
import ApplicationServices
import Foundation
var diagnostics = ["windows": 0, "webareas": 0, "matches": 0]
var selectedWindow: AXUIElement?

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}
func string(_ element: AXUIElement, _ name: String) -> String {
    guard let value = attribute(element, name) else { return "" }
    if let text = value as? String { return text }
    if let url = value as? URL { return url.absoluteString }
    return ""
}
func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, "AXChildren") as? [AXUIElement] ?? []
}
func findWeb(_ root: AXUIElement, _ expected: String, _ depth: Int = 0) -> AXUIElement? {
    if depth > 12 { return nil }
    if string(root, "AXRole") == "AXWebArea" {
        diagnostics["webareas", default: 0] += 1
        let matches = string(root, "AXURL").trimmingCharacters(in: CharacterSet(charactersIn: "/")) == expected.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if matches { diagnostics["matches", default: 0] += 1 }
        return matches ? root : nil
    }
    for child in children(root) { if let found = findWeb(child, expected, depth + 1) { return found } }
    return nil
}
func selectedWeb(_ expected: String) -> AXUIElement? {
    for application in NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome") {
        let app = AXUIElementCreateApplication(application.processIdentifier)
        for window in attribute(app, "AXWindows") as? [AXUIElement] ?? [] {
            diagnostics["windows", default: 0] += 1
            if let web = findWeb(window, expected) { selectedWindow = window; return web }
        }
    }
    return nil
}
func snapshot(_ element: AXUIElement, _ path: [Int], _ output: inout [[String: Any]], _ depth: Int = 0) {
    if depth > 35 || output.count >= 15000 { return }
    let role = string(element, "AXRole")
    let secure = role == "AXSecureTextField" || string(element, "AXSubrole") == "AXSecureTextField"
    let row: [String: Any] = ["path": path, "role": role, "name": string(element, "AXTitle"),
        "value": secure ? "" : string(element, "AXValue"), "url": string(element, "AXURL"),
        "description": string(element, "AXDescription"), "placeholder": string(element, "AXPlaceholderValue")]
    output.append(row)
    for (index, child) in children(element).enumerated() { snapshot(child, path + [index], &output, depth + 1) }
}
func resolve(_ root: AXUIElement, _ path: String) -> AXUIElement? {
    var current = root
    for token in path.split(separator: ".") {
        guard let index = Int(token), index >= 0 else { return nil }
        let list = children(current)
        guard index < list.count else { return nil }
        current = list[index]
    }
    return current
}
func focusWindow(_ element: AXUIElement, _ expectedURL: String) -> Bool {
    var pid: pid_t = 0
    guard AXUIElementGetPid(element, &pid) == .success,
          let application = NSRunningApplication(processIdentifier: pid), application.bundleIdentifier == "com.google.Chrome",
          let window = selectedWindow, AXUIElementPerformAction(window, "AXRaise" as CFString) == .success else { return false }
    application.activate(options: [.activateIgnoringOtherApps])
    for _ in 0..<20 {
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid { break }
        Thread.sleep(forTimeInterval: 0.05)
    }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { return false }
    guard let focused = attribute(AXUIElementCreateApplication(pid), "AXFocusedWindow"), CFEqual(focused, window) else { return false }
    guard findWeb(window, expectedURL) != nil else { return false }
    return true
}
func press(_ element: AXUIElement, _ expectedURL: String) -> Bool {
    if AXUIElementPerformAction(element, "AXPress" as CFString) == .success { return true }
    guard focusWindow(element, expectedURL) else { return false }
    guard let p = attribute(element, "AXPosition"), let s = attribute(element, "AXSize") else { return false }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point), AXValueGetValue(s as! AXValue, .cgSize, &size), size.width > 0, size.height > 0 else { return false }
    let location = CGPoint(x: point.x + size.width / 2, y: point.y + size.height / 2)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: location, mouseButton: .left)?.post(tap: .cghidEventTap)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: location, mouseButton: .left)?.post(tap: .cghidEventTap)
    return true
}
func main() -> Int32 {
    let args = CommandLine.arguments
    guard args.count >= 3, AXIsProcessTrusted(), let url = URL(string: args[2]), url.scheme == "https", url.host == "kad.arbitr.ru",
          url.path == "/" || url.path.lowercased().hasPrefix("/card/") else { return 1 }
    let selected = selectedWeb(args[2])
    if args[1] == "probe" {
        FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: diagnostics))
        return 0
    }
    guard let web = selected else { return 2 }
    if args[1] == "snapshot" {
        var rows: [[String: Any]] = []
        snapshot(web, [], &rows)
        guard rows.count < 15000, let data = try? JSONSerialization.data(withJSONObject: rows) else { return 3 }
        FileHandle.standardOutput.write(data)
        return 0
    }
    guard args.count >= 4, let target = resolve(web, args[3]) else { return 4 }
    if args[1] == "set", args.count == 5, ["AXTextField", "AXTextArea"].contains(string(target, "AXRole")) {
        guard string(target, "AXPlaceholderValue") == "дд.мм.гггг", args[4].range(of: "^\\d{2}\\.\\d{2}\\.\\d{4}$", options: .regularExpression) != nil else { return 5 }
        return AXUIElementSetAttributeValue(target, "AXValue" as CFString, args[4] as CFString) == .success ? 0 : 5
    }
    if args[1] == "press", args.count == 5, ["AXButton", "AXLink", "AXStaticText", "AXGroup"].contains(string(target, "AXRole")) {
        guard ["AXTitle", "AXValue", "AXDescription"].contains(where: { string(target, $0).trimmingCharacters(in: .whitespacesAndNewlines) == args[4] }) else { return 6 }
        return press(target, args[2]) ? 0 : 6
    }
    return 7
}
exit(main())
