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
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
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

    private func updateTitle() {
        let title = NSMutableAttributedString()
        if accounts.isEmpty {
            title.append(NSAttributedString(string: lastError == nil ? "AI …" : "AI ⚠", attributes: [.font: titleFont]))
        }
        for (index, account) in accounts.enumerated() {
            if index > 0 { title.append(NSAttributedString(string: "  ", attributes: [.font: titleFont])) }
            let short = account["short"] as? String ?? "?"
            // No explicit color: the button's default text color keeps the menu bar's vibrancy and contrast.
            title.append(NSAttributedString(string: short + " ", attributes: [.font: titleFont]))
            let headline = account["headline"] as? [String: Any] ?? [:]
            let values = headline["values"] as? [Double] ?? []
            let levels = headline["levels"] as? [String] ?? []
            if values.isEmpty {
                title.append(NSAttributedString(string: "?", attributes: [.font: titleFont, .foregroundColor: NSColor.systemRed]))
            }
            for (i, value) in values.enumerated() {
                if i > 0 { title.append(NSAttributedString(string: "/", attributes: [.font: titleFont])) }
                let lvl = i < levels.count ? levels[i] : level(for: value)
                title.append(NSAttributedString(string: String(Int(value.rounded(.down))), attributes: [.font: titleFont, .foregroundColor: color(for: lvl)]))
            }
            if (account["ok"] as? Bool) == false {
                title.append(NSAttributedString(string: "!", attributes: [.font: titleFont, .foregroundColor: NSColor.systemRed]))
            }
        }
        statusItem.button?.attributedTitle = title
        statusItem.button?.toolTip = "AI usage: % usable now per account (right now, per independent pool)"
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
                let line = NSMutableAttributedString(attributedString: text("  " + pad(window["label"] as? String ?? "", 20)))
                line.append(text(bar(pct) + " " + String(format: "%3d%%", Int(pct.rounded(.down))), mono, color(for: level(for: pct))))
                var reset = "  not started"
                if (window["resetSinceFetch"] as? Bool) == true {
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
        print("TITLE: " + (statusItem.button?.attributedTitle.string ?? ""))
        for item in menu.items { print(item.isSeparatorItem ? "────" : item.title) }
    }
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
