import AppKit

// Menu bar front end for `ai-usage json`. All provider logic lives in the Node CLI; this only renders it.

struct RunError: Error { let message: String }

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let command: String
    private let refreshSeconds: TimeInterval
    private var snapshot: [String: Any]?
    private var lastError: String?
    private var refreshing = false
    private var lastRefresh: Date?
    private var timer: Timer?

    private let mono = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
    private let monoBold = NSFont.monospacedSystemFont(ofSize: 12, weight: .semibold)
    private let titleFont = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)

    override init() {
        let info = Bundle.main.infoDictionary ?? [:]
        command = (info["AIUsageCommand"] as? String) ?? NSHomeDirectory() + "/.local/bin/ai-usage"
        let configured = UserDefaults.standard.double(forKey: "refreshSeconds")
        refreshSeconds = configured >= 60 ? configured : 300
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem.button?.attributedTitle = NSAttributedString(string: "AI …", attributes: [.font: titleFont])
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
        rebuildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: refreshSeconds, repeats: true) { [weak self] _ in self?.refresh() }
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(didWake), name: NSWorkspace.didWakeNotification, object: nil)
    }

    @objc private func didWake() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) {
        rebuildMenu()
        if let last = lastRefresh, Date().timeIntervalSince(last) < 60 { return }
        refresh(maxAge: 60)
    }

    // MARK: - Data

    private func refresh(maxAge: Int? = nil, force: Bool = false) {
        guard !refreshing else { return }
        refreshing = true
        rebuildMenu()
        var args = ["json"]
        if force { args.append("--refresh") } else { args += ["--max-age", String(maxAge ?? Int(refreshSeconds) - 10)] }
        let command = self.command
        DispatchQueue.global(qos: .utility).async {
            let result = AppDelegate.run(command, args)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.refreshing = false
                self.lastRefresh = Date()
                switch result {
                case .success(let data):
                    if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        self.snapshot = json
                        self.lastError = nil
                    } else {
                        self.lastError = "could not parse ai-usage output"
                    }
                case .failure(let err):
                    self.lastError = err.message
                }
                self.updateTitle()
                self.rebuildMenu()
            }
        }
    }

    private static func run(_ command: String, _ args: [String]) -> Result<Data, RunError> {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command)
        process.arguments = args
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        env["PATH"] = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", env["PATH"] ?? ""].joined(separator: ":")
        env["NO_COLOR"] = "1"
        process.environment = env
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do { try process.run() } catch { return .failure(RunError(message: "cannot run \(command): \(error.localizedDescription)")) }
        // ai-usage bounds its own provider calls well under this; a run still going means it is stuck,
        // e.g. on a macOS permission prompt. Stop it so the menu shows an error instead of "AI …" forever.
        let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 180, execute: watchdog)
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog.cancel()
        if process.terminationReason == .uncaughtSignal {
            return .failure(RunError(message: "ai-usage did not finish within 3 minutes; if macOS is showing a permission prompt for it, answer it, or re-run install.sh"))
        }
        if process.terminationStatus != 0 {
            let message = String(data: errData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return .failure(RunError(message: message.isEmpty ? "ai-usage exited \(process.terminationStatus)" : message))
        }
        return .success(data)
    }

    private var accounts: [[String: Any]] { snapshot?["accounts"] as? [[String: Any]] ?? [] }
    private var lanes: [[String: Any]] { snapshot?["lanes"] as? [[String: Any]] ?? [] }

    // MARK: - Rendering

    private func color(for level: String?) -> NSColor {
        switch level {
        case "ok": return .systemGreen
        case "mid": return .systemOrange
        case "low", "out", "error": return .systemRed
        default: return .secondaryLabelColor
        }
    }

    private func level(for pct: Double) -> String {
        pct <= 0.5 ? "out" : pct < 20 ? "low" : pct < 50 ? "mid" : "ok"
    }

    // MARK: - Menu bar sections

    /// Text-style skull (U+2620 with the text variation selector) so it takes a colour.
    static let skull = "\u{2620}\u{FE0E}"

    /// `value` is empty for entries in the ☠ group, which lists names only.
    private struct Entry { let label: String; let tag: String?; let value: String; let level: String; let stale: Bool }
    private struct Section { let header: String; let skull: Bool; let entries: [Entry] }
    private typealias BarModel = (sections: [Section], unavailable: [String])

    private let barHeaderFont = NSFont.systemFont(ofSize: 9, weight: .heavy)
    private let barLabelFont = NSFont.systemFont(ofSize: 10.5, weight: .medium)
    private let barTagFont = NSFont.systemFont(ofSize: 8, weight: .bold)
    private let barNumberFont = NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .semibold)
    // The skull glyph is drawn small by the symbol font; a larger size makes it read at a glance.
    private let barSkullFont = NSFont.systemFont(ofSize: 14, weight: .regular)

    /// `ai-usage json`: one section per limit window length ("5h", "wk", …), then a ☠ section for pools
    /// whose weekly (or longer) limit is used up, then accounts with no data.
    private func barModel() -> BarModel {
        func entry(_ raw: [String: Any], withValue: Bool) -> Entry {
            let pct = raw["pct"] as? Double ?? 0
            return Entry(label: raw["label"] as? String ?? "?", tag: raw["tag"] as? String,
                         value: withValue ? String(Int(pct.rounded(.down))) : "",
                         level: raw["level"] as? String ?? level(for: pct), stale: (raw["stale"] as? Bool) == true)
        }
        var sections = (snapshot?["sections"] as? [[String: Any]] ?? []).map { section in
            Section(header: (section["label"] as? String ?? "?").uppercased(), skull: false,
                    entries: (section["entries"] as? [[String: Any]] ?? []).map { entry($0, withValue: true) })
        }
        let exhausted = (snapshot?["exhausted"] as? [[String: Any]] ?? []).map { entry($0, withValue: false) }
        if !exhausted.isEmpty { sections.append(Section(header: AppDelegate.skull, skull: true, entries: exhausted)) }
        let unavailable = (snapshot?["unavailable"] as? [[String: Any]] ?? []).map { $0["label"] as? String ?? "?" }
        return (sections, unavailable)
    }

    private func width(_ text: String, _ font: NSFont) -> CGFloat {
        ceil(NSAttributedString(string: text, attributes: [.font: font]).size().width)
    }

    /// One rounded box per section: "5H", "WK", … with each account's % left, then "☠" with the names of
    /// everything used up for the week.
    private func barImage(_ model: BarModel) -> NSImage {
        let height: CGFloat = 22, boxHeight: CGFloat = 18, inset: CGFloat = 5, headerGap: CGFloat = 5
        let entryGap: CGFloat = 6, labelGap: CGFloat = 2, sectionGap: CGFloat = 4
        func headerFont(_ section: Section) -> NSFont { section.skull ? barSkullFont : barHeaderFont }
        func entryWidth(_ entry: Entry) -> CGFloat {
            width(entry.label, barLabelFont) + (entry.tag.map { width($0, barTagFont) + 1 } ?? 0)
                + (entry.value.isEmpty ? 0 : labelGap + width(entry.value, barNumberFont))
                + (entry.stale ? width("!", barNumberFont) : 0)
        }
        func sectionWidth(_ section: Section) -> CGFloat {
            2 * inset + width(section.header, headerFont(section)) + headerGap
                + section.entries.map(entryWidth).reduce(0, +) + CGFloat(max(0, section.entries.count - 1)) * entryGap
        }
        let unavailableText = model.unavailable.map { "\($0) ?" }.joined(separator: "  ")
        var total = model.sections.map(sectionWidth).reduce(0, +) + CGFloat(max(0, model.sections.count - 1)) * sectionGap
        if !unavailableText.isEmpty { total += (total > 0 ? sectionGap : 0) + width(unavailableText, barLabelFont) }

        let image = NSImage(size: NSSize(width: max(total, 1), height: height), flipped: false) { [self] _ in
            @discardableResult
            func draw(_ string: String, _ font: NSFont, _ color: NSColor, at x: CGFloat, raise: CGFloat = 0) -> CGFloat {
                let text = NSAttributedString(string: string, attributes: [.font: font, .foregroundColor: color])
                let size = text.size()
                text.draw(at: NSPoint(x: x, y: (height - size.height) / 2 + raise))
                return ceil(size.width)
            }
            // Not secondaryLabelColor: inside an image it gets no vibrancy and vanishes over the translucent bar.
            let muted = NSColor.labelColor.withAlphaComponent(0.75)
            var x: CGFloat = 0
            for (i, section) in model.sections.enumerated() {
                if i > 0 { x += sectionGap }
                let boxWidth = sectionWidth(section)
                let box = NSRect(x: x, y: (height - boxHeight) / 2, width: boxWidth, height: boxHeight)
                NSColor.labelColor.withAlphaComponent(0.13).setFill()
                NSBezierPath(roundedRect: box, xRadius: 5, yRadius: 5).fill()
                var cx = x + inset
                cx += draw(section.header, headerFont(section), section.skull ? levelText(color(for: "out")) : muted, at: cx) + headerGap
                for (j, entry) in section.entries.enumerated() {
                    if j > 0 { cx += entryGap }
                    cx += draw(entry.label, barLabelFont, .labelColor, at: cx)
                    if let tag = entry.tag { cx += 1 + draw(tag, barTagFont, muted, at: cx + 1, raise: -2) }
                    if !entry.value.isEmpty {
                        cx += labelGap
                        cx += draw(entry.value, barNumberFont, levelText(color(for: entry.level)), at: cx)
                    }
                    if entry.stale { cx += draw("!", barNumberFont, .systemRed, at: cx) }
                }
                x += boxWidth
            }
            if !unavailableText.isEmpty {
                if x > 0 { x += sectionGap }
                draw(unavailableText, barLabelFont, .systemRed, at: x)
            }
            return true
        }
        image.isTemplate = false
        return image
    }

    /// Level colour for numbers, darkened in light mode where bright green is hard to read.
    private func levelText(_ tint: NSColor) -> NSColor {
        NSColor(name: nil) { appearance in
            let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return dark ? tint : (tint.usingColorSpace(.deviceRGB)?.blended(withFraction: 0.4, of: .black) ?? tint)
        }
    }

    private func barSummary(_ model: BarModel) -> String {
        var parts = model.sections.map { section -> String in
            let entries = section.entries.map { entry -> String in
                let label = entry.tag.map { tag in "\(entry.label)·\(tag)" } ?? entry.label
                return (entry.value.isEmpty ? label : "\(label) \(entry.value)") + (entry.stale ? "!" : "")
            }
            return "\(section.header) " + entries.joined(separator: " · ")
        }
        if !model.unavailable.isEmpty { parts.append(model.unavailable.map { "\($0) ?" }.joined(separator: " · ")) }
        return parts.joined(separator: " | ")
    }

    private func updateTitle() {
        guard let button = statusItem.button else { return }
        if accounts.isEmpty {
            button.image = nil
            button.attributedTitle = NSAttributedString(string: lastError == nil ? "AI …" : "AI ⚠", attributes: [.font: titleFont])
            return
        }
        let model = barModel()
        button.attributedTitle = NSAttributedString(string: "")
        button.image = barImage(model)
        button.imagePosition = .imageOnly
        button.setAccessibilityLabel("AI usage, % left: " + barSummary(model))
        button.toolTip = "% left, grouped by limit window: 5H = 5-hour, WK = weekly.\n☠ = used up for the week (details in the menu).\nAntigravity pools: G = Gemini, C = Claude & GPT-OSS. ! = last refresh failed."
    }

    /// `AIUsageBar --render-title <png> [--light]`: draw the menu bar pills to a PNG for checking.
    func renderTitle(to path: String, light: Bool) {
        if case .success(let data) = AppDelegate.run(command, ["json"]) {
            snapshot = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        }
        let image = barImage(barModel())
        let scale: CGFloat = 2
        let appearance = NSAppearance(named: light ? .aqua : .darkAqua)!
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(image.size.width * scale), pixelsHigh: Int(image.size.height * scale),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return }
        rep.size = image.size
        appearance.performAsCurrentDrawingAppearance {
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            (light ? NSColor(white: 0.93, alpha: 1) : NSColor(white: 0.16, alpha: 1)).setFill()
            NSRect(origin: .zero, size: image.size).fill()
            image.draw(in: NSRect(origin: .zero, size: image.size))
            NSGraphicsContext.restoreGraphicsState()
        }
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
        print("TITLE: " + barSummary(barModel()))
    }

    private func pad(_ s: String, _ n: Int) -> String {
        s.count >= n ? s : s + String(repeating: " ", count: n - s.count)
    }

    private func bar(_ pct: Double, width: Int = 12) -> String {
        let filled = max(0, min(width, Int((pct / 100 * Double(width)).rounded())))
        return String(repeating: "█", count: filled) + String(repeating: "░", count: width - filled)
    }

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private func relative(_ date: Date) -> String {
        let mins = Int(date.timeIntervalSinceNow / 60)
        if mins <= 0 { return "now" }
        if mins < 60 { return "\(mins)m" }
        let hours = mins / 60
        if hours < 48 { return "\(hours)h \(String(format: "%02d", mins % 60))m" }
        return "\(hours / 24)d \(hours % 24)h"
    }

    private func clock(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDateInToday(date) ? "h:mm a" : "EEE h:mm a"
        return f.string(from: date)
    }

    private func infoItem(_ text: NSAttributedString) -> NSMenuItem {
        let item = NSMenuItem(title: text.string, action: #selector(noop), keyEquivalent: "")
        item.attributedTitle = text
        item.target = self
        return item
    }

    private func actionItem(_ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    private func text(_ s: String, _ font: NSFont? = nil, _ color: NSColor = .labelColor) -> NSAttributedString {
        NSAttributedString(string: s, attributes: [.font: font ?? mono, .foregroundColor: color])
    }

    private func rebuildMenu() {
        menu.removeAllItems()
        var status = refreshing ? "Refreshing…" : "Not loaded yet"
        if let generated = snapshot?["generatedAt"] as? String, let date = AppDelegate.iso.date(from: generated), !refreshing {
            status = "Updated \(clock(date))"
        }
        menu.addItem(infoItem(text("AI usage · \(status)", monoBold, .secondaryLabelColor)))
        menu.addItem(infoItem(text("Menu bar: % left by limit window (5H, WK) · ☠ = used up for the week · G = Gemini, C = Claude & GPT-OSS", mono, .tertiaryLabelColor)))
        if let lastError {
            menu.addItem(infoItem(text("⚠ \(lastError.prefix(120))", mono, .systemRed)))
        }

        for account in accounts {
            menu.addItem(.separator())
            let label = account["label"] as? String ?? "?"
            let plan = (account["plan"] as? String).map { "  \($0)" } ?? ""
            let headline = account["headline"] as? [String: Any] ?? [:]
            let heading = NSMutableAttributedString(attributedString: text(label, monoBold, color(for: headline["level"] as? String)))
            heading.append(text(plan, mono, .secondaryLabelColor))
            menu.addItem(infoItem(heading))
            if let error = account["error"] as? String {
                let age = (account["ageMinutes"] as? Int).map { ", showing data from \($0)m ago" } ?? ""
                menu.addItem(infoItem(text("  ⚠ \(error.prefix(90))\(age)", mono, .systemRed)))
            }
            for window in account["windows"] as? [[String: Any]] ?? [] {
                let pct = window["remainingPct"] as? Double ?? 0
                let blockedBy = window["blockedBy"] as? String
                let dead = (window["exhausted"] as? Bool) == true || blockedBy != nil
                let line = NSMutableAttributedString(attributedString: text("  " + pad(window["label"] as? String ?? "", 20)))
                let amount = dead ? "   " + AppDelegate.skull : String(format: "%3d%%", Int(pct.rounded(.down)))
                line.append(text(bar(blockedBy == nil ? pct : 0) + " " + amount, mono, dead ? color(for: "out") : color(for: level(for: pct))))
                var reset = "  not started"
                if let blockedBy {
                    reset = "  unusable until the \(blockedBy) limit resets"
                } else if (window["resetSinceFetch"] as? Bool) == true {
                    reset = "  reset since last check"
                } else if let r = window["resetsAt"] as? String, let date = AppDelegate.iso.date(from: r) {
                    reset = "  resets \(clock(date)) (\(relative(date)))"
                }
                line.append(text(reset, mono, .secondaryLabelColor))
                menu.addItem(infoItem(line))
            }
        }

        let visible = lanes.filter { ($0["redundant"] as? Bool) != true }
        if !visible.isEmpty {
            menu.addItem(.separator())
            menu.addItem(infoItem(text("Where to spend next", monoBold, .secondaryLabelColor)))
            var rank = 0
            for lane in visible {
                let available = (lane["status"] as? String) == "available"
                let line = NSMutableAttributedString()
                if available {
                    rank += 1
                    line.append(text(" \(rank)  "))
                } else {
                    line.append(text(" ✗  ", mono, .systemRed))
                }
                line.append(text(pad(lane["label"] as? String ?? "", 34), mono, available ? .labelColor : .secondaryLabelColor))
                if available, let pts = lane["surplusPts"] as? Int {
                    let ptsColor: NSColor = pts >= 15 ? .systemGreen : pts <= -10 ? .systemOrange : .secondaryLabelColor
                    line.append(text(String(format: "%+5d pts  ", pts), mono, ptsColor))
                }
                line.append(text(lane["advice"] as? String ?? "", mono, .secondaryLabelColor))
                if let route = lane["route"] as? String {
                    line.append(text("  → \(route)", mono, .systemBlue))
                }
                menu.addItem(infoItem(line))
            }
        }

        menu.addItem(.separator())
        menu.addItem(actionItem("Refresh Now", #selector(refreshNow), "r"))
        menu.addItem(actionItem("Open Live View in Terminal", #selector(openWatch), "t"))
        menu.addItem(actionItem("Edit Config…", #selector(openConfig), ","))
        menu.addItem(actionItem("Quit AI Usage", #selector(quit), "q"))
    }

    // MARK: - Actions

    @objc private func noop() {}

    @objc private func refreshNow() { refresh(force: true) }

    @objc private func openWatch() {
        let script = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("ai-usage-watch.command")
        let body = "#!/bin/sh\nclear\nexec '\(command)' watch\n"
        do {
            try body.write(to: script, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
            NSWorkspace.shared.open(script)
        } catch {
            lastError = "could not open Terminal: \(error.localizedDescription)"
            rebuildMenu()
        }
    }

    @objc private func openConfig() {
        let config = URL(fileURLWithPath: NSHomeDirectory() + "/.config/ai-usage/config.json")
        NSWorkspace.shared.open(config)
    }

    @objc private func quit() { NSApp.terminate(nil) }

    /// `AIUsageBar --dump-menu`: load once, print the title and menu as text, exit. For checking output without clicking.
    func dumpMenu() {
        switch AppDelegate.run(command, ["json"]) {
        case .success(let data): snapshot = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        case .failure(let err): lastError = err.message
        }
        updateTitle()
        rebuildMenu()
        print("TITLE: " + barSummary(barModel()))
        for item in menu.items { print(item.isSeparatorItem ? "────" : item.title) }
    }
}

if let index = CommandLine.arguments.firstIndex(of: "--render-title"), index + 1 < CommandLine.arguments.count {
    AppDelegate().renderTitle(to: CommandLine.arguments[index + 1], light: CommandLine.arguments.contains("--light"))
    exit(0)
}

if CommandLine.arguments.contains("--dump-menu") {
    let delegate = AppDelegate()
    delegate.dumpMenu()
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
