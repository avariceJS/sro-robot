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
func focusedWeb(_ expected: String) -> AXUIElement? {
    guard let application = NSWorkspace.shared.frontmostApplication,
          application.bundleIdentifier == "com.google.Chrome" else { return nil }
    let app = AXUIElementCreateApplication(application.processIdentifier)
    guard let value = attribute(app, "AXFocusedWindow"), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    let window = value as! AXUIElement
    diagnostics["windows", default: 0] += 1
    guard let web = findWeb(window, expected) else { return nil }
    selectedWindow = window
    return web
}
func focusOwnedWindow(probeOnly: Bool = false, inputData: Data? = nil, readinessPolls: Int = 15, readinessDeadline: TimeInterval? = nil) -> Int32 {
    let data = inputData ?? FileHandle.standardInput.readDataToEndOfFile()
    guard data.count <= 16384,
          let input = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let title = input["title"] as? String, let bounds = input["bounds"] as? [Double],
          bounds.count == 4, bounds.allSatisfy({ $0.isFinite }), bounds[2] > bounds[0], bounds[3] > bounds[1] else { return 8 }
    let deadline = readinessDeadline ?? ProcessInfo.processInfo.systemUptime + 1.5
    var expectedPID: pid_t?
    if let supplied = input["native_pid"] {
        guard let value = supplied as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
              value.intValue > 0, value.doubleValue == Double(value.int32Value) else { return 8 }
        expectedPID = value.int32Value
    }
    var matches: [(NSRunningApplication, AXUIElement, AXUIElement)] = []
    let applications = NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.Chrome")
    var windowCounts: [Int] = [], beforeCounts: [Int] = [], windowStatuses: [Int] = [], roleStatuses: [Int] = []
    var titleMatches = 0, boundsMatches = 0, brandMatches = 0
    for application in applications {
        // A retained lease pins the process before any AX raise/activation.
        if let pid = expectedPID, application.processIdentifier != pid { continue }
        let app = AXUIElementCreateApplication(application.processIdentifier)
        var before: CFTypeRef?
        let windowStatus = AXUIElementCopyAttributeValue(app, "AXWindows" as CFString, &before)
        beforeCounts.append((before as? [AXUIElement] ?? []).count)
        windowStatuses.append(Int(windowStatus.rawValue))
        // Chromium lazily enables its native AX mode when an assistive client
        // reads the application role. This is a runtime metadata request, not a
        // browser/profile/security setting or a scan of PDF accessibility text.
        // chromium/src/chrome/browser/chrome_browser_application_mac.mm
        var applicationRole: CFTypeRef?
        let roleStatus = AXUIElementCopyAttributeValue(app, "AXRole" as CFString, &applicationRole)
        roleStatuses.append(Int(roleStatus.rawValue))
        let windows = attribute(app, "AXWindows") as? [AXUIElement] ?? []
        windowCounts.append(windows.count)
        for window in windows {
            let actualTitle = string(window, "AXTitle")
            let branded = [" - Google Chrome", " – Google Chrome", " — Google Chrome"].contains { actualTitle == title + $0 }
            if branded { brandMatches += 1 }
            // AX adds a Chrome brand suffix that SDEF/CGWindow titles omit.
            // Accept only those exact representations, never substring identity.
            let titleMatchesWindow = actualTitle == title || branded
            if titleMatchesWindow { titleMatches += 1 }
            guard let position = attribute(window, "AXPosition"), let dimensions = attribute(window, "AXSize"),
                  CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(dimensions) == AXValueGetTypeID() else { continue }
            var point = CGPoint.zero, size = CGSize.zero
            guard AXValueGetValue(position as! AXValue, .cgPoint, &point), AXValueGetValue(dimensions as! AXValue, .cgSize, &size) else { continue }
            let actual = [Double(point.x), Double(point.y), Double(point.x + size.width), Double(point.y + size.height)]
            if zip(actual, bounds).allSatisfy({ abs($0.0 - $0.1) <= 2 }) {
                boundsMatches += 1
                if titleMatchesWindow { matches.append((application, app, window)) }
            }
        }
    }
    if probeOnly {
        let displayIDs = NSScreen.screens.compactMap { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value }
        let displays = displayIDs.map { CGDisplayBounds($0) }.filter { $0.width > 0 && $0.height > 0 }
        let sourceRect = CGRect(x: bounds[0], y: bounds[1], width: bounds[2]-bounds[0], height: bounds[3]-bounds[1])
        let intersectsDisplay: Any = displays.isEmpty ? NSNull() : displays.contains { $0.intersects(sourceRect) }
        let windowInfo = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        var cgChromeCounts = [Int](repeating: 0, count: applications.count)
        var cgTitleCounts = [Int](repeating: 0, count: applications.count)
        var cgBoundsCounts = [Int](repeating: 0, count: applications.count)
        var cgTitleWindows: [[String: Any]] = []
        for info in windowInfo {
            guard let owner = info[kCGWindowOwnerPID as String] as? Int,
                  let index = applications.firstIndex(where: { Int($0.processIdentifier) == owner }) else { continue }
            cgChromeCounts[index] += 1
            if info[kCGWindowName as String] as? String == title {
                cgTitleCounts[index] += 1
                let geometry = info[kCGWindowBounds as String] as? [String: Double] ?? [:]
                let actual = [geometry["X"] ?? .nan, geometry["Y"] ?? .nan,
                    (geometry["X"] ?? .nan) + (geometry["Width"] ?? .nan), (geometry["Y"] ?? .nan) + (geometry["Height"] ?? .nan)]
                cgTitleWindows.append(["app_index": index, "onscreen": info[kCGWindowIsOnscreen as String] as? Bool ?? false,
                    "layer_zero": (info[kCGWindowLayer as String] as? Int) == 0,
                    "bounds_match": zip(actual, bounds).allSatisfy({ abs($0.0 - $0.1) <= 2 })])
            }
            if let geometry = info[kCGWindowBounds as String] as? [String: Double],
               let x = geometry["X"], let y = geometry["Y"], let width = geometry["Width"], let height = geometry["Height"],
               zip([x, y, x + width, y + height], bounds).allSatisfy({ abs($0.0 - $0.1) <= 2 }) { cgBoundsCounts[index] += 1 }
        }
        let result: [String: Any] = ["chrome_apps": applications.count, "ax_window_counts": windowCounts,
            "source_intersects_display": intersectsDisplay, "display_count": displays.count, "cg_title_windows": cgTitleWindows, "cg_window_counts": cgChromeCounts, "cg_title_counts": cgTitleCounts, "cg_bounds_counts": cgBoundsCounts,
            "ax_window_counts_before_role": beforeCounts, "ax_window_statuses": windowStatuses, "ax_role_statuses": roleStatuses,
            "brand_matches": brandMatches, "title_matches": titleMatches, "bounds_matches": boundsMatches, "candidate_matches": matches.count]
        FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: result))
        return 0
    }
    guard matches.count == 1 else {
        // Window-menu reveal and PDF viewer titles reach native AX lazily.
        // Poll metadata only; never repeat a menu press or relax identity.
        if matches.isEmpty, readinessPolls > 0, ProcessInfo.processInfo.systemUptime < deadline {
            Thread.sleep(forTimeInterval: 0.1)
            return focusOwnedWindow(inputData: data, readinessPolls: readinessPolls - 1, readinessDeadline: deadline)
        }
        return 9
    }
    let (application, app, window) = matches[0]
    application.activate(options: [])
    guard AXUIElementPerformAction(window, "AXRaise" as CFString) == .success else { return 10 }
    for name in ["AXMain", "AXFocused"] {
        var settable = DarwinBoolean(false)
        if AXUIElementIsAttributeSettable(window, name as CFString, &settable) == .success && settable.boolValue {
            _ = AXUIElementSetAttributeValue(window, name as CFString, kCFBooleanTrue)
        }
    }
    for _ in 0..<20 {
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == application.processIdentifier,
           let focused = attribute(app, "AXFocusedWindow"), CFEqual(focused, window) {
            FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: ["native_pid": Int(application.processIdentifier)]))
            return 0
        }
        Thread.sleep(forTimeInterval: 0.05)
    }
    return 11
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
    let focused = CommandLine.arguments.contains("--focused")
    let probeOnly = CommandLine.arguments.contains("--probe-only")
    let args = CommandLine.arguments.filter { $0 != "--focused" && $0 != "--probe-only" }
    guard args.count >= 3, AXIsProcessTrusted(), let url = URL(string: args[2]), url.scheme == "https", url.host == "kad.arbitr.ru" else { return 1 }
    if args[1] == "focus-owned", args.count == 3 { return focusOwnedWindow(probeOnly: probeOnly) }
    guard
          url.path == "/" || url.path.lowercased().hasPrefix("/card/") else { return 1 }
    let selected = focused ? focusedWeb(args[2]) : selectedWeb(args[2])
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
