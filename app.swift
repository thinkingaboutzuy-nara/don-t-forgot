import AppKit
import SwiftUI
import Combine
import UserNotifications

// MARK: - Config

struct AppConfig: Decodable {
    var appName: String
    var bundleIdentifier: String?
    var projectDirectory: String?
    var dataDirectory: String
    var conversationURL: String?
    var dingtalkAppPath: String
    var timeZone: String
    var scheduleHours: [Int]?
    var scheduleText: String?
    var hours: [Int] { (scheduleHours ?? [9, 15]).sorted() }
    var schedule: String { scheduleText ?? "每天 " + hours.map { String(format: "%02d:00", $0) }.joined(separator: "、") + " 整理" }
    static let fallback = AppConfig(appName: "别忘了", bundleIdentifier: nil, projectDirectory: nil,
                                    dataDirectory: "~/Library/Application Support/DingTalkTodo", conversationURL: nil,
                                    dingtalkAppPath: "/Applications/DingTalk.app", timeZone: TimeZone.current.identifier,
                                    scheduleHours: [9, 15], scheduleText: nil)
    static func load() -> AppConfig? {
        guard let url = Bundle.main.url(forResource: "config", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(AppConfig.self, from: data)
    }
}
let loadedConfig = AppConfig.load()
let appConfig = loadedConfig ?? .fallback
let appTimeZone = TimeZone(identifier: appConfig.timeZone) ?? .current
let appCalendar: Calendar = {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = appTimeZone
    return calendar
}()

// MARK: - Data

struct Source: Codable, Hashable {
    var chat: String
    var time: String
    var quote: String
    var messageId: String
    var conversationId: String
    var senderName: String?
    var senderId: String?
    var recipientName: String?
    var conversationType: String?
    var directionText: String {
        if conversationType == "system" || conversationId == "todo" { return "系统待办记录" }
        if conversationType == "report" { return "日报待办" }
        guard let senderName, !senderName.isEmpty else { return "发送人未确认" }
        guard let recipientName, !recipientName.isEmpty else { return "\(senderName) · 接收对象未确认" }
        return "\(senderName) → \(recipientName)"
    }
}
struct Dispatch: Codable, Hashable {
    var threadId: String?
    var title: String?
    var status: String?
}
/// Link to a todo in the work-report platform. `changedAt` is when整理 last saw the platform flip it.
struct ReportTodo: Codable, Hashable {
    var id: String
    var version: Int
    var done: Bool
    var reportId: String?
    var workDate: String?
    var changedAt: String?
}
struct Todo: Codable, Identifiable {
    var id: String
    var title: String
    var detail: String
    var category: String
    var priority: Int
    var due: String?
    var sources: [Source]
    var resolution: String?
    var needsConfirm: Bool?
    var waitingSince: String?
    var dispatch: Dispatch?
    var firstSeen: String?
    var lastSeen: String?
    var closedAt: String?
    var reportTodo: ReportTodo?
    static let confirmPrefixes = ["待确认：", "待确认:", "待确认 "]
    var isUnconfirmed: Bool { needsConfirm ?? title.hasPrefix("待确认") }
    var displayTitle: String {
        for prefix in Todo.confirmPrefixes where title.hasPrefix(prefix) { return String(title.dropFirst(prefix.count)) }
        return title
    }
}
struct SyncInfo: Codable {
    var state: String?
    var message: String?
    var scannedThrough: String?
    var scope: String?
    var schedule: String?
    var mode: String?
    var runThreadId: String?
}
struct Inbox: Codable {
    var schemaVersion: Int
    var updatedAt: String?
    var tasks: [Todo]
    var sync: SyncInfo?
}
/// One entry per task in actions.json. Written only by this window; the整理 job reads it as user feedback.
struct LocalAction: Codable, Equatable {
    var status: String?
    var until: Double?
    var updatedAt: String
    var reason: String?
    var note: String?
    var handoffAt: String?
}
struct Closure {
    var icon: String
    var headline: String
    var when: String?
    var detail: String?
}
struct WorkGroup: Identifiable {
    var id: String
    var title: String
    var tasks: [Todo]
    var collapsible = false
}
struct UndoInfo {
    var token = UUID()
    var id: String
    var previous: LocalAction?
    var text: String
}

let completeReasons = ["办完了", "对方已处理", "不用做了"]
let dismissReasons = ["不是我的事", "已在别处跟进", "太琐碎", "重复了"]

private let isoFractional: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
}()
private let isoPlain = ISO8601DateFormatter()
private let localFormats: [DateFormatter] = ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm", "yyyy-MM-dd"].map {
    let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = appTimeZone; f.dateFormat = $0; return f
}
func parseDate(_ str: String?) -> Date? {
    guard let str, !str.isEmpty else { return nil }
    if let d = isoFractional.date(from: str) ?? isoPlain.date(from: str) { return d }
    // Python isoformat() writes microseconds, which ISO8601DateFormatter rejects; trim to milliseconds.
    if let dot = str.firstIndex(of: "."), let end = str[dot...].firstIndex(where: { "+-Z".contains($0) }), str.distance(from: dot, to: end) > 4 {
        let trimmed = String(str[..<str.index(dot, offsetBy: 4)]) + str[end...]
        if let d = isoFractional.date(from: trimmed) { return d }
    }
    return localFormats.lazy.compactMap { $0.date(from: str) }.first
}
func dateText(_ date: Date, format: String = "M/d HH:mm") -> String {
    let f = DateFormatter(); f.locale = Locale(identifier: "zh_CN"); f.timeZone = appTimeZone; f.dateFormat = format
    return f.string(from: date)
}
func isoNow() -> String { ISO8601DateFormatter().string(from: Date()) }

@MainActor final class TodoStore: ObservableObject {
    @Published var inbox = Inbox(schemaVersion: 1, updatedAt: nil, tasks: [], sync: nil)
    @Published var actions: [String: LocalAction] = [:]
    @Published var loadError: String?
    @Published var saveError: String?
    @Published var undo: UndoInfo?
    @Published var now = Date()
    var onChange: (() -> Void)?
    let root: URL
    private var lastInbox: Data?
    private var timer: Timer?
    private var watcher: DispatchSourceFileSystemObject?

    init(root: URL, live: Bool = true) {
        self.root = root
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let actionURL = root.appendingPathComponent("actions.json")
            if FileManager.default.fileExists(atPath: actionURL.path) {
                actions = try JSONDecoder().decode([String: LocalAction].self, from: Data(contentsOf: actionURL))
            }
        } catch { saveError = "勾选记录读取失败。请保留 actions.json，先别再勾选，交给 Codex 排查。" }
        reload()
        guard live else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = Date(); self?.reload(); self?.onChange?() }
        }
        watch()
    }
    /// The整理 job replaces inbox.json atomically, which shows up as a write on the directory.
    private func watch() {
        let fd = open(root.path, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
        source.setEventHandler { [weak self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { Task { @MainActor in self?.reload() } }
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }
    func reload() {
        let url = root.appendingPathComponent("inbox.json")
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        do {
            let data = try Data(contentsOf: url)
            guard data != lastInbox else { return }
            let value = try JSONDecoder().decode(Inbox.self, from: data)
            guard value.schemaVersion == 1 else { throw CocoaError(.fileReadCorruptFile) }
            inbox = value; lastInbox = data; loadError = nil; onChange?()
        } catch { loadError = "新清单暂时无法读取，先显示上次的内容。" }
    }

    // MARK: State (keep in step with effective_status in update_inbox.py)
    func automatic(_ todo: Todo) -> String {
        todo.category == "resolved" ? "done" : todo.category == "stale" ? "dismissed" : "active"
    }
    func status(_ todo: Todo) -> String {
        guard let a = actions[todo.id], let s = a.status else { return automatic(todo) }
        if s == "snoozed" { return (a.until ?? 0) > now.timeIntervalSince1970 ? "snoozed" : automatic(todo) }
        // A tick or untick made on the report platform after this manual action wins.
        if let changed = parseDate(todo.reportTodo?.changedAt), let acted = parseDate(a.updatedAt), changed > acted { return automatic(todo) }
        return s
    }
    func isArchived(_ todo: Todo) -> Bool { ["done", "dismissed"].contains(status(todo)) }
    var endOfToday: Date { appCalendar.date(byAdding: .day, value: 1, to: appCalendar.startOfDay(for: now))! }
    func isUrgent(_ todo: Todo) -> Bool {
        todo.priority == 0 || parseDate(todo.due).map { $0 < endOfToday } == true
    }
    func closedDate(_ todo: Todo) -> Date? {
        if let a = actions[todo.id], ["done", "dismissed"].contains(a.status ?? "") { return parseDate(a.updatedAt) }
        return parseDate(todo.closedAt ?? todo.lastSeen)
    }
    func items(_ tab: Int) -> [Todo] {
        let wanted: Set<String> = tab == 0 ? ["active"] : tab == 1 ? ["snoozed"] : ["done", "dismissed"]
        let rows = inbox.tasks.filter { wanted.contains(status($0)) }
        if tab == 2 { return rows.sorted { (closedDate($0) ?? .distantPast) > (closedDate($1) ?? .distantPast) } }
        if tab == 1 { return rows.sorted { (actions[$0.id]?.until ?? 0) < (actions[$1.id]?.until ?? 0) } }
        return rows.sorted { a, b in
            if a.priority != b.priority { return a.priority < b.priority }
            return (parseDate(a.due) ?? .distantFuture) < (parseDate(b.due) ?? .distantFuture)
        }
    }
    /// Case-insensitive search over everything a closed card shows; every space-separated word must match.
    func matches(_ todo: Todo, _ query: String) -> Bool {
        let words = query.lowercased().split(whereSeparator: \.isWhitespace)
        guard !words.isEmpty else { return true }
        let a = actions[todo.id]
        let fields = [todo.title, todo.detail, todo.resolution ?? "", a?.reason ?? "", a?.note ?? ""]
            + todo.sources.flatMap { [$0.chat, $0.quote, $0.senderName ?? "", $0.recipientName ?? ""] }
        let haystack = fields.joined(separator: "\n").lowercased()
        return words.allSatisfy { haystack.contains($0) }
    }
    /// Archived cards matching the query, grouped by the day they were closed (newest first).
    func archivedSections(_ query: String) -> [(title: String, tasks: [Todo])] {
        var sections: [(title: String, tasks: [Todo])] = []
        for todo in items(2) where matches(todo, query) {
            let label = dayLabel(closedDate(todo))
            if sections.last?.title == label { sections[sections.count - 1].tasks.append(todo) }
            else { sections.append((label, [todo])) }
        }
        return sections
    }
    func dayLabel(_ date: Date?) -> String {
        guard let date else { return "更早" }
        if appCalendar.isDate(date, inSameDayAs: now) { return "今天" }
        if let y = appCalendar.date(byAdding: .day, value: -1, to: now), appCalendar.isDate(date, inSameDayAs: y) { return "昨天" }
        return dateText(date, format: "M月d日")
    }
    func workGroups() -> [WorkGroup] {
        var today: [Todo] = [], week: [Todo] = [], waiting: [Todo] = [], confirm: [Todo] = []
        for todo in items(0) {
            let urgentAction = todo.category != "waiting" && isUrgent(todo)
            // Unconfirmed leads are kept out of the way unless they are urgent personal actions.
            if todo.isUnconfirmed && !urgentAction { confirm.append(todo) }
            // Responsibility wins over urgency: waiting never mixes with personal actions.
            else if todo.category == "waiting" { waiting.append(todo) }
            else if urgentAction { today.append(todo) }
            else { week.append(todo) }
        }
        return [WorkGroup(id: "today", title: "今天优先", tasks: today),
                WorkGroup(id: "week", title: "本周处理", tasks: week),
                WorkGroup(id: "waiting", title: "等别人回复", tasks: waiting),
                WorkGroup(id: "confirm", title: "待确认", tasks: confirm, collapsible: true)].filter { !$0.tasks.isEmpty }
    }
    var mineCount: Int { workGroups().filter { ["today", "week"].contains($0.id) }.reduce(0) { $0 + $1.tasks.count } }
    var summaryText: String {
        let groups = Dictionary(uniqueKeysWithValues: workGroups().map { ($0.id, $0.tasks.count) })
        var parts: [String] = []
        if let n = groups["today"] { parts.append("今天 \(n)") }
        if let n = groups["week"] { parts.append("本周 \(n)") }
        if let n = groups["waiting"] { parts.append("等回复 \(n)") }
        if let n = groups["confirm"] { parts.append("待确认 \(n)") }
        return parts.isEmpty ? "眼前没有待办" : parts.joined(separator: " · ")
    }
    func waitingDays(_ todo: Todo) -> Int? {
        guard todo.category == "waiting" else { return nil }
        let since = parseDate(todo.waitingSince) ?? todo.sources.compactMap { parseDate($0.time) }.min() ?? parseDate(todo.firstSeen)
        guard let since else { return nil }
        return appCalendar.dateComponents([.day], from: appCalendar.startOfDay(for: since), to: appCalendar.startOfDay(for: now)).day
    }
    func isHandedOff(_ todo: Todo) -> Bool { todo.dispatch?.threadId != nil || actions[todo.id]?.handoffAt != nil }
    /// Why an archived card is archived. Shown on the card so a past tick can always be explained.
    func closure(_ todo: Todo) -> Closure? {
        let s = status(todo)
        guard s == "done" || s == "dismissed" else { return nil }
        let when = closedDate(todo).map { dateText($0) }
        if let a = actions[todo.id], a.status == s {
            let reason = a.reason ?? "旧版勾选，未记录理由"
            return Closure(icon: s == "done" ? "checkmark.circle.fill" : "minus.circle",
                           headline: (s == "done" ? "你标记完成：" : "你忽略了：") + reason, when: when, detail: a.note)
        }
        if todo.category == "stale" { return Closure(icon: "clock.arrow.circlepath", headline: "长时间没有新进展，自动收起", when: when, detail: todo.resolution) }
        return Closure(icon: "checkmark.seal", headline: "整理时确认已完成", when: when, detail: todo.resolution)
    }

    // MARK: Actions
    @discardableResult func persist(_ next: [String: LocalAction]) -> Bool {
        // If the prior state is unreadable, do not silently overwrite completion history.
        let url = root.appendingPathComponent("actions.json")
        do {
            if FileManager.default.fileExists(atPath: url.path) {
                _ = try JSONDecoder().decode([String: LocalAction].self, from: Data(contentsOf: url))
            }
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(next).write(to: url, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            actions = next; saveError = nil; onChange?(); return true
        } catch { saveError = "这次操作没有保存，请检查本机存储后重试。"; return false }
    }
    @discardableResult func set(_ todo: Todo, _ status: String, until: Date? = nil, reason: String? = nil, note: String? = nil, undoText: String) -> Bool {
        let previous = actions[todo.id]
        var next = actions
        next[todo.id] = LocalAction(status: status, until: until?.timeIntervalSince1970, updatedAt: isoNow(),
                                    reason: reason, note: (note?.isEmpty ?? true) ? nil : note, handoffAt: previous?.handoffAt)
        guard persist(next) else { return false }
        let info = UndoInfo(id: todo.id, previous: previous, text: undoText)
        undo = info
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in
            if self?.undo?.token == info.token { self?.undo = nil }
        }
        return true
    }
    @discardableResult func complete(_ todo: Todo, reason: String, note: String) -> Bool {
        set(todo, "done", reason: reason, note: note, undoText: "已完成：\(reason)")
    }
    @discardableResult func dismiss(_ todo: Todo, reason: String, note: String) -> Bool {
        set(todo, "dismissed", reason: reason, note: note, undoText: "已忽略：\(reason)")
    }
    func restore(_ todo: Todo) { set(todo, "active", undoText: "已恢复到待办") }
    func snooze(_ todo: Todo, until: Date, label: String) { set(todo, "snoozed", until: until, undoText: "\(label) 再提醒") }
    func undoLast() {
        guard let info = undo else { return }
        var next = actions; next[info.id] = info.previous
        if persist(next) { undo = nil }
    }
    func markHandoff(_ todo: Todo) {
        var next = actions
        var entry = next[todo.id] ?? LocalAction(status: nil, until: nil, updatedAt: isoNow())
        entry.handoffAt = isoNow()
        next[todo.id] = entry
        persist(next)
    }
    var snoozeChoices: [(label: String, date: Date)] {
        let tomorrow = appCalendar.date(byAdding: .day, value: 1, to: appCalendar.startOfDay(for: now))!
        let monday = appCalendar.nextDate(after: tomorrow.addingTimeInterval(-1), matching: DateComponents(hour: 9, minute: 0, weekday: 2), matchingPolicy: .nextTime)!
        return [("1 小时后", now.addingTimeInterval(3600)),
                ("明天 09:00", tomorrow.addingTimeInterval(9 * 3600)),
                ("下周一 09:00", monday)]
    }
    func clipboardText(_ todo: Todo) -> String {
        let sources = todo.sources.map { "\($0.directionText)\n会话：\($0.chat) · \($0.time)\n\($0.quote)" }.joined(separator: "\n\n")
        return "\(todo.title)\n\(todo.detail)\n\n\(sources)"
    }
    func handoffPrompt(_ todo: Todo) -> String {
        var lines = ["请接手「别忘了」里的这件事（事项 ID：\(todo.id)）。", "", "事项：\(todo.title)"]
        if let due = parseDate(todo.due) { lines.append("期限：\(dateText(due, format: "M月d日 HH:mm"))") }
        lines += ["现状：\(todo.detail)", "", "依据："]
        for s in todo.sources.prefix(5) {
            lines.append("- \(s.directionText) · \(s.chat) · \(s.time)：\(s.quote.prefix(240))")
        }
        lines += ["", "先核对相关项目和已有 Codex 对话，避免重复做；能直接推进的就推进。发布、对外发消息、修改正式数据之前先问我。做完告诉我完成依据。"]
        return lines.joined(separator: "\n")
    }
    func handoffURL(_ todo: Todo) -> URL? { codexNewThreadURL(handoffPrompt(todo)) }

    // MARK: Daily report
    /// Everything that became done today, by the user or by整理, newest first.
    var completedToday: [Todo] {
        items(2).filter { status($0) == "done" && closedDate($0).map { appCalendar.isDate($0, inSameDayAs: now) } == true }
    }
    func reportPrompt() -> String {
        let day = dateText(now, format: "yyyy-MM-dd")
        var lines = ["请用 work-reports MCP 整理我 \(dateText(now, format: "M月d日"))（\(day)）的日报。", ""]
        let done = completedToday
        if done.isEmpty {
            lines.append("今天在「别忘了」还没有标记完成的事。请先读取今天日报的现状，再问我今天做了什么。")
        } else {
            lines.append("今天在「别忘了」完成的事：")
            for (i, t) in done.enumerated() {
                let a = actions[t.id]
                var line = "\(i + 1). \(t.displayTitle)（事项 ID：\(t.id)）"
                if let r = t.reportTodo { line += "［日报待办 \(r.id)，平台\(r.done ? "已" : "未")勾选，version \(r.version)］" }
                if let a, a.status == "done" {
                    line += " — 我选的理由：\(a.reason ?? "未记录")"
                    if let note = a.note { line += "；备注：\(note)" }
                } else if let r = t.resolution {
                    line += " — 整理时确认完成：\(r.prefix(160))"
                }
                lines.append(line)
            }
        }
        lines += ["", "要求：",
                  "- 先 get_reporting_context，再用 search_reports 查今天是否已有日报；已有就在原报告上用 save_report_draft 按新事项 ID 追加，不改动已有事项。",
                  "- 默认只写理由为“办完了”或整理确认完成、且是我本人做的工作；“对方已处理”“不用做了”的先列出来问我要不要写。",
                  "- 工时不要猜，留空让我填；项目只在 search_projects 能唯一对应时才带上，否则留空并告诉我。",
                  "- 先保存草稿，把事项和缺项给我看。我明确说“提交”后再 submit_report，并把平台确认链接给我。",
                  "- 标了［日报待办］且平台未勾选的，用 set_todo_done 同步勾选（先 get_todos 取当前 version）。"]
        return lines.joined(separator: "\n")
    }
    var reportURL: URL? { codexNewThreadURL(reportPrompt()) }

    // MARK: Schedule
    func lastExpectedRun(before date: Date) -> Date? {
        let start = appCalendar.startOfDay(for: date)
        return [-1, 0].flatMap { day in appConfig.hours.compactMap { hour in
            appCalendar.date(byAdding: .hour, value: hour, to: appCalendar.date(byAdding: .day, value: day, to: start)!)
        } }.filter { $0 <= date }.max()
    }
    var nextRun: Date? {
        let start = appCalendar.startOfDay(for: now)
        return [0, 1].flatMap { day in appConfig.hours.compactMap { hour in
            appCalendar.date(byAdding: .hour, value: hour, to: appCalendar.date(byAdding: .day, value: day, to: start)!)
        } }.filter { $0 > now }.min()
    }
    /// A run is late when its slot passed 90 minutes ago and the scan watermark is still before it.
    var staleText: String? {
        guard inbox.updatedAt != nil, let expected = lastExpectedRun(before: now.addingTimeInterval(-90 * 60)) else { return nil }
        let scanned = parseDate(inbox.sync?.scannedThrough) ?? .distantPast
        guard scanned < expected.addingTimeInterval(-10 * 60) else { return nil }
        return "\(dateText(expected)) 那次整理还没有结果，清单可能不是最新。"
    }
    var syncLine: String {
        var parts: [String] = []
        if let time = parseDate(inbox.sync?.scannedThrough) { parts.append("已读到 \(dateText(time))") } else { parts.append("等待首次整理") }
        if let next = nextRun { parts.append("下次 " + dateText(next, format: appCalendar.isDate(next, inSameDayAs: now) ? "HH:mm" : "明天 HH:mm")) }
        return parts.joined(separator: " · ")
    }
    var runURL: URL? {
        if let id = inbox.sync?.runThreadId, !id.isEmpty { return URL(string: "codex://threads/\(id)") }
        if let s = appConfig.conversationURL, !s.isEmpty { return URL(string: s) }
        return nil
    }
}

func codexNewThreadURL(_ prompt: String) -> URL? {
    var allowed = CharacterSet.alphanumerics; allowed.insert(charactersIn: "-._~")
    guard let encoded = prompt.addingPercentEncoding(withAllowedCharacters: allowed) else { return nil }
    return URL(string: "codex://threads/new?prompt=" + encoded)
}

// MARK: - UI

let accent = Color(red: 0.16, green: 0.47, blue: 0.39)
let warm = Color(red: 0.93, green: 0.62, blue: 0.22)
let overdue = Color(red: 0.80, green: 0.32, blue: 0.24)
let calm = Color(red: 0.32, green: 0.45, blue: 0.70)

@MainActor final class WindowSettings: ObservableObject {
    @Published var collapsed = false
    @Published var pinned = true
    var resize: (() -> Void)?
    var pinChanged: (() -> Void)?
    var hide: (() -> Void)?
    func toggleCollapse() { collapsed.toggle(); resize?() }
    func togglePin() { pinned.toggle(); pinChanged?() }
}

func openURL(_ url: URL?) { if let url { NSWorkspace.shared.open(url) } }
func openDingTalk() { NSWorkspace.shared.open(URL(fileURLWithPath: appConfig.dingtalkAppPath)) }

struct Burst: Equatable {
    var radius: CGFloat = 4
    var ring: CGFloat = 0.6
    var opacity: Double = 0
}

/// Ring and dots that pop out of the checkmark once per completion.
struct CompletionBurst: View {
    var trigger: Bool
    var body: some View {
        KeyframeAnimator(initialValue: Burst(), trigger: trigger) { v in
            ZStack {
                Circle().stroke(accent.opacity(0.55), lineWidth: 2).frame(width: 24, height: 24).scaleEffect(v.ring)
                ForEach(0..<8, id: \.self) { i in
                    let angle = Double(i) / 8 * 2 * .pi
                    Circle().fill(i.isMultiple(of: 2) ? accent : warm).frame(width: 4, height: 4)
                        .offset(x: cos(angle) * v.radius, y: sin(angle) * v.radius)
                }
            }.opacity(v.opacity)
        } keyframes: { _ in
            KeyframeTrack(\.opacity) { LinearKeyframe(1, duration: 0.02); LinearKeyframe(1, duration: 0.25); LinearKeyframe(0, duration: 0.35) }
            KeyframeTrack(\.ring) { LinearKeyframe(0.6, duration: 0.02); SpringKeyframe(2.0, duration: 0.6, spring: .smooth) }
            KeyframeTrack(\.radius) { LinearKeyframe(4, duration: 0.02); SpringKeyframe(21, duration: 0.6, spring: .snappy) }
        }
        .allowsHitTesting(false)
    }
}

enum RowMode { case normal, completing, dismissing }

struct TodoRow: View {
    @ObservedObject var store: TodoStore
    var todo: Todo
    @State private var expanded = false
    @State private var mode = RowMode.normal
    @State private var note = ""
    @State private var celebrating = false
    @State private var finishedReason: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var status: String { store.status(todo) }
    var archived: Bool { store.isArchived(todo) }
    var dueText: String? {
        guard let d = parseDate(todo.due), !archived else { return nil }
        if d < store.now { return "已到期 · " + dateText(d) }
        let today = appCalendar.isDate(d, inSameDayAs: store.now)
        return (today ? "今天 " : "") + dateText(d, format: today ? "HH:mm" : "M/d HH:mm")
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .top, spacing: 10) {
                checkButton
                VStack(alignment: .leading, spacing: 5) {
                    Text(todo.displayTitle).font(.system(size: 14, weight: .semibold)).lineSpacing(3)
                        .strikethrough(celebrating, color: .secondary)
                        .foregroundStyle(archived || celebrating ? Color.secondary : Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let finishedReason, celebrating {
                        Label("已完成 · \(finishedReason)", systemImage: "checkmark").font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(accent).transition(.opacity.combined(with: .move(edge: .top)))
                    } else { badges }
                }
                Spacer(minLength: 0)
                moreMenu
            }
            if mode == .completing {
                reasonPanel(title: "为什么完成？以后回看时有据可查", reasons: completeReasons) { finish($0) }
            } else if mode == .dismissing {
                reasonPanel(title: "为什么忽略？整理时会参考，减少同类误判", reasons: dismissReasons) { dismiss($0) }
            }
            Text(todo.detail).font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(3)
                .lineLimit(archived && !expanded ? 2 : nil)
                .fixedSize(horizontal: false, vertical: true).padding(.leading, 38)
            if let c = store.closure(todo) { closureBox(c) }
            if status == "snoozed", let until = store.actions[todo.id]?.until {
                Label("\(dateText(Date(timeIntervalSince1970: until))) 回到待办", systemImage: "clock")
                    .font(.system(size: 10)).foregroundStyle(accent).padding(.leading, 38)
            }
            evidence
        }
        .padding(13)
        .background(celebrating ? accent.opacity(0.09) : Color(nsColor: .controlBackgroundColor).opacity(0.82), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(celebrating ? accent.opacity(0.35) : Color.primary.opacity(0.055), lineWidth: 1))
    }

    var checkButton: some View {
        Button {
            if archived { withAnimation(.easeInOut(duration: 0.3)) { store.restore(todo) } }
            else { withAnimation(.snappy(duration: 0.25)) { mode = mode == .completing ? .normal : .completing; note = "" } }
        } label: {
            ZStack {
                if celebrating { CompletionBurst(trigger: celebrating) }
                Image(systemName: celebrating || status == "done" ? "checkmark.circle.fill" : status == "dismissed" ? "minus.circle" : mode == .completing ? "circle.dotted" : "circle")
                    .font(.system(size: 22, weight: .regular))
                    .foregroundStyle(celebrating || status == "done" || mode == .completing ? accent : Color.secondary)
                    .contentTransition(.symbolEffect(.replace))
                    .scaleEffect(celebrating ? 1.15 : 1)
            }.frame(width: 28, height: 28)
        }
        .buttonStyle(.plain)
        .help(archived ? "恢复到待办" : "完成：选一个理由，仅更新本机清单")
        .accessibilityLabel((archived ? "恢复：" : "完成：") + todo.displayTitle)
    }

    @ViewBuilder var badges: some View {
        HStack(spacing: 6) {
            if status == "dismissed" { badge(todo.category == "stale" && store.actions[todo.id]?.status == nil ? "自动收起" : "已忽略", .secondary) }
            if !archived {
                if todo.category == "waiting" {
                    let days = store.waitingDays(todo) ?? 0
                    badge(days >= 1 ? "等回复 · \(days) 天" : "等回复", days >= 5 ? overdue : .orange)
                }
                if todo.isUnconfirmed { badge("待确认", calm) }
                if todo.priority == 0 && todo.category != "waiting" { badge("优先", accent) }
                if store.isHandedOff(todo) { badge("Codex 跟进", calm) }
                if todo.reportTodo != nil { badge("日报", accent) }
            }
            if let dueText {
                Text(dueText).font(.system(size: 10, weight: .medium))
                    .foregroundStyle((parseDate(todo.due) ?? .distantFuture) < store.now ? .orange : .secondary).lineLimit(1)
            }
            if let s = todo.sources.first {
                Text(s.chat).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1).layoutPriority(-1)
            }
        }
    }

    var moreMenu: some View {
        Menu {
            if !archived {
                if status != "snoozed" {
                    Menu("稍后提醒") {
                        ForEach(store.snoozeChoices, id: \.label) { choice in
                            Button(choice.label) { withAnimation(.easeInOut(duration: 0.3)) { store.snooze(todo, until: choice.date, label: choice.label) } }
                        }
                    }
                }
                Button("忽略这条…") { withAnimation(.snappy(duration: 0.25)) { mode = .dismissing; note = "" } }
                Divider()
                Button(store.isHandedOff(todo) ? "再交给 Codex…" : "交给 Codex 处理…") {
                    openURL(store.handoffURL(todo)); store.markHandoff(todo)
                }
            }
            if archived || status == "snoozed" {
                Button("恢复到待办") { withAnimation(.easeInOut(duration: 0.3)) { store.restore(todo) } }
            }
            Divider()
            Button("复制待办与来源") { copyToPasteboard() }
            Button("打开钉钉") { openDingTalk() }
        } label: { Image(systemName: "ellipsis").frame(width: 20, height: 24) }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .help("更多操作").accessibilityLabel("更多操作：" + todo.displayTitle)
    }

    func reasonPanel(title: String, reasons: [String], choose: @escaping (String) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 78), spacing: 6)], alignment: .leading, spacing: 6) {
                ForEach(reasons, id: \.self) { reason in
                    Button { choose(reason) } label: {
                        Text(reason).font(.system(size: 11, weight: .medium)).frame(maxWidth: .infinity).padding(.vertical, 6)
                            .background(accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 7))
                            .foregroundStyle(accent).contentShape(Rectangle())
                    }.buttonStyle(.plain).accessibilityLabel("\(reason)：\(todo.displayTitle)")
                }
            }
            HStack(spacing: 8) {
                TextField("补充一句（可选），回车＝\(reasons[0])", text: $note)
                    .textFieldStyle(.roundedBorder).font(.system(size: 11))
                    .onSubmit { choose(reasons[0]) }
                Button("取消") { withAnimation(.snappy(duration: 0.2)) { mode = .normal } }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(10).background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 10))
        .padding(.leading, 38)
        .transition(.opacity.combined(with: .move(edge: .top)))
    }

    func closureBox(_ c: Closure) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: c.icon).font(.system(size: 11)).foregroundStyle(accent).padding(.top, 1)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(c.headline).font(.system(size: 11, weight: .medium))
                    Spacer(minLength: 0)
                    if let when = c.when { Text(when).font(.system(size: 10)).foregroundStyle(.tertiary).fixedSize() }
                }
                if let d = c.detail, !d.isEmpty {
                    Text(d).font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(2)
                        .lineLimit(expanded ? nil : 3).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(9).background(accent.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        .padding(.leading, 38)
    }

    var evidence: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(.snappy(duration: 0.2)) { expanded.toggle() } } label: {
                HStack(spacing: 7) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right").font(.system(size: 10, weight: .semibold))
                    Text(expanded ? "收起聊天依据" : "聊天依据")
                    Text("\(todo.sources.count) 条").foregroundStyle(.tertiary)
                    Spacer(minLength: 0)
                }
                .font(.system(size: 11, weight: .medium)).foregroundStyle(accent)
                .frame(maxWidth: .infinity, minHeight: 28, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel((expanded ? "收起聊天依据：" : "展开聊天依据：") + todo.displayTitle)
            .accessibilityValue(expanded ? "已展开" : "已收起")
            if expanded {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(todo.sources.enumerated()), id: \.offset) { _, s in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(s.directionText).font(.system(size: 11, weight: .semibold)).foregroundStyle(accent)
                            Text("会话：\(s.chat) · \(s.time)").font(.system(size: 10)).foregroundStyle(.secondary)
                            Text(s.quote).font(.system(size: 11)).lineSpacing(3).textSelection(.enabled)
                        }
                    }
                    HStack {
                        Button("复制线索") { copyToPasteboard() }
                        Button("打开钉钉") { openDingTalk() }
                    }.font(.system(size: 11)).buttonStyle(.bordered)
                }.padding(.top, 4)
            }
        }.padding(.leading, 38)
    }

    func finish(_ reason: String) {
        let text = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if reduceMotion {
            withAnimation(.easeOut(duration: 0.2)) { _ = store.complete(todo, reason: reason, note: text) }
            return
        }
        finishedReason = reason
        withAnimation(.spring(response: 0.32, dampingFraction: 0.6)) { celebrating = true; mode = .normal }
        // Let the checkmark land before the card leaves the list.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.85) {
            withAnimation(.easeInOut(duration: 0.35)) {
                if !store.complete(todo, reason: reason, note: text) { celebrating = false }
            }
        }
    }
    func dismiss(_ reason: String) {
        let text = note.trimmingCharacters(in: .whitespacesAndNewlines)
        withAnimation(.easeInOut(duration: 0.3)) { mode = .normal; _ = store.dismiss(todo, reason: reason, note: text) }
    }
    func copyToPasteboard() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(store.clipboardText(todo), forType: .string)
    }
    func badge(_ text: String, _ color: Color) -> some View {
        Text(text).font(.system(size: 9, weight: .semibold)).foregroundStyle(color).lineLimit(1).fixedSize()
            .padding(.horizontal, 5).padding(.vertical, 2).background(color.opacity(0.10), in: Capsule())
    }
}

final class WindowDragView: NSView {
    override var mouseDownCanMoveWindow: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
}

struct WindowDragArea: NSViewRepresentable {
    func makeNSView(context: Context) -> WindowDragView {
        let view = WindowDragView()
        view.toolTip = "按住这里拖动窗口"
        return view
    }
    func updateNSView(_ nsView: WindowDragView, context: Context) {}
}

let rowTransition = AnyTransition.asymmetric(
    insertion: .opacity.combined(with: .move(edge: .top)),
    removal: .opacity.combined(with: .scale(scale: 0.94, anchor: .top)).combined(with: .offset(x: 36)))

struct MainView: View {
    @ObservedObject var store: TodoStore
    @ObservedObject var settings: WindowSettings
    @State private var tab = 0
    @State private var showSync = false
    @State private var query = ""
    @FocusState private var searchFocused: Bool
    @AppStorage("confirmGroupExpanded") private var confirmExpanded = false
    var body: some View {
        VStack(spacing: 0) {
            header
            if !settings.collapsed {
                HStack(spacing: 5) {
                    tabButton("待办", 0, count: store.items(0).count)
                    tabButton("稍后", 1, count: store.items(1).count)
                    tabButton("已收起", 2, count: store.items(2).count)
                }.padding(.horizontal, 17).padding(.bottom, 12)
                .background {
                    // ⌘F jumps to the archive search from any tab.
                    Button("") { tab = 2; DispatchQueue.main.async { searchFocused = true } }
                        .keyboardShortcut("f", modifiers: .command).opacity(0).allowsHitTesting(false)
                }
                if tab == 2 && !store.items(2).isEmpty { searchField }
                if loadedConfig == nil { alertBar("缺少 config.json，正在使用默认设置。请交给 Codex 重新构建。") }
                if let error = store.saveError ?? store.loadError { alertBar(error) }
                else if store.inbox.sync?.state == "error" || store.inbox.sync?.state == "partial" {
                    alertBar(store.inbox.sync?.message ?? "这次读取不完整，已保留旧待办。")
                } else if let stale = store.staleText { alertBar(stale) }
                list
                Divider().opacity(0.5)
                if let undo = store.undo {
                    HStack {
                        Text(undo.text).font(.system(size: 11)).foregroundStyle(accent).lineLimit(1)
                        Spacer()
                        Button("撤销") { withAnimation(.easeInOut(duration: 0.3)) { store.undoLast() } }
                            .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(accent)
                            .keyboardShortcut("z", modifiers: .command)
                    }.padding(.horizontal, 17).padding(.top, 9).transition(.opacity)
                }
                footer
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.primary.opacity(0.09), lineWidth: 1))
        .tint(accent)
        .animation(.easeInOut(duration: 0.2), value: store.undo?.token)
    }

    var header: some View {
        HStack(spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: "checklist").font(.system(size: 20, weight: .medium)).foregroundStyle(accent)
                    .frame(width: 37, height: 37).background(accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 11))
                VStack(alignment: .leading, spacing: 3) {
                    Text(appConfig.appName).font(.system(size: 19, weight: .bold))
                    Text(store.summaryText).font(.system(size: 10)).foregroundStyle(.secondary)
                        .contentTransition(.numericText()).lineLimit(1)
                }
                Spacer(minLength: 0)
            }.overlay(WindowDragArea())
            iconButton(settings.pinned ? "pin.fill" : "pin", label: settings.pinned ? "取消置顶" : "窗口置顶") { settings.togglePin() }
            iconButton(settings.collapsed ? "chevron.down" : "chevron.up", label: settings.collapsed ? "展开清单" : "收起清单") { settings.toggleCollapse() }
            iconButton("xmark", label: "隐藏到菜单栏") { settings.hide?() }
        }.padding(.horizontal, 17).padding(.top, 16).padding(.bottom, 14)
    }

    var list: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 9) {
                if store.items(tab).isEmpty { emptyState }
                if tab == 0 {
                    ForEach(store.workGroups()) { group in
                        groupHeader(group)
                        if !group.collapsible || confirmExpanded {
                            ForEach(group.tasks) { todo in TodoRow(store: store, todo: todo).transition(rowTransition) }
                        }
                    }
                } else if tab == 2 {
                    let sections = store.archivedSections(query)
                    if sections.isEmpty && !store.items(2).isEmpty { noResults }
                    ForEach(sections, id: \.title) { section in
                        HStack(spacing: 6) {
                            Text(section.title).font(.system(size: 12, weight: .semibold))
                            Text("\(section.tasks.count)").font(.system(size: 10)).foregroundStyle(.secondary)
                            Spacer()
                        }.padding(.horizontal, 4).padding(.top, 8)
                        ForEach(section.tasks) { todo in TodoRow(store: store, todo: todo).transition(rowTransition) }
                    }
                } else {
                    ForEach(store.items(tab)) { todo in TodoRow(store: store, todo: todo).transition(rowTransition) }
                }
            }.padding(.horizontal, 14).padding(.bottom, 14)
        }
    }

    @ViewBuilder func groupHeader(_ group: WorkGroup) -> some View {
        let content = HStack(spacing: 6) {
            if group.collapsible {
                Image(systemName: confirmExpanded ? "chevron.down" : "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
            }
            Text(group.title).font(.system(size: 12, weight: .semibold))
            Text("\(group.tasks.count)").font(.system(size: 10)).foregroundStyle(.secondary).contentTransition(.numericText())
            if group.collapsible && !confirmExpanded {
                Text("线索不确定，有空再看").font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Spacer()
        }.padding(.horizontal, 4).padding(.top, 8).frame(minHeight: 24).contentShape(Rectangle())
        if group.collapsible {
            Button { withAnimation(.snappy(duration: 0.25)) { confirmExpanded.toggle() } } label: { content }
                .buttonStyle(.plain).accessibilityLabel(confirmExpanded ? "收起待确认" : "展开待确认")
        } else { content }
    }

    var searchField: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("搜索：标题、理由、备注、聊天原文", text: $query)
                .textFieldStyle(.plain).focused($searchFocused)
                .onExitCommand { query = ""; searchFocused = false }
            if !query.isEmpty {
                Text("\(store.archivedSections(query).reduce(0) { $0 + $1.tasks.count }) 件").foregroundStyle(.secondary).monospacedDigit()
                Button { query = "" } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.plain).foregroundStyle(.tertiary).accessibilityLabel("清空搜索")
            }
        }
        .font(.system(size: 12)).padding(.horizontal, 10).padding(.vertical, 7)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
        .padding(.horizontal, 14).padding(.bottom, 8)
    }

    var noResults: some View {
        VStack(spacing: 8) {
            Image(systemName: "magnifyingglass").font(.system(size: 26, weight: .light)).foregroundStyle(accent.opacity(0.7))
            Text("没有找到“\(query)”").font(.system(size: 13, weight: .medium))
            Button("清空搜索") { query = "" }.font(.system(size: 11)).buttonStyle(.bordered)
        }.frame(maxWidth: .infinity).padding(.vertical, 40)
    }

    var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: tab == 0 ? "checkmark.seal" : "tray").font(.system(size: 32, weight: .light)).foregroundStyle(accent.opacity(0.7))
            Text(tab == 0 ? "眼前的事都处理好了" : tab == 1 ? "没有暂缓的事" : "完成和忽略的事会留在这里").font(.system(size: 13, weight: .medium))
            Text(tab == 0 ? "下一次整理后，新待办会自动出现。" : "所有操作都可以恢复。").font(.system(size: 11)).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity).padding(.vertical, 55)
    }

    var footer: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 3) {
                Text(store.inbox.sync?.schedule ?? appConfig.schedule).font(.system(size: 10, weight: .medium))
                Text(store.syncLine).font(.system(size: 9)).foregroundStyle(.secondary)
            }
            Spacer()
            Button { openURL(store.reportURL) } label: {
                HStack(spacing: 4) {
                    Image(systemName: "doc.text")
                    Text(store.completedToday.isEmpty ? "整理日报" : "整理日报 · \(store.completedToday.count)").contentTransition(.numericText())
                }.font(.system(size: 11, weight: .medium)).foregroundStyle(accent)
                    .padding(.horizontal, 8).padding(.vertical, 5).background(accent.opacity(0.10), in: Capsule())
            }
            .buttonStyle(.plain).help("把今天完成的事带进新的 Codex 对话，整理成日报草稿")
            .accessibilityLabel("整理今天日报，今天完成 \(store.completedToday.count) 件")
            Button { showSync.toggle() } label: { Image(systemName: "info.circle").font(.system(size: 14)) }
                .buttonStyle(.plain).foregroundStyle(.secondary).help("上次整理的范围和结果").accessibilityLabel("上次整理详情")
                .popover(isPresented: $showSync, arrowEdge: .top) { syncDetail }
        }.padding(.horizontal, 17).padding(.vertical, 12)
    }

    var syncDetail: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("上次整理").font(.system(size: 12, weight: .semibold))
            if let message = store.inbox.sync?.message, !message.isEmpty { Text(message).font(.system(size: 11)) }
            if let scope = store.inbox.sync?.scope { Text(scope).font(.system(size: 10)).foregroundStyle(.secondary) }
            if let mode = store.inbox.sync?.mode { Text(mode == "full" ? "本次为完整回扫" : "本次为增量整理").font(.system(size: 10)).foregroundStyle(.secondary) }
            HStack {
                if store.runURL != nil { Button("打开整理记录") { openURL(store.runURL) } }
                if let dir = appConfig.projectDirectory { Button("打开项目目录") { openURL(URL(fileURLWithPath: dir)) } }
            }.font(.system(size: 11)).buttonStyle(.bordered)
        }
        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        .padding(14).frame(width: 300, alignment: .leading)
    }

    func tabButton(_ text: String, _ value: Int, count: Int) -> some View {
        Button { withAnimation(.snappy(duration: 0.2)) { tab = value } } label: {
            HStack(spacing: 5) { Text(text); Text("\(count)").monospacedDigit().opacity(0.7).contentTransition(.numericText()) }
                .font(.system(size: 11, weight: tab == value ? .semibold : .regular))
                .frame(maxWidth: .infinity).padding(.vertical, 7)
                .background(tab == value ? accent.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
                .foregroundStyle(tab == value ? accent : Color.secondary)
                .contentShape(Rectangle())
        }.buttonStyle(.plain).keyboardShortcut(KeyEquivalent(Character(String(value + 1))), modifiers: .command)
    }
    func iconButton(_ name: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: name).font(.system(size: 11, weight: .medium)).frame(width: 22, height: 25).contentShape(Rectangle()) }
            .buttonStyle(.plain).foregroundStyle(.secondary).help(label).accessibilityLabel(label)
    }
    func alertBar(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "exclamationmark.circle")
            Text(text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }.font(.system(size: 10)).foregroundStyle(.orange).padding(10)
            .background(Color.orange.opacity(0.07)).padding(.horizontal, 14).padding(.bottom, 8)
    }
}

/// Local reminder 15 minutes before a personal action is due. Waiting items never notify.
@MainActor final class DueNotifier {
    let center = UNUserNotificationCenter.current()
    var authorized = false
    var signature = ""
    func start(_ store: TodoStore) {
        center.requestAuthorization(options: [.alert, .sound]) { ok, _ in
            Task { @MainActor in self.authorized = ok; self.signature = ""; self.reschedule(store) }
        }
    }
    func reschedule(_ store: TodoStore) {
        guard authorized else { return }
        let horizon = store.now.addingTimeInterval(7 * 86400)
        let due = store.items(0).compactMap { t -> (Todo, Date)? in
            guard t.category != "waiting", let d = parseDate(t.due) else { return nil }
            let fire = d.addingTimeInterval(-15 * 60)
            return fire > store.now && fire < horizon ? (t, d) : nil
        }
        let next = due.map { "\($0.0.id)@\($0.1.timeIntervalSince1970)" }.joined(separator: ",")
        guard next != signature else { return }
        signature = next
        center.removeAllPendingNotificationRequests()
        for (todo, date) in due {
            let content = UNMutableNotificationContent()
            content.title = "15 分钟后到期"
            content.body = todo.displayTitle
            content.sound = .default
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: max(1, date.addingTimeInterval(-15 * 60).timeIntervalSinceNow), repeats: false)
            center.add(UNNotificationRequest(identifier: "due-\(todo.id)", content: content, trigger: trigger))
        }
    }
}

final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}
@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, UNUserNotificationCenterDelegate {
    var panel: FloatingPanel!
    var statusItem: NSStatusItem!
    var store: TodoStore!
    var settings = WindowSettings()
    var notifier = DueNotifier()
    var testMode = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        testMode = args.contains("--test-mode")
        let root: URL
        if let i = args.firstIndex(of: "--data-dir"), i + 1 < args.count { root = URL(fileURLWithPath: args[i + 1]) }
        else { root = URL(fileURLWithPath: (appConfig.dataDirectory as NSString).expandingTildeInPath) }
        if !testMode, let bundleID = Bundle.main.bundleIdentifier {
            let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
            if let other = others.first { other.activate(); NSApp.terminate(nil); return }
        }
        NSApp.setActivationPolicy(.accessory)
        store = TodoStore(root: root)
        let frame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        panel = FloatingPanel(contentRect: NSRect(x: frame.maxX - 400, y: frame.maxY - 660, width: 374, height: 620),
                              styleMask: [.borderless, .nonactivatingPanel, .resizable], backing: .buffered, defer: false)
        panel.title = appConfig.appName; panel.isReleasedWhenClosed = false; panel.hidesOnDeactivate = false
        panel.level = .floating; panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true; panel.backgroundColor = .clear; panel.isOpaque = false; panel.hasShadow = true
        panel.minSize = NSSize(width: 340, height: 420); panel.maxSize = NSSize(width: 550, height: 1000)
        panel.delegate = self
        if !testMode { panel.setFrameAutosaveName("DingTalkTodoWindow") }
        panel.contentView = NSHostingView(rootView: MainView(store: store, settings: settings))
        settings.resize = { [weak self] in self?.resize() }
        settings.pinChanged = { [weak self] in guard let self else { return }; self.panel.level = self.settings.pinned ? .floating : .normal }
        settings.hide = { [weak self] in self?.panel.orderOut(nil) }
        store.onChange = { [weak self] in self?.updateStatus() }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.target = self; statusItem.button?.action = #selector(toggle)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        if !testMode { UNUserNotificationCenter.current().delegate = self; notifier.start(store) }
        updateStatus(); panel.orderFrontRegardless()
    }
    func updateStatus() {
        statusItem?.button?.title = "✓ \(store.mineCount)"
        statusItem?.button?.toolTip = "\(appConfig.appName) · \(store.summaryText)\n点击显示或隐藏，右键更多操作"
        if !testMode { notifier.reschedule(store) }
    }
    @objc func toggle() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "显示待办", action: #selector(show), keyEquivalent: "").target = self
            menu.addItem(withTitle: "窗口回到右上角", action: #selector(resetPosition), keyEquivalent: "").target = self
            if appConfig.projectDirectory != nil { menu.addItem(withTitle: "打开项目目录", action: #selector(openProject), keyEquivalent: "").target = self }
            menu.addItem(.separator())
            menu.addItem(withTitle: "退出\(appConfig.appName)", action: #selector(quit), keyEquivalent: "q").target = self
            statusItem.menu = menu; statusItem.button?.performClick(nil); statusItem.menu = nil
        } else { panel.isVisible ? panel.orderOut(nil) : panel.orderFrontRegardless() }
    }
    @objc func show() { panel.orderFrontRegardless() }
    @objc func openProject() { if let dir = appConfig.projectDirectory { openURL(URL(fileURLWithPath: dir)) } }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func resetPosition() {
        if let f = NSScreen.main?.visibleFrame { panel.setFrameOrigin(NSPoint(x: f.maxX - panel.frame.width - 26, y: f.maxY - panel.frame.height - 30)) }
        show()
    }
    func resize() {
        let old = panel.frame
        panel.minSize = NSSize(width: 340, height: settings.collapsed ? 82 : 420)
        panel.maxSize = NSSize(width: 550, height: settings.collapsed ? 82 : 1000)
        let height: CGFloat = settings.collapsed ? 82 : 620
        panel.setFrame(NSRect(x: old.minX, y: old.maxY - height, width: old.width, height: height), display: true, animate: false)
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { show(); return true }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        await MainActor.run { self.show() }
    }
}
struct TodoApplication {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
