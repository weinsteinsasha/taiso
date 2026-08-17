// TaisoWindow — полноэкранное окно зарядки Radio Taiso.
// Компилируется install.sh: swiftc -O src/TaisoWindow.swift -o ~/.radio-taiso/bin/TaisoWindow
// Дизайн: 03_design.md W3. Инварианты:
// - таймер зачёта идёт только при видимом окне И играющем видео (не по key-фокусу);
// - позиция видео сохраняется каждые 5 сек (resume после прерывания);
// - вся запись в базу — через `taiso.py` (argv, без shell);
// - lock-файл с PID: второй запуск не открывается, мёртвый lock игнорируется.

import AVKit
import AppKit
import Foundation

let fm = FileManager.default
let taisoDir = ProcessInfo.processInfo.environment["TAISO_DIR"]
    ?? (NSHomeDirectory() + "/.radio-taiso")
let lockPath = taisoDir + "/window.lock"
let taisoPy = taisoDir + "/bin/taiso.py"

func runTaiso(_ args: [String]) -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    p.arguments = [taisoPy] + args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = Pipe()
    do { try p.run() } catch { return "" }
    p.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

// --- single instance: PID-lock со сверкой живости (eng-ревью п.7)
func acquireLock() -> Bool {
    if let pidStr = try? String(contentsOfFile: lockPath, encoding: .utf8),
       let pid = Int32(pidStr.trimmingCharacters(in: .whitespacesAndNewlines)),
       kill(pid, 0) == 0 {
        return false // живой процесс окна уже есть
    }
    try? String(ProcessInfo.processInfo.processIdentifier)
        .write(toFile: lockPath, atomically: true, encoding: .utf8)
    return true
}

func releaseLock() { try? fm.removeItem(atPath: lockPath) }

// --- конфиг: путь к видео с realpath-проверкой внутри taisoDir (security 6)
func videoURL() -> URL? {
    guard let data = fm.contents(atPath: taisoDir + "/config.json"),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let path = json["video_path"] as? String, !path.isEmpty
    else { return nil }
    let resolved = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
    let root = URL(fileURLWithPath: taisoDir).resolvingSymlinksInPath().path
    guard resolved.hasPrefix(root + "/"), fm.fileExists(atPath: resolved)
    else { return nil }
    return URL(fileURLWithPath: resolved)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var player: AVPlayer?
    var timer: Timer?
    var accrued = 0.0
    var required = 180.0
    var pausedLabel: NSTextField!
    var progressLabel: NSTextField!
    var lastSavedPos = 0.0
    var timerOnlyMode = false

    func applicationDidFinishLaunching(_ n: Notification) {
        if let r = Double(runTaiso(["exercise-required"])), r > 0 { required = r }

        let screen = NSScreen.main ?? NSScreen.screens[0]
        window = NSWindow(contentRect: screen.frame, styleMask: [.borderless],
                          backing: .buffered, defer: false)
        window.level = .screenSaver
        window.backgroundColor = .black
        window.collectionBehavior = [.fullScreenPrimary, .canJoinAllSpaces]

        let content = NSView(frame: screen.frame)
        window.contentView = content

        if let url = videoURL() {
            let item = AVPlayerItem(url: url)
            let p = AVPlayer(playerItem: item)
            let pv = AVPlayerView(frame: screen.frame)
            pv.player = p
            pv.controlsStyle = .none // без перемотки — честный досмотр
            pv.autoresizingMask = [.width, .height]
            content.addSubview(pv)
            let pos = Double(runTaiso(["video-pos"])) ?? 0
            if pos > 1 {
                p.seek(to: CMTime(seconds: pos, preferredTimescale: 600))
            }
            p.play()
            player = p
            NotificationCenter.default.addObserver(
                forName: .AVPlayerItemDidPlayToEndTime, object: item,
                queue: .main) { [weak self] _ in
                // видео короче требуемого (долг) — заводим заново
                p.seek(to: .zero); p.play()
                _ = self // keep
            }
        } else {
            timerOnlyMode = true // деградация: видео нет — просто таймер (W3)
        }

        pausedLabel = label("ПАУЗА — вернись в окно, видео должно играть",
                            size: 44, color: .systemRed)
        pausedLabel.isHidden = true
        content.addSubview(pausedLabel)
        progressLabel = label("", size: 28, color: .white)
        content.addSubview(progressLabel)
        layoutLabels(in: content.bounds)

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) {
            [weak self] _ in self?.tick()
        }
    }

    func label(_ text: String, size: CGFloat, color: NSColor) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.font = .systemFont(ofSize: size, weight: .bold)
        l.textColor = color
        l.backgroundColor = NSColor.black.withAlphaComponent(0.55)
        l.alignment = .center
        l.drawsBackground = true
        return l
    }

    func layoutLabels(in bounds: NSRect) {
        pausedLabel.frame = NSRect(x: 0, y: bounds.midY - 40,
                                   width: bounds.width, height: 80)
        progressLabel.frame = NSRect(x: 0, y: 30, width: bounds.width, height: 44)
    }

    func tick() {
        // зачёт: окно видимо (occlusion) И (видео играет ИЛИ таймер-режим) — eng-ревью п.3
        let visible = window.occlusionState.contains(.visible)
        let playing = timerOnlyMode || (player?.rate ?? 0) > 0
        let accruing = visible && playing
        if accruing {
            accrued += 0.5
        } else if visible && !timerOnlyMode {
            // окно видно, но видео стоит — перезапуск (например после seek)
            player?.play()
        }
        pausedLabel.isHidden = accruing
        let left = max(0, Int(required - accrued))
        progressLabel.stringValue = String(format: "Осталось %d:%02d", left / 60, left % 60)

        // позиция каждые 5 сек — resume после прерывания
        if let t = player?.currentTime().seconds, t - lastSavedPos >= 5 {
            lastSavedPos = t
            _ = runTaiso(["video-pos", "--set", String(format: "%.1f", t)])
        }

        if accrued >= required {
            timer?.invalidate()
            _ = runTaiso(["exercise-done", "--duration", String(Int(accrued))])
            releaseLock()
            NSApp.terminate(nil)
        }
    }

    func applicationWillTerminate(_ n: Notification) {
        if accrued < required {
            let pos = player?.currentTime().seconds ?? 0
            _ = runTaiso(["exercise-abort", "--position",
                          String(format: "%.1f", pos)])
        }
        releaseLock()
    }
}

// ============================================================ menubar mode
// Тот же бинарь, запуск `TaisoWindow --menubar` (03_design W5, PRD FR-10).
// Всё чтение — через CLI taiso.py (argv), в Swift ни строчки SQL.

final class MenubarDelegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) {
            [weak self] _ in self?.refresh()
        }
        Timer.scheduledTimer(withTimeInterval: 600, repeats: true) {
            [weak self] _ in self?.maybeAutoFeedback()
        }
        rebuildMenu()
    }

    func refresh() {
        let line = runTaiso(["status", "--line"])
        guard let btn = item.button else { return }
        if line.isEmpty {
            btn.title = "⛩"
        } else if line.contains("ДОЛГ") || line.contains("БЛОК") {
            btn.attributedTitle = NSAttributedString(
                string: line.replacingOccurrences(
                    of: " — скажи «давай зарядку»", with: ""),
                attributes: [.foregroundColor: NSColor.systemRed])
        } else {
            btn.title = line
        }
    }

    func rebuildMenu() {
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
    }

    @objc func startExercise() {
        let bin = taisoDir + "/bin/TaisoWindow"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        try? p.run() // отдельный процесс; PID-lock сам разрулит дубликаты
    }

    @objc func setInterval(_ sender: NSMenuItem) {
        _ = runTaiso(["config-set", "work_minutes_per_exercise",
                      String(sender.tag)])
        refresh()
    }

    @objc func openConfig() {
        NSWorkspace.shared.open(URL(fileURLWithPath: taisoDir + "/config.json"))
    }

    @objc func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "⛩ Radio Taiso"
        alert.informativeText = """
        Привет! Я Саша, и я папа.

        За последнее время моя жизнь сильно изменилась: я начал клодходить. С марта я каждый \
        день провожу по десять–двенадцать часов за Claude Code — от агента невозможно \
        оторваться: он всё время что-то дописывает, а ты всё время «почти закончил».

        За эти полгода у меня вырос живот и ослабла спина. Появились те самые программистские \
        проблемы, которых у меня никогда не было — потому что я никогда не был программистом.

        Своему ребёнку я ставлю родительский контроль: экранное время, лимиты, всё как \
        положено. А себе — нет. Помогло единственное: отдать контроль тому, от кого не \
        оторваться. Теперь мой агент — мой родительский контроль. Клод не работает, пока я \
        не сделаю трёхминутную японскую зарядку — ту самую, которую Япония делает каждое \
        утро с 1928 года.

        Пришло время.

        — Саша Вайнштейн
        """
        alert.addButton(withTitle: "Открыть сайт")
        alert.addButton(withTitle: "Мой LinkedIn")
        alert.addButton(withTitle: "Закрыть")
        let resp = alert.runModal()
        if resp == .alertFirstButtonReturn,
           let url = URL(string: "https://weinsteinsasha.github.io/taiso/") {
            NSWorkspace.shared.open(url)
        } else if resp == .alertSecondButtonReturn,
           let url = URL(string: "https://www.linkedin.com/in/alexander-weinstein-b4847910b/") {
            NSWorkspace.shared.open(url)
        }
    }

    // --- фидбек: вручную из меню + автопромпт раз в день после 20:00 ---
    @objc func giveFeedback() { showFeedbackDialog(auto: false) }

    func maybeAutoFeedback() {
        let hour = Calendar.current.component(.hour, from: Date())
        guard hour >= 20 else { return }
        let marker = taisoDir + "/.feedback-" + todayString()
        guard !fm.fileExists(atPath: marker) else { return }
        try? "x".write(toFile: marker, atomically: true, encoding: .utf8)
        showFeedbackDialog(auto: true)
    }

    func todayString() -> String {
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd"
        return df.string(from: Date())
    }

    func showFeedbackDialog(auto: Bool) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = auto ? "Как прошёл день с Radio Taiso?"
                                 : "Фидбек по Radio Taiso"
        alert.informativeText =
            "Пара строк: что бесило, что зашло, чего не хватает. " +
            "Улетит Саше письмом (откроется почта — просто нажми отправить)."
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 420, height: 110))
        tv.font = .systemFont(ofSize: 13)
        tv.isRichText = false
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 420, height: 110))
        scroll.documentView = tv
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll
        alert.addButton(withTitle: "Отправить")
        alert.addButton(withTitle: auto ? "Не сегодня" : "Отмена")
        alert.window.initialFirstResponder = tv
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = tv.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let stats = runTaiso(["stats"])
        let body = "\(text)\n\n---\n\(stats)\nv: phase2 · \(todayString())"
        // локальная копия — в почтовом черновике можно и передумать
        try? body.write(toFile: taisoDir + "/feedback-\(todayString()).txt",
                        atomically: true, encoding: .utf8)
        var comp = URLComponents(string: "mailto:weinsteinsasha@gmail.com")!
        comp.queryItems = [
            URLQueryItem(name: "subject", value: "radio-taiso feedback"),
            URLQueryItem(name: "body", value: body),
        ]
        if let url = comp.url { NSWorkspace.shared.open(url) }
    }
}

extension MenubarDelegate: NSMenuDelegate {
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let status = runTaiso(["status"])
        let stats = runTaiso(["stats"])
        for line in (status + "\n" + stats).split(separator: "\n") {
            let it = NSMenuItem(title: String(line), action: nil, keyEquivalent: "")
            it.isEnabled = false
            menu.addItem(it)
        }
        // watchdog: hooks молчат при работающем компе (eng-ревью п.11)
        if let silent = Int(runTaiso(["watchdog"])), silent > 6 * 3600 {
            let it = NSMenuItem(
                title: "⚠️ Hooks молчат \(silent / 3600) ч — проверь установку",
                action: nil, keyEquivalent: "")
            it.isEnabled = false
            menu.addItem(it)
        }
        menu.addItem(.separator())
        let go = NSMenuItem(title: "Зарядка сейчас", action: #selector(startExercise),
                            keyEquivalent: "g")
        go.target = self
        menu.addItem(go)
        let intervals = NSMenuItem(title: "Интервал", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let current = Int(runTaiso(["config-get-interval"])) ?? 0
        for v in [40, 50, 60, 90] {
            let it = NSMenuItem(title: "\(v) минут", action: #selector(setInterval(_:)),
                                keyEquivalent: "")
            it.target = self
            it.tag = v
            if v == current { it.state = .on }
            sub.addItem(it)
        }
        intervals.submenu = sub
        menu.addItem(intervals)
        let fb = NSMenuItem(title: "Оставить фидбек…", action: #selector(giveFeedback),
                            keyEquivalent: "f")
        fb.target = self
        menu.addItem(fb)
        let about = NSMenuItem(title: "О проекте", action: #selector(showAbout),
                               keyEquivalent: "")
        about.target = self
        menu.addItem(about)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Выйти", action:
            #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }
}

// ============================================================ entry point
let app = NSApplication.shared
if CommandLine.arguments.contains("--menubar") {
    let delegate = MenubarDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory) // без иконки в Dock
    app.run()
} else {
    guard acquireLock() else { exit(0) } // второе окно не открываем
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
