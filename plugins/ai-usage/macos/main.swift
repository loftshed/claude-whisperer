import AppKit

// Menu bar front end for `ai-usage json`. All provider logic lives in the Node CLI; this only renders it.

struct RunError: Error { let message: String }

/// A menu row that expands or collapses the section under it. Clicking a plain menu item closes the menu, so this
/// is a view item: it takes the click itself and the menu stays open while the section's rows show or hide.
final class DisclosureRow: NSView {
    static let height: CGFloat = 22
    private static let arrowX: CGFloat = 9
    private static let textX: CGFloat = 22

    var expanded: Bool { didSet { needsDisplay = true } }
    var label: NSAttributedString { didSet { needsDisplay = true } }
    private let onToggle: () -> Void

    init(label: NSAttributedString, expanded: Bool, width: CGFloat, onToggle: @escaping () -> Void) {
        self.label = label
        self.expanded = expanded
        self.onToggle = onToggle
        super.init(frame: NSRect(x: 0, y: 0, width: DisclosureRow.textX + width + 16, height: DisclosureRow.height))
        autoresizingMask = [.width]
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    static func width(of text: NSAttributedString) -> CGFloat { ceil(text.size().width) }

    override func mouseUp(with event: NSEvent) { onToggle() }

    override func draw(_ dirtyRect: NSRect) {
        let highlighted = enclosingMenuItem?.isHighlighted == true
        if highlighted {
            NSColor.selectedContentBackgroundColor.setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 5, dy: 0), xRadius: 4, yRadius: 4).fill()
        }
        let text = NSMutableAttributedString(attributedString: label)
        let font = text.length > 0 ? text.attribute(.font, at: 0, effectiveRange: nil) as? NSFont : nil
        let arrow = NSMutableAttributedString(string: expanded ? "▾" : "▸", attributes: [.font: font ?? NSFont.menuFont(ofSize: 12), .foregroundColor: NSColor.secondaryLabelColor])
        if highlighted {
            for s in [text, arrow] { s.addAttribute(.foregroundColor, value: NSColor.selectedMenuItemTextColor, range: NSRange(location: 0, length: s.length)) }
        }
        let y = (bounds.height - text.size().height) / 2
        arrow.draw(at: NSPoint(x: DisclosureRow.arrowX, y: y))
        text.draw(at: NSPoint(x: DisclosureRow.textX, y: y))
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .disclosureTriangle }
    override func accessibilityLabel() -> String? { label.string }
    override func accessibilityValue() -> Any? { expanded ? 1 : 0 }
    override func accessibilityPerformPress() -> Bool { onToggle(); return true }
}

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
    /// Collapsed dropdown sections ("account:<id>", "ranking"), remembered across launches. Everything starts expanded.
    private var collapsed = Set(UserDefaults.standard.stringArray(forKey: "collapsedSections") ?? [])
    /// Usage hidden from the menu bar: the status item shrinks to "AI" until shown again. Remembered across launches.
    private var compactBar = UserDefaults.standard.bool(forKey: "compactBar")
    private var pendingMenuOpen: DispatchWorkItem?
    private var sections: [String: (row: DisclosureRow, item: NSMenuItem, label: NSAttributedString, summary: NSAttributedString, children: [NSMenuItem])] = [:]

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
        // No statusItem.menu: an attached menu opens on the first click and swallows a double-click.
        statusItem.button?.target = self
        statusItem.button?.action = #selector(statusClicked)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
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
        // Drain stderr concurrently: reading stdout to the end first would deadlock if the child filled the
        // stderr pipe (64 KB) before closing stdout.
        var errData = Data()
        let stderrDone = DispatchGroup()
        stderrDone.enter()
        DispatchQueue.global(qos: .utility).async {
            errData = err.fileHandleForReading.readDataToEndOfFile()
            stderrDone.leave()
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        stderrDone.wait()
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

    // MARK: - Menu bar pills, one per provider

    /// Text-style skull (U+2620 with the text variation selector) so it takes a colour.
    static let skull = "\u{2620}\u{FE0E}"
    /// Marks a pool near its weekly rollover with capacity left: spend it before it is lost.
    static let expiringMark = "\u{23F3}"

    private struct Gauge { let pct: Double; let level: String; let resetsAt: Date?; let exhausted: Bool; let expiring: Bool }
    /// One independently limited pool: its short (5-hour) window and its weekly window.
    private struct PoolReading { let tag: String?; let short: Gauge?; let weekly: Gauge? }
    private struct ProviderPill { let label: String; let pools: [PoolReading]; let stale: Bool }
    private typealias BarModel = (pills: [ProviderPill], unavailable: [String], refreshFailed: Bool)

    private let barLabelFont = NSFont.systemFont(ofSize: 10.5, weight: .medium)
    private let barTagFont = NSFont.systemFont(ofSize: 8, weight: .bold)
    private let barNumberFont = NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .semibold)
    private let barTimeFont = NSFont.monospacedDigitSystemFont(ofSize: 8.5, weight: .semibold)
    // The skull glyph is drawn small by the symbol font; a larger size makes it read at a glance.
    private let barSkullFont = NSFont.systemFont(ofSize: 13, weight: .regular)
    private let barMarkFont = NSFont.systemFont(ofSize: 8.5, weight: .regular)

    /// `ai-usage json` accounts → one pill per provider, from each account's `pills` (pool readings).
    private func barModel() -> BarModel {
        func gauge(_ raw: Any?) -> Gauge? {
            guard let g = raw as? [String: Any], let pct = g["pct"] as? Double else { return nil }
            return Gauge(pct: pct, level: g["level"] as? String ?? level(for: pct),
                         resetsAt: (g["resetsAt"] as? String).flatMap { AppDelegate.iso.date(from: $0) },
                         exhausted: (g["exhausted"] as? Bool) == true, expiring: (g["expiring"] as? Bool) == true)
        }
        var pills: [ProviderPill] = []
        var unavailable: [String] = []
        for account in accounts {
            let label = account["short"] as? String ?? "?"
            let pools = (account["pills"] as? [[String: Any]] ?? []).map {
                PoolReading(tag: $0["tag"] as? String, short: gauge($0["short"]), weekly: gauge($0["weekly"]))
            }
            if pools.isEmpty { unavailable.append(label); continue }
            pills.append(ProviderPill(label: label, pools: pools, stale: (account["ok"] as? Bool) == false))
        }
        return (pills, unavailable, lastError != nil)
    }

    /// Time until a rollover in the largest whole unit: "3d", "5h", "40m".
    private func timeLeft(_ date: Date?) -> String {
        guard let date else { return "" }
        let minutes = Int(date.timeIntervalSinceNow / 60)
        if minutes <= 0 { return "now" }
        if minutes >= 1440 { return "\(minutes / 1440)d" }
        if minutes >= 60 { return "\(minutes / 60)h" }
        return "\(minutes)m"
    }

    private func width(_ text: String, _ font: NSFont) -> CGFloat {
        ceil(NSAttributedString(string: text, attributes: [.font: font]).size().width)
    }

    /// A piece of text in a pill; the same list is used to measure and to draw.
    private struct Run { let text: String; let font: NSFont; let color: NSColor; let gap: CGFloat; var raise: CGFloat = 0 }

    /// "CP 16·78 3d": label, 5-hour % left, weekly % left, time to the weekly rollover. A pool whose week is
    /// used up shows ☠ and the time until it is back; one near its rollover with capacity left gets ⏳.
    private func runs(for pill: ProviderPill) -> [Run] {
        let muted = labelTint(0.75)
        var out = [Run(text: pill.label, font: barLabelFont, color: .labelColor, gap: 0)]
        for (i, pool) in pill.pools.enumerated() {
            if i > 0 { out.append(Run(text: "\u{2502}", font: barLabelFont, color: labelTint(0.35), gap: 3)) }
            if let tag = pool.tag { out.append(Run(text: tag, font: barTagFont, color: muted, gap: i > 0 ? 3 : 4, raise: -2)) }
            let lead: CGFloat = pool.tag == nil ? 4 : 1
            if let weekly = pool.weekly, weekly.exhausted {
                out.append(Run(text: AppDelegate.skull, font: barSkullFont, color: levelText(color(for: "out")), gap: lead))
                out.append(Run(text: timeLeft(weekly.resetsAt), font: barTimeFont, color: muted, gap: 1, raise: -1.5))
                continue
            }
            if let short = pool.short {
                out.append(Run(text: String(Int(short.pct.rounded(.down))), font: barNumberFont, color: levelText(color(for: short.level)), gap: lead))
            }
            if let weekly = pool.weekly {
                if pool.short != nil { out.append(Run(text: "\u{00B7}", font: barNumberFont, color: muted, gap: 1)) }
                out.append(Run(text: String(Int(weekly.pct.rounded(.down))), font: barNumberFont, color: levelText(color(for: weekly.level)), gap: pool.short == nil ? lead : 1))
                out.append(Run(text: timeLeft(weekly.resetsAt), font: barTimeFont, color: weekly.expiring ? levelText(.systemOrange) : muted, gap: 2, raise: -1.5))
                if weekly.expiring { out.append(Run(text: AppDelegate.expiringMark, font: barMarkFont, color: .labelColor, gap: 1)) }
            }
        }
        if pill.stale { out.append(Run(text: "!", font: barNumberFont, color: .systemRed, gap: 1)) }
        return out
    }

    /// The shrunken bar: one pill with each provider's weekly % left behind a tiny label, "ᶜʷ44 ᶜᴾ91 ᴬᴳ53·100".
    /// No 5-hour numbers or rollover times; ☠ for a week used up, ⏳ for one to spend before its rollover.
    private func compactRuns(_ pills: [ProviderPill]) -> [Run] {
        let muted = labelTint(0.75)
        var out: [Run] = []
        for (i, pill) in pills.enumerated() {
            out.append(Run(text: pill.label, font: barTagFont, color: muted, gap: i > 0 ? 5 : 0, raise: -2))
            for (j, pool) in pill.pools.enumerated() {
                guard let weekly = pool.weekly else { continue }
                if j > 0 { out.append(Run(text: "\u{00B7}", font: barNumberFont, color: muted, gap: 1)) }
                let lead: CGFloat = j > 0 ? 1 : 2
                if weekly.exhausted {
                    out.append(Run(text: AppDelegate.skull, font: barSkullFont, color: levelText(color(for: "out")), gap: lead))
                } else {
                    out.append(Run(text: String(Int(weekly.pct.rounded(.down))), font: barNumberFont, color: levelText(color(for: weekly.level)), gap: lead))
                    if weekly.expiring { out.append(Run(text: AppDelegate.expiringMark, font: barMarkFont, color: .labelColor, gap: 1)) }
                }
            }
            if pill.stale { out.append(Run(text: "!", font: barNumberFont, color: .systemRed, gap: 1)) }
        }
        return out
    }

    private func barImage(_ model: BarModel, compact: Bool = false) -> NSImage {
        let height: CGFloat = 22, boxHeight: CGFloat = 18, inset: CGFloat = compact ? 5 : 6, pillGap: CGFloat = 4
        let pillRuns = compact ? (model.pills.isEmpty ? [] : [compactRuns(model.pills)]) : model.pills.map(runs(for:))
        let boxWidths = pillRuns.map { runs in 2 * inset + runs.reduce(0) { $0 + $1.gap + width($1.text, $1.font) } }
        let unavailableText = model.unavailable.map { "\($0) ?" }.joined(separator: "  ")
        // The last refresh failed as a whole: the numbers are from the previous one.
        let failedMark = "\u{26A0}\u{FE0E}"
        var total = boxWidths.reduce(0, +) + CGFloat(max(0, boxWidths.count - 1)) * pillGap
        if !unavailableText.isEmpty { total += (total > 0 ? pillGap : 0) + width(unavailableText, barLabelFont) }
        if model.refreshFailed { total += (total > 0 ? pillGap : 0) + width(failedMark, barLabelFont) }

        let image = NSImage(size: NSSize(width: max(total, 1), height: height), flipped: false) { _ in
            @discardableResult
            func draw(_ string: String, _ font: NSFont, _ color: NSColor, at x: CGFloat, raise: CGFloat = 0) -> CGFloat {
                let text = NSAttributedString(string: string, attributes: [.font: font, .foregroundColor: color])
                let size = text.size()
                text.draw(at: NSPoint(x: x, y: (height - size.height) / 2 + raise))
                return ceil(size.width)
            }
            var x: CGFloat = 0
            for (i, runs) in pillRuns.enumerated() {
                if i > 0 { x += pillGap }
                let box = NSRect(x: x, y: (height - boxHeight) / 2, width: boxWidths[i], height: boxHeight)
                NSColor.labelColor.withAlphaComponent(0.13).setFill()
                NSBezierPath(roundedRect: box, xRadius: 5, yRadius: 5).fill()
                var cx = x + inset
                for run in runs {
                    cx += run.gap
                    cx += draw(run.text, run.font, run.color, at: cx, raise: run.raise)
                }
                x += boxWidths[i]
            }
            if !unavailableText.isEmpty {
                if x > 0 { x += pillGap }
                x += draw(unavailableText, self.barLabelFont, .systemRed, at: x)
            }
            if model.refreshFailed {
                if x > 0 { x += pillGap }
                draw(failedMark, self.barLabelFont, .systemRed, at: x)
            }
            return true
        }
        image.isTemplate = false
        return image
    }

    /// Label colour at reduced opacity, resolved when drawn. (labelColor.withAlphaComponent resolves immediately,
    /// freezing the appearance at build time; secondaryLabelColor gets no vibrancy inside an image.)
    private func labelTint(_ alpha: CGFloat) -> NSColor {
        NSColor(name: nil) { appearance in
            let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return (dark ? NSColor.white : NSColor.black).withAlphaComponent(alpha)
        }
    }

    /// Level colour for numbers, darkened in light mode where bright green is hard to read.
    private func levelText(_ tint: NSColor) -> NSColor {
        NSColor(name: nil) { appearance in
            let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return dark ? tint : (tint.usingColorSpace(.deviceRGB)?.blended(withFraction: 0.4, of: .black) ?? tint)
        }
    }

    /// Same layout as text, for accessibility and `--dump-menu`: "CW ☠5h · CP 16·78 3d · …".
    private func barSummary(_ model: BarModel) -> String {
        var parts = model.pills.map { pill -> String in
            let pools = pill.pools.map { pool -> String in
                let tag = pool.tag ?? ""
                if let weekly = pool.weekly, weekly.exhausted { return "\(tag)\(AppDelegate.skull)\(timeLeft(weekly.resetsAt))" }
                var values: [String] = []
                if let short = pool.short { values.append(String(Int(short.pct.rounded(.down)))) }
                if let weekly = pool.weekly {
                    values.append("\(Int(weekly.pct.rounded(.down))) \(timeLeft(weekly.resetsAt))" + (weekly.expiring ? AppDelegate.expiringMark : ""))
                }
                return tag + values.joined(separator: "\u{00B7}")
            }
            return "\(pill.label) \(pools.joined(separator: " "))\(pill.stale ? "!" : "")"
        }
        if !model.unavailable.isEmpty { parts.append(model.unavailable.map { "\($0) ?" }.joined(separator: " · ")) }
        if model.refreshFailed { parts.append("refresh failed") }
        return parts.joined(separator: " · ")
    }

    private func updateTitle() {
        guard let button = statusItem.button else { return }
        if accounts.isEmpty {
            button.image = nil
            button.attributedTitle = NSAttributedString(string: lastError == nil ? "AI …" : "AI ⚠", attributes: [.font: titleFont])
            return
        }
        let model = barModel()
        if model.pills.isEmpty && model.unavailable.isEmpty {
            // Nothing to draw (no accounts configured, or output from an older ai-usage without sections).
            button.image = nil
            button.attributedTitle = NSAttributedString(string: model.refreshFailed ? "AI ⚠" : "AI –", attributes: [.font: titleFont])
            button.toolTip = lastError ?? "ai-usage returned no accounts; run ai-usage config to check the configuration"
            return
        }
        button.attributedTitle = NSAttributedString(string: "")
        button.image = barImage(model, compact: compactBar)
        button.imagePosition = .imageOnly
        button.setAccessibilityLabel("AI usage, % left: " + barSummary(model))
        if compactBar {
            button.toolTip = "Weekly % left per account (shrunk). Double-click for the full pills; click for the menu."
            return
        }
        button.toolTip = "One pill per provider: 5-hour % left · weekly % left, then time to the weekly rollover (3d, 5h).\n☠ = used up for the week, then time until it is back · ⏳ = rollover soon with capacity left: spend it.\nAntigravity pools: G = Gemini, C = Claude & GPT-OSS. ! = last refresh failed.\nDouble-click to shrink to weekly % only when the menu bar is crowded."
    }

    /// `AIUsageBar --render-title <png> [--light] [--compact]`: draw the menu bar pills to a PNG for checking.
    func renderTitle(to path: String, light: Bool, compact: Bool) {
        if case .success(let data) = AppDelegate.run(command, ["json"]) {
            snapshot = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        }
        let image = barImage(barModel(), compact: compact)
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

    /// Adds a heading that expands or collapses `children`. Collapsed, the heading shows `summary` after the label.
    private func addSection(_ key: String, _ label: NSAttributedString, summary: NSAttributedString, children: [NSMenuItem]) {
        let widest = max(DisclosureRow.width(of: label), DisclosureRow.width(of: joined(label, summary)))
        let row = DisclosureRow(label: label, expanded: true, width: widest) { [weak self] in self?.toggle(key) }
        let item = NSMenuItem(title: label.string, action: nil, keyEquivalent: "")
        item.view = row
        menu.addItem(item)
        children.forEach(menu.addItem)
        sections[key] = (row, item, label, summary, children)
        applySection(key)
    }

    private func joined(_ label: NSAttributedString, _ summary: NSAttributedString) -> NSAttributedString {
        let s = NSMutableAttributedString(attributedString: label)
        if summary.length > 0 { s.append(text("   ")); s.append(summary) }
        return s
    }

    private func applySection(_ key: String) {
        guard let section = sections[key] else { return }
        let isCollapsed = collapsed.contains(key)
        section.children.forEach { $0.isHidden = isCollapsed }
        section.row.expanded = !isCollapsed
        section.row.label = isCollapsed ? joined(section.label, section.summary) : section.label
        section.item.title = (isCollapsed ? "▸ " : "▾ ") + section.row.label.string
    }

    private func toggle(_ key: String) {
        if collapsed.remove(key) == nil { collapsed.insert(key) }
        UserDefaults.standard.set(collapsed.sorted(), forKey: "collapsedSections")
        applySection(key)
    }

    /// One-line account summary for a collapsed heading: "5h 96% · wk 44% · Fable wk 89%".
    private func windowSummary(_ account: [String: Any]) -> NSAttributedString {
        let s = NSMutableAttributedString()
        if account["error"] is String { s.append(text("⚠ ", mono, .systemRed)) }
        for (i, window) in (account["windows"] as? [[String: Any]] ?? []).enumerated() {
            if i > 0 { s.append(text(" · ", mono, .tertiaryLabelColor)) }
            let name = (window["label"] as? String ?? "")
                .replacingOccurrences(of: "5-hour", with: "5h")
                .replacingOccurrences(of: "weekly", with: "wk")
            let pct = window["remainingPct"] as? Double ?? 0
            let dead = (window["exhausted"] as? Bool) == true || window["blockedBy"] is String
            s.append(text(name + " ", mono, .secondaryLabelColor))
            s.append(text(dead ? AppDelegate.skull : "\(Int(pct.rounded(.down)))%", mono, dead ? color(for: "out") : color(for: level(for: pct))))
        }
        return s
    }

    private func rebuildMenu() {
        menu.removeAllItems()
        sections = [:]
        var status = refreshing ? "Refreshing…" : "Not loaded yet"
        if let generated = snapshot?["generatedAt"] as? String, let date = AppDelegate.iso.date(from: generated), !refreshing {
            status = "Updated \(clock(date))"
        }
        menu.addItem(infoItem(text("AI usage · \(status)", monoBold, .secondaryLabelColor)))
        menu.addItem(infoItem(text("Menu bar: 5-hour % · weekly % + time to rollover · ☠ used up for the week · ⏳ spend before rollover · G Gemini, C Claude & GPT-OSS · double-click the bar to shrink it", mono, .tertiaryLabelColor)))
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
            var rows: [NSMenuItem] = []
            if let error = account["error"] as? String {
                let age = (account["ageMinutes"] as? Int).map { ", showing data from \($0)m ago" } ?? ""
                rows.append(infoItem(text("  ⚠ \(error.prefix(90))\(age)", mono, .systemRed)))
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
                rows.append(infoItem(line))
            }
            addSection("account:\(account["id"] as? String ?? label)", heading, summary: windowSummary(account), children: rows)
        }

        let visible = lanes.filter { ($0["redundant"] as? Bool) != true }
        if !visible.isEmpty {
            menu.addItem(.separator())
            var rows: [NSMenuItem] = []
            var best = NSMutableAttributedString()
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
                if available, rank == 1 {
                    best = NSMutableAttributedString(attributedString: text("1  " + (lane["label"] as? String ?? ""), mono, .secondaryLabelColor))
                    if let pts = lane["surplusPts"] as? Int { best.append(text(String(format: "  %+d pts", pts), mono, .secondaryLabelColor)) }
                }
                rows.append(infoItem(line))
            }
            addSection("ranking", text("Where to spend next", monoBold, .secondaryLabelColor), summary: best, children: rows)
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

    /// Single click opens the menu once the double-click interval has passed; a double-click hides or shows the
    /// usage instead. Right-click opens the menu at once.
    @objc private func statusClicked() {
        pendingMenuOpen?.cancel()
        pendingMenuOpen = nil
        guard let event = NSApp.currentEvent, event.type != .rightMouseUp else { return openMenu() }
        if event.clickCount >= 2 { return toggleCompactBar() }
        let open = DispatchWorkItem { [weak self] in self?.openMenu() }
        pendingMenuOpen = open
        DispatchQueue.main.asyncAfter(deadline: .now() + NSEvent.doubleClickInterval, execute: open)
    }

    private func openMenu() {
        pendingMenuOpen = nil
        statusItem.menu = menu
        statusItem.button?.performClick(nil)  // tracks the menu until it closes
        statusItem.menu = nil
    }

    @objc private func toggleCompactBar() {
        compactBar.toggle()
        UserDefaults.standard.set(compactBar, forKey: "compactBar")
        updateTitle()
        rebuildMenu()
    }

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

    /// `AIUsageBar --dump-menu [--collapse all|key,key]`: load once, print the title and the visible menu as text, exit.
    /// For checking output without clicking. `--collapse` replaces the saved collapsed sections for this run only.
    func dumpMenu(collapse: String?) {
        switch AppDelegate.run(command, ["json"]) {
        case .success(let data): snapshot = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        case .failure(let err): lastError = err.message
        }
        if let collapse {
            collapsed = collapse == "all"
                ? Set(accounts.map { "account:\($0["id"] as? String ?? $0["label"] as? String ?? "")" } + ["ranking"])
                : Set(collapse.split(separator: ",").map(String.init))
        }
        updateTitle()
        rebuildMenu()
        print("TITLE: " + barSummary(barModel()))
        for item in menu.items where !item.isHidden { print(item.isSeparatorItem ? "────" : item.title) }
    }
}

if let index = CommandLine.arguments.firstIndex(of: "--render-title"), index + 1 < CommandLine.arguments.count {
    AppDelegate().renderTitle(to: CommandLine.arguments[index + 1], light: CommandLine.arguments.contains("--light"), compact: CommandLine.arguments.contains("--compact"))
    exit(0)
}

if CommandLine.arguments.contains("--dump-menu") {
    let delegate = AppDelegate()
    let args = CommandLine.arguments
    let collapse = args.firstIndex(of: "--collapse").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
    delegate.dumpMenu(collapse: collapse)
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
