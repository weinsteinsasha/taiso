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
    writePrivate(String(ProcessInfo.processInfo.processIdentifier), to: lockPath)
    return true
}

func releaseLock() { try? fm.removeItem(atPath: lockPath) }

// --- локализация: "lang" из config.json ("en" | "ru"), дефолт en
func cfgLang() -> String {
    guard let data = fm.contents(atPath: taisoDir + "/config.json"),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let l = json["lang"] as? String else { return "en" }
    return l == "ru" ? "ru" : "en"
}

func L(_ en: String, _ ru: String) -> String { cfgLang() == "ru" ? ru : en }

func cfgBool(_ key: String, _ def: Bool) -> Bool {
    guard let data = fm.contents(atPath: taisoDir + "/config.json"),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return def }
    return (json[key] as? Bool) ?? def
}

func writePrivate(_ text: String, to path: String) {
    fm.createFile(atPath: path, contents: text.data(using: .utf8),
                  attributes: [.posixPermissions: 0o600])
}

func countAllApps() -> Bool {
    guard let data = fm.contents(atPath: taisoDir + "/config.json"),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return true }
    return (json["count_all_apps"] as? Bool) ?? true
}

// --- конфиг: путь к видео с realpath-проверкой внутри taisoDir (security 6)
func videoURL() -> URL? {
    guard let data = fm.contents(atPath: taisoDir + "/config.json"),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let path = json["video_path"] as? String, !path.isEmpty
    else { return nil }
    let resolved = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
    let root = URL(fileURLWithPath: taisoDir).resolvingSymlinksInPath().path
    guard resolved.hasPrefix(root + "/"), fm.fileExists(atPath: resolved),
          ["mp4", "mov", "m4v"].contains(URL(fileURLWithPath: resolved).pathExtension.lowercased()),
          (try? fm.attributesOfItem(atPath: resolved))?[.type] as? FileAttributeType == .typeRegular
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
    var finished = false

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
                guard let s = self else { return }
                // хвост долга ≤15 сек не заслуживает повтора ролика — зачёт сразу
                if s.required - s.accrued <= 15 {
                    s.finish()
                } else {
                    p.seek(to: .zero); p.play()
                }
            }
        } else {
            timerOnlyMode = true // деградация: видео нет — просто таймер (W3)
        }

        pausedLabel = label(L("PAUSED — come back, the video must be playing",
                              "ПАУЗА — вернись в окно, видео должно играть"),
                            size: 44, color: .systemRed)
        pausedLabel.isHidden = true
        content.addSubview(pausedLabel)
        progressLabel = label("", size: 28, color: .white)
        content.addSubview(progressLabel)
        layoutLabels(in: content.bounds)

        // кнопка «взять в долг» — только при блоке и один раз за период
        if runTaiso(["postpone-available"]) == "yes" {
            let btn = NSButton(
                title: L("Borrow +20 min — exercise later (×1.5)",
                         "Взять в долг +20 мин — сделаю позже (×1.5)"),
                target: self, action: #selector(postponePressed))
            btn.bezelStyle = .rounded
            btn.controlSize = .large
            btn.frame = NSRect(x: content.bounds.midX - 190,
                               y: content.bounds.height - 90, width: 380, height: 44)
            btn.autoresizingMask = [.minXMargin, .maxXMargin, .minYMargin]
            content.addSubview(btn)
        }

        let escHint = label(L("Esc — leave without credit", "Esc — выйти без зачёта"),
                            size: 14, color: NSColor.white.withAlphaComponent(0.6))
        escHint.frame = NSRect(x: 0, y: 6, width: content.bounds.width, height: 20)
        escHint.autoresizingMask = [.width]
        content.addSubview(escHint)
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            if ev.keyCode == 53 { // Esc: прерывание честно логируется, позиция сохраняется
                self?.abortAndQuit()
                return nil
            }
            return ev
        }

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) {
            [weak self] _ in self?.tick()
        }
    }

    func abortAndQuit() {
        guard !finished else { return }
        finished = true
        timer?.invalidate()
        let pos = player?.currentTime().seconds ?? 0
        _ = runTaiso(["exercise-abort", "--position",
                      String(format: "%.1f", pos.isFinite ? pos : 0)])
        accrued = required
        releaseLock()
        NSApp.terminate(nil)
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
        progressLabel.stringValue = String(
            format: L("%d:%02d left", "Осталось %d:%02d"), left / 60, left % 60)

        // позиция каждые 5 сек — resume после прерывания
        if let t = player?.currentTime().seconds, t.isFinite, t - lastSavedPos >= 5 {
            lastSavedPos = t
            _ = runTaiso(["video-pos", "--set", String(format: "%.1f", t)])
        }

        if accrued >= required {
            finish()
        }
    }

    func finish() {
        guard !finished else { return }
        finished = true
        timer?.invalidate()
        _ = runTaiso(["exercise-done", "--duration", String(Int(accrued))])
        accrued = required // чтобы willTerminate не записал abort
        releaseLock()
        NSApp.terminate(nil)
    }

    @objc func postponePressed() {
        _ = runTaiso(["postpone"])
        let pos = player?.currentTime().seconds ?? 0
        _ = runTaiso(["exercise-abort", "--position", String(format: "%.1f", pos)])
        accrued = required // не дублировать abort в willTerminate
        finished = true
        releaseLock()
        NSApp.terminate(nil)
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
        // универсальный учёт: раз в минуту, если был ввод — тикаем баланс,
        // независимо от приложения (config: count_all_apps)
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
            guard countAllApps() else { return }
            let idle = [CGEventType.keyDown, .mouseMoved, .leftMouseDown,
                        .scrollWheel].map {
                CGEventSource.secondsSinceLastEventType(
                    .combinedSessionState, eventType: $0)
            }.min() ?? 9999
            if idle < 60 { _ = runTaiso(["ping-activity"]) }
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
        // тот же путь, что у `taiso go`: python-спавн с setsid — надёжный detach
        _ = runTaiso(["go"])
    }

    @objc func setLangItem(_ sender: NSMenuItem) {
        if let code = sender.representedObject as? String {
            _ = runTaiso(["config-set-lang", code])
            refresh()
        }
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
        let storyRU = """
        Привет! Я Саша, и я папа.

        За последнее время моя жизнь сильно изменилась: я начал клодкодить. С марта я каждый \
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
        let storyEN = """
        Hi! I'm Sasha, and I'm a dad.

        My life has changed a lot recently: I started Claude-coding. Since March I've been \
        spending ten to twelve hours a day inside Claude Code — you can't tear yourself away \
        from an agent: it's always finishing something up, and you're always "almost done."

        In those six months I grew a belly and my back got weak. I developed the classic \
        programmer problems I'd never had — because I had never been a programmer.

        I set up parental controls for my kid: screen time, limits, all of it. And none for \
        myself. Only one thing helped: giving the controls to the thing I can't quit. Now my \
        agent is my parental control. Claude won't work until I've done the three-minute \
        Japanese routine Japan has broadcast every morning since 1928.

        It was time.

        — Sasha Weinstein
        """
        alert.informativeText = L(storyEN, storyRU)
        alert.addButton(withTitle: L("Open website", "Открыть сайт"))
        alert.addButton(withTitle: L("My LinkedIn", "Мой LinkedIn"))
        alert.addButton(withTitle: L("Close", "Закрыть"))
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
        guard cfgBool("feedback_prompt", true) else { return }
        let hour = Calendar.current.component(.hour, from: Date())
        guard hour >= 20 else { return }
        let marker = taisoDir + "/feedback-" + todayString() + ".prompted"
        guard !fm.fileExists(atPath: marker) else { return }
        writePrivate("x", to: marker)
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
        alert.messageText = auto
            ? L("How was your day with Radio Taiso?", "Как прошёл день с Radio Taiso?")
            : L("Radio Taiso feedback", "Фидбек по Radio Taiso")
        alert.informativeText = L(
            "A couple of lines: what annoyed you, what clicked, what's missing. " +
            "It goes to Sasha by email (your mail app opens — just hit send).",
            "Пара строк: что бесило, что зашло, чего не хватает. " +
            "Улетит Саше письмом (откроется почта — просто нажми отправить).")
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 420, height: 110))
        tv.font = .systemFont(ofSize: 13)
        tv.isRichText = false
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 420, height: 110))
        scroll.documentView = tv
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll
        alert.addButton(withTitle: L("Send", "Отправить"))
        alert.addButton(withTitle: auto ? L("Not today", "Не сегодня")
                                        : L("Cancel", "Отмена"))
        alert.window.initialFirstResponder = tv
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = tv.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let stats = runTaiso(["stats"])
        let body = "\(text)\n\n---\n\(stats)\nv: phase2 · \(todayString())"
        // локальная копия — в почтовом черновике можно и передумать
        writePrivate(body, to: taisoDir + "/feedback-\(todayString()).txt")
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
                title: L("⚠️ Hooks silent for \(silent / 3600) h — check the install",
                         "⚠️ Hooks молчат \(silent / 3600) ч — проверь установку"),
                action: nil, keyEquivalent: "")
            it.isEnabled = false
            menu.addItem(it)
        }
        menu.addItem(.separator())
        let go = NSMenuItem(title: L("Exercise now", "Зарядка сейчас"),
                            action: #selector(startExercise), keyEquivalent: "g")
        go.target = self
        menu.addItem(go)
        let intervals = NSMenuItem(title: L("Interval", "Интервал"),
                                   action: nil, keyEquivalent: "")
        let sub = NSMenu()
        let current = Int(runTaiso(["config-get-interval"])) ?? 0
        for v in [40, 50, 60, 90] {
            let it = NSMenuItem(title: L("\(v) minutes", "\(v) минут"),
                                action: #selector(setInterval(_:)), keyEquivalent: "")
            it.target = self
            it.tag = v
            if v == current { it.state = .on }
            sub.addItem(it)
        }
        intervals.submenu = sub
        menu.addItem(intervals)
        let langItem = NSMenuItem(title: "Language / Язык", action: nil, keyEquivalent: "")
        let langSub = NSMenu()
        let curLang = cfgLang()
        for (code, title) in [("en", "English"), ("ru", "Русский")] {
            let it = NSMenuItem(title: title, action: #selector(setLangItem(_:)),
                                keyEquivalent: "")
            it.target = self
            it.representedObject = code
            if code == curLang { it.state = .on }
            langSub.addItem(it)
        }
        langItem.submenu = langSub
        menu.addItem(langItem)
        let fb = NSMenuItem(title: L("Leave feedback…", "Оставить фидбек…"),
                            action: #selector(giveFeedback), keyEquivalent: "f")
        fb.target = self
        menu.addItem(fb)
        let about = NSMenuItem(title: L("About", "О проекте"),
                               action: #selector(showAbout), keyEquivalent: "")
        about.target = self
        menu.addItem(about)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: L("Quit", "Выйти"), action:
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
