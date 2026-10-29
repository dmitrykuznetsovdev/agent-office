// Agent Office в статусной панели macOS: траты за сегодня, сессии, запуск сервера.
// Собирается scripts/build-menubar.sh в build/AgentOffice.app; без зависимостей.
import Cocoa

// ── Настройки: из Info.plist собранного приложения, иначе — по умолчанию ─────
struct Config {
    let python: String
    let server: String
    let port: Int
    let log: String

    static func load() -> Config {
        let info = Bundle.main.infoDictionary ?? [:]
        let env = ProcessInfo.processInfo.environment
        let here = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
        return Config(
            python: env["AO_PYTHON"] ?? info["AOPython"] as? String ?? "/usr/bin/python3",
            server: env["AO_SERVER"] ?? info["AOServer"] as? String
                ?? here.appendingPathComponent("../../../../server.py").standardized.path,
            port: Int(env["AO_PORT"] ?? "") ?? info["AOPort"] as? Int ?? 8767,
            log: NSHomeDirectory() + "/Library/Logs/agent-office.log")
    }

    var base: String { "http://127.0.0.1:\(port)" }
}

struct Session: Decodable {
    let id: String
    let title: String
    let project: String
    let at: Double
    let isLive: Bool
    let working: Int
    let cost: Double
    let today: Double?
}

func money(_ usd: Double) -> String { String(format: "$%.2f", usd) }

func clip(_ s: String, _ n: Int = 50) -> String {
    s.count <= n ? s : String(s.prefix(n - 1)) + "…"
}

// ── Сервер: свой дочерний процесс или чужой, уже слушающий порт ──────────────
final class Server {
    let cfg: Config
    private(set) var child: Process?
    private(set) var failures = 0        // падения подряд; после MAX_FAILURES не поднимаем
    var onChange: (() -> Void)?
    private var restarting = false
    private var quitting = false
    static let maxFailures = 10

    init(_ cfg: Config) { self.cfg = cfg }

    var gaveUp: Bool { failures >= Server.maxFailures }
    var isOurs: Bool { child?.isRunning ?? false }

    func spawn() {
        guard child == nil || !(child!.isRunning) else { return }
        guard !gaveUp else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.python)
        p.arguments = [cfg.server, "--port", String(cfg.port), "--watch-parent"]
        if let fh = openLog() {
            p.standardOutput = fh
            p.standardError = fh
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.died(proc) }
        }
        do {
            try p.run()
            child = p
            NSLog("Agent Office: server started, pid %d", p.processIdentifier)
        } catch {
            NSLog("Agent Office: cannot start server: %@", error.localizedDescription)
            failures += 1
        }
        onChange?()
    }

    private func died(_ proc: Process) {
        guard proc === child else { return }
        child = nil
        if quitting { return }
        if restarting {
            restarting = false
            spawn()
            return
        }
        failures += 1
        NSLog("Agent Office: server exited (%d), failure %d", proc.terminationStatus, failures)
        onChange?()
        if !gaveUp {
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.spawn() }
        }
    }

    /// Свой сервер перезапускаем; чужой на нашем порту — убиваем и поднимаем свой.
    func restart() {
        failures = 0
        if let c = child, c.isRunning {
            restarting = true
            c.terminate()
            return
        }
        for pid in listeners() { kill(pid, SIGTERM) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.spawn() }
    }

    func stop() {
        quitting = true
        child?.terminate()
    }

    private func listeners() -> [pid_t] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        p.arguments = ["-tiTCP:\(cfg.port)", "-sTCP:LISTEN"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return [] }
        p.waitUntilExit()
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return text.split(whereSeparator: { $0.isNewline }).compactMap { pid_t($0) }
    }

    private func openLog() -> FileHandle? {
        let fm = FileManager.default
        if !fm.fileExists(atPath: cfg.log) { fm.createFile(atPath: cfg.log, contents: nil) }
        guard let fh = FileHandle(forWritingAtPath: cfg.log) else { return nil }
        fh.seekToEndOfFile()
        return fh
    }
}

// ── Приложение ──────────────────────────────────────────────────────────────
final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let cfg = Config.load()
    lazy var server = Server(cfg)
    var item: NSStatusItem!
    let menu = NSMenu()
    var sessions: [Session] = []
    var online = false
    var misses = 0              // опросов подряд без ответа

    func applicationDidFinishLaunching(_ note: Notification) {
        if let me = Bundle.main.bundleIdentifier,
           NSRunningApplication.runningApplications(withBundleIdentifier: me).count > 1 {
            NSLog("Agent Office: already running, exiting")
            NSApp.terminate(nil)
            return
        }
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.image = NSImage(systemSymbolName: "building.2", accessibilityDescription: "Agent Office")
            b.imagePosition = .imageLeading
            b.title = " $—"
        }
        menu.delegate = self
        item.menu = menu
        server.onChange = { [weak self] in self?.refreshTitle() }

        probe { [weak self] ok in
            guard let self = self else { return }
            if ok { NSLog("Agent Office: server already on port %d", self.cfg.port) } else { self.server.spawn() }
        }
        let t = Timer(timeInterval: 5, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(t, forMode: .common)
        poll()
    }

    func applicationWillTerminate(_ note: Notification) { server.stop() }

    // ── Опрос сервера ──
    private func request(_ path: String, _ done: @escaping (Data?) -> Void) {
        var req = URLRequest(url: URL(string: cfg.base + path)!)
        req.timeoutInterval = 3
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok ? data : nil) }
        }.resume()
    }

    private func probe(_ done: @escaping (Bool) -> Void) {
        request("/api/sessions") { done($0 != nil) }
    }

    func poll() {
        request("/api/sessions") { [weak self] data in
            guard let self = self else { return }
            if let d = data, let list = try? JSONDecoder().decode([Session].self, from: d) {
                self.sessions = list
                self.online = true
                self.misses = 0
            } else {
                self.online = false
                self.misses += 1
                // сервер молчит, а своего нет — поднимаем (чужой умер или не было никого)
                if self.misses >= 3 && !self.server.isOurs { self.server.spawn() }
            }
            self.refreshTitle()
        }
    }

    var today: Double { sessions.reduce(0) { $0 + ($1.today ?? 0) } }
    var total: Double { sessions.reduce(0) { $0 + $1.cost } }
    var working: Int { sessions.reduce(0) { $0 + $1.working } }

    func refreshTitle() {
        guard let b = item?.button else { return }
        if !online { b.title = " $—"; return }
        b.title = " " + money(today) + (working > 0 ? " ·\(working)" : "")
    }

    // ── Меню строится при открытии из последних данных ──
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        if online {
            menu.addItem(label("Сегодня: \(money(today)) · за сутки: \(money(total))"))
        } else if server.gaveUp {
            menu.addItem(label("Сервер не запускается, см. лог"))
        } else {
            menu.addItem(label("Сервер не отвечает"))
        }
        menu.addItem(.separator())

        let live = sessions.filter { $0.isLive }
        let rest = sessions.filter { !$0.isLive }
        if live.isEmpty {
            menu.addItem(label(online ? "Открытых сессий нет" : "—"))
        } else {
            menu.addItem(label("Открытые"))
            for s in live { menu.addItem(sessionItem(s, dot: true)) }
        }
        if !rest.isEmpty {
            let sub = NSMenu()
            for s in rest { sub.addItem(sessionItem(s, dot: false)) }
            let holder = NSMenuItem(title: "Остальные за сутки (\(rest.count))", action: nil, keyEquivalent: "")
            holder.submenu = sub
            menu.addItem(holder)
        }
        menu.addItem(.separator())

        menu.addItem(action("Открыть офис", #selector(openOffice), key: "o"))
        menu.addItem(action("Перезапустить сервер", #selector(restartServer), key: "r"))
        menu.addItem(action("Показать лог", #selector(showLog), key: "l"))
        menu.addItem(.separator())
        let plist = NSHomeDirectory() + "/Library/LaunchAgents/local.agent-office.plist"
        let auto = FileManager.default.fileExists(atPath: plist)
        menu.addItem(label("Автозапуск: " + (auto ? "включён" : "выключен (scripts/install-launchd.sh)")))
        menu.addItem(action("Выход", #selector(NSApplication.terminate(_:)), key: "q"))
    }

    private func label(_ title: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    private func action(_ title: String, _ sel: Selector, key: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self
        return i
    }

    private func sessionItem(_ s: Session, dot: Bool) -> NSMenuItem {
        let mark = dot ? (s.working > 0 ? "● " : "○ ") : ""
        let title = "\(mark)\(clip(s.title)) — \(s.project) · \(money(s.cost))"
        let i = NSMenuItem(title: title, action: #selector(openSession(_:)), keyEquivalent: "")
        i.target = self
        i.representedObject = s.id
        i.toolTip = s.working > 0 ? "в работе: \(s.working)" : "затихла"
        return i
    }

    // ── Действия ──
    private func open(_ path: String) {
        if let u = URL(string: "http://localhost:\(cfg.port)" + path) { NSWorkspace.shared.open(u) }
    }

    @objc func openOffice() { open("/") }

    @objc func openSession(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        open("/#" + id)
    }

    @objc func restartServer() {
        server.restart()
        misses = 0
    }

    @objc func showLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: cfg.log))
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
