#!/usr/bin/env swift
import AppKit

// Rebuild the Chrome/Edge extension icons and Chrome Web Store promo tile.
// Requires macOS and the built-in AppKit framework; no fonts or assets are bundled.

let output = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".")
let iconDirectory = output.appendingPathComponent("../icons", isDirectory: true)
try FileManager.default.createDirectory(at: iconDirectory, withIntermediateDirectories: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

let navy = NSColor(calibratedRed: 0.055, green: 0.102, blue: 0.176, alpha: 1)
let navyLight = NSColor(calibratedRed: 0.086, green: 0.157, blue: 0.255, alpha: 1)
let blue = NSColor(calibratedRed: 0.31, green: 0.72, blue: 0.94, alpha: 1)
let mint = NSColor(calibratedRed: 0.42, green: 0.88, blue: 0.76, alpha: 1)
let white = NSColor(calibratedWhite: 0.96, alpha: 1)
let pale = NSColor(calibratedRed: 0.72, green: 0.81, blue: 0.91, alpha: 1)

func bitmap(width: Int, height: Int, draw: (NSRect) -> Void) throws -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                               isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let context = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = context
    context.imageInterpolation = .high
    let bounds = NSRect(x: 0, y: 0, width: width, height: height)
    draw(bounds)
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    return rep
}

func save(_ rep: NSBitmapImageRep, to url: URL) throws {
    try rep.representation(using: .png, properties: [:])!.write(to: url, options: .atomic)
}

func drawMark(in bounds: NSRect) {
    let side = min(bounds.width, bounds.height)
    let unit = side / 128
    let inset = side * 0.125
    let tile = NSRect(x: bounds.midX - side / 2 + inset,
                      y: bounds.midY - side / 2 + inset,
                      width: side - inset * 2, height: side - inset * 2)
    let tilePath = NSBezierPath(roundedRect: tile, xRadius: side * 0.19, yRadius: side * 0.19)
    let gradient = NSGradient(starting: navyLight, ending: navy)!
    gradient.draw(in: tilePath, angle: -45)

    // Abstract K mark: clear at toolbar sizes, with no court-system logo or implied affiliation.
    let stroke = max(2.2 * unit, side * 0.095)
    let x = bounds.midX - side * 0.19
    let y0 = bounds.midY - side * 0.25
    let y1 = bounds.midY + side * 0.25
    let spine = NSBezierPath()
    spine.lineCapStyle = .round
    spine.lineJoinStyle = .round
    spine.lineWidth = stroke
    spine.move(to: NSPoint(x: x, y: y0))
    spine.line(to: NSPoint(x: x, y: y1))
    white.setStroke()
    spine.stroke()

    let arms = NSBezierPath()
    arms.lineCapStyle = .round
    arms.lineJoinStyle = .round
    arms.lineWidth = stroke
    arms.move(to: NSPoint(x: x + side * 0.015, y: bounds.midY - side * 0.015))
    arms.line(to: NSPoint(x: bounds.midX + side * 0.20, y: y1))
    arms.move(to: NSPoint(x: x + side * 0.015, y: bounds.midY + side * 0.015))
    arms.line(to: NSPoint(x: bounds.midX + side * 0.20, y: y0))
    blue.setStroke()
    arms.stroke()

    let dot = NSRect(x: bounds.midX + side * 0.205, y: bounds.midY - side * 0.035,
                     width: side * 0.075, height: side * 0.075)
    mint.setFill()
    NSBezierPath(ovalIn: dot).fill()
}

for size in [16, 32, 48, 128] {
    let image = try bitmap(width: size, height: size) { drawMark(in: $0) }
    try save(image, to: iconDirectory.appendingPathComponent("icon-\(size).png"))
}

let promo = try bitmap(width: 440, height: 280) { bounds in
    navy.setFill()
    bounds.fill()

    let glow = NSBezierPath(ovalIn: NSRect(x: 286, y: 95, width: 230, height: 230))
    NSColor(calibratedRed: 0.12, green: 0.31, blue: 0.43, alpha: 0.5).setFill()
    glow.fill()

    let brand = NSRect(x: 35, y: 77, width: 126, height: 126)
    drawMark(in: brand)

    // A visual of a checked document supports the product's explicit human selection step.
    let page = NSRect(x: 299, y: 67, width: 87, height: 129)
    let pagePath = NSBezierPath(roundedRect: page, xRadius: 11, yRadius: 11)
    white.setFill()
    pagePath.fill()
    let fold = NSBezierPath()
    fold.lineWidth = 3
    pale.setStroke()
    for row in 0..<3 {
        let y = CGFloat(157 - row * 25)
        fold.move(to: NSPoint(x: 319, y: y))
        fold.line(to: NSPoint(x: 365, y: y))
    }
    fold.stroke()
    let check = NSBezierPath()
    check.lineWidth = 4
    check.lineCapStyle = .round
    check.lineJoinStyle = .round
    check.move(to: NSPoint(x: 319, y: 94))
    check.line(to: NSPoint(x: 331, y: 82))
    check.line(to: NSPoint(x: 351, y: 105))
    mint.setStroke()
    check.stroke()

    let title = NSAttributedString(string: "СРО Робот", attributes: [
        .font: NSFont.systemFont(ofSize: 31, weight: .bold), .foregroundColor: white,
    ])
    title.draw(at: NSPoint(x: 36, y: 218))
    let subtitle = NSAttributedString(string: "Помощник КАД", attributes: [
        .font: NSFont.systemFont(ofSize: 22, weight: .semibold), .foregroundColor: blue,
    ])
    subtitle.draw(at: NSPoint(x: 36, y: 185))
    let caption = NSAttributedString(string: "Сотрудник выбирает документ", attributes: [
        .font: NSFont.systemFont(ofSize: 15, weight: .medium), .foregroundColor: pale,
    ])
    caption.draw(at: NSPoint(x: 36, y: 41))
}
try save(promo, to: output.appendingPathComponent("chrome-small-promo-440x280.png"))

print("Wrote four extension icons and the 440x280 store promo to \(output.path)")
