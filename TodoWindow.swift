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
    /// Used to tell who a waiting item is waiting on (compared with Source.senderId).
    var selfOpenDingTalkId: String?
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
    /// When the one-time “waited 3 days” notification was sent.
    var nudgedAt: String?
    /// The user's own deadline; wins over `due` from整理, which never overwrites it.
    var dueOverride: String?
    var isEmpty: Bool { status == nil && handoffAt == nil && nudgedAt == nil && dueOverride == nil }
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
    /// Shown next to a collapsed group's count.
    var hint: String?
}
struct RunRecord {
    var when: Date?
    var added: [Todo]
    var updated: [Todo]
    var closed: [Todo]
    var isEmpty: Bool { added.isEmpty && updated.isEmpty && closed.isEmpty }
}
struct UndoInfo {
    var token = UUID()
    var id: String
    var previous: LocalAction?
    var text: String
}

/// How much of each card to show; remembered per tab.
enum CardLength: Int, CaseIterable, Identifiable {
    case compact, standard, full
    var id: Int { rawValue }
    var label: String { ["精简", "标准", "完整"][rawValue] }
    var hint: String { ["只看标题和一行状态", "多两行说明和依据入口", "全部展开"][rawValue] }
}

let completeReasons = ["办完了", "对方已处理", "不用做了"]
let dismissReasons = ["不是我的事", "已在别处跟进", "太琐碎", "重复了"]

/// Each kind of system notification can be turned off on its own in ⓘ; all are on by default.
enum NotifyKind: String, CaseIterable, Identifiable {
    case due, snooze, summary, nudge
    var id: String { rawValue }
    var label: String {
        switch self {
        case .due: return "到期前 15 分钟"
        case .snooze: return "稍后提醒到点"
        case .summary: return "早上摘要"
        case .nudge: return "等回复满 3 天"
        }
    }
}
/// A notification the window wants delivered. `fireAt == nil` means now.
struct PlannedNotice: Equatable {
    var identifier: String
    var kind: NotifyKind
    var todoId: String?
    var fireAt: Date?
    var title: String
    var body: String
    var category: String?
}
/// Notification category and action identifiers shared by the planner and the delegate.
enum NoticeAction {
    static let snoozeCategory = "snooze"
    static let complete = "complete"
    static let snoozeHour = "snooze-1h"
}
/// Ask the window to show one card, e.g. after a notification click. `complete` also opens the reason panel.
struct FocusRequest: Equatable {
    var token = UUID()
    var id: String
    var complete = false
}

struct ChangelogEntry: Identifiable, Equatable {
    var title: String
    var items: [String]
    var id: String { title }
}
/// `## 标题` starts an entry, `- ` lines are its items, indented lines continue the previous item.
func parseChangelog(_ text: String) -> [ChangelogEntry] {
    var entries: [ChangelogEntry] = []
    for raw in text.components(separatedBy: .newlines) {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.hasPrefix("## ") {
            entries.append(ChangelogEntry(title: String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces), items: []))
        } else if entries.isEmpty || line.isEmpty {
            continue
        } else if line.hasPrefix("- ") || line.hasPrefix("* ") {
            entries[entries.count - 1].items.append(String(line.dropFirst(2)))
        } else if raw.hasPrefix(" "), !entries[entries.count - 1].items.isEmpty {
            entries[entries.count - 1].items[entries[entries.count - 1].items.count - 1] += line
        }
    }
    return entries
}

func clip(_ text: String, _ limit: Int) -> String {
    guard text.count > limit else { return text }
    return limit <= 1 ? String(text.prefix(max(0, limit))) : String(text.prefix(limit - 1)) + "…"
}
/// Best-effort removal of passwords, keys and tokens from text the user is about to paste elsewhere.
func redactSecrets(_ text: String) -> String {
    let rules: [(String, String)] = [
        (#"(?i)(密码|口令|验证码|password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|cookie)(\s*[:：=是为]?\s*)[A-Za-z0-9!@#$%^&*._+=/\-]{4,}"#, "$1$2***"),
        (#"(https?://[^\s?？]+)\?\S+"#, "$1"),
        (#"[A-Za-z0-9_\-]{24,}"#, "***"),
    ]
    var out = text
    for (pattern, template) in rules {
        guard let re = try? NSRegularExpression(pattern: pattern) else { continue }
        out = re.stringByReplacingMatches(in: out, range: NSRange(out.startIndex..., in: out), withTemplate: template)
    }
    return out
}

private let isoFractional: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
}()
private let isoPlain = ISO8601DateFormatter()
private let localFormats: [DateFormatter] = ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm", "yyyy-MM-dd"].map {
    let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = appTimeZone; f.dateFormat = $0; return f
}
// Parsing is the hot path of every redraw, so each distinct string is parsed once. All callers run on the main thread.
private var parsedDates: [String: Date?] = [:]
func parseDate(_ str: String?) -> Date? {
    guard let str, !str.isEmpty else { return nil }
    if let hit = parsedDates[str] { return hit }
    let value = parseDateUncached(str)
    parsedDates[str] = value
    return value
}
private func parseDateUncached(_ str: String) -> Date? {
    guard str.contains("T") else { return localFormats.lazy.compactMap { $0.date(from: str) }.first }
    // Python isoformat() writes microseconds, which ISO8601DateFormatter rejects; trim to milliseconds first.
    if let dot = str.firstIndex(of: "."), let end = str[dot...].firstIndex(where: { "+-Z".contains($0) }), str.distance(from: dot, to: end) > 4 {
        return isoFractional.date(from: String(str[..<str.index(dot, offsetBy: 4)]) + str[end...])
    }
    return isoPlain.date(from: str) ?? isoFractional.date(from: str)
}
private var formatters: [String: DateFormatter] = [:]
func dateText(_ date: Date, format: String = "M/d HH:mm") -> String {
    if let f = formatters[format] { return f.string(from: date) }
    let f = DateFormatter(); f.locale = Locale(identifier: "zh_CN"); f.timeZone = appTimeZone; f.dateFormat = format
    formatters[format] = f
    return f.string(from: date)
}
func isoNow() -> String { isoPlain.string(from: Date()) }
/// Readable timestamp in the app's time zone, e.g. 2026-10-24T18:00:00+08:00.
private let isoLocal: ISO8601DateFormatter = { let f = ISO8601DateFormatter(); f.timeZone = appTimeZone; return f }()
func isoLocalText(_ date: Date) -> String { isoLocal.string(from: date) }

@MainActor final class TodoStore: ObservableObject {
    @Published var inbox = Inbox(schemaVersion: 1, updatedAt: nil, tasks: [], sync: nil) { didSet { derived = nil } }
    @Published var actions: [String: LocalAction] = [:] { didSet { derived = nil } }
    @Published var loadError: String?
    @Published var saveError: String?
    @Published var undo: UndoInfo?
    @Published var now = Date() { didSet { derived = nil } }
    /// Items first merged after this moment carry a “新” badge until the user looks at the window.
    @Published var seenThrough = Date.distantPast
    var persistSeen: ((Date) -> Void)?
    /// Notification kinds the user turned off in ⓘ.
    @Published var muted: Set<NotifyKind> = []
    var persistMuted: ((Set<NotifyKind>) -> Void)?
    /// Day (yyyy-MM-dd) the morning summary was last handled; at most once a day.
    var summaryShownDay: String?
    var persistSummaryDay: ((String) -> Void)?
    /// Card the window should scroll to and highlight. The two tokens record who already handled it.
    @Published var focus: FocusRequest?
    var scrolledFocus: UUID?
    var highlightedFocus: UUID?
    var onChange: (() -> Void)?
    /// Everything a redraw needs, computed once per change of inbox, actions or clock.
    private struct Derived {
        var status: [String: String] = [:]
        var closed: [String: Date] = [:]
        var items: [[Todo]] = [[], [], []]
        var groups: [WorkGroup] = []
        var haystack: [String: String] = [:]
        var sections: [String: [(title: String, tasks: [Todo])]] = [:]
    }
    private var derived: Derived?
    private var cache: Derived {
        get { if let d = derived { return d }; let d = derive(); derived = d; return d }
        set { derived = newValue }
    }
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
    func status(_ todo: Todo) -> String { cache.status[todo.id] ?? computeStatus(todo) }
    private func computeStatus(_ todo: Todo) -> String {
        guard let a = actions[todo.id], let s = a.status else { return automatic(todo) }
        if s == "snoozed" { return (a.until ?? 0) > now.timeIntervalSince1970 ? "snoozed" : automatic(todo) }
        // A tick or untick made on the report platform after this manual action wins.
        if let changed = parseDate(todo.reportTodo?.changedAt), let acted = parseDate(a.updatedAt), changed > acted { return automatic(todo) }
        return s
    }
    func isArchived(_ todo: Todo) -> Bool { ["done", "dismissed"].contains(status(todo)) }
    /// The deadline everything uses: the user's own override first, then整理's `due`.
    func due(_ todo: Todo) -> Date? { parseDate(actions[todo.id]?.dueOverride) ?? parseDate(todo.due) }
    func hasDueOverride(_ todo: Todo) -> Bool { parseDate(actions[todo.id]?.dueOverride) != nil }
    var endOfToday: Date { appCalendar.date(byAdding: .day, value: 1, to: appCalendar.startOfDay(for: now))! }
    func isUrgent(_ todo: Todo) -> Bool {
        todo.priority == 0 || due(todo).map { $0 < endOfToday } == true
    }
    func isOverdue(_ todo: Todo) -> Bool { due(todo).map { $0 < now } == true }
    func closedDate(_ todo: Todo) -> Date? { cache.closed[todo.id] ?? computeClosedDate(todo) }
    private func computeClosedDate(_ todo: Todo) -> Date? {
        if let a = actions[todo.id], ["done", "dismissed"].contains(a.status ?? "") { return parseDate(a.updatedAt) }
        return parseDate(todo.closedAt ?? todo.lastSeen)
    }
    func items(_ tab: Int) -> [Todo] { cache.items[tab] }
    private func derive() -> Derived {
        var d = Derived()
        for t in inbox.tasks {
            d.status[t.id] = computeStatus(t)
            if let c = computeClosedDate(t) { d.closed[t.id] = c }
        }
        let pick = { (wanted: Set<String>) in self.inbox.tasks.filter { wanted.contains(d.status[$0.id] ?? "") } }
        d.items[0] = pick(["active"]).sorted { a, b in
            if a.priority != b.priority { return a.priority < b.priority }
            return (self.due(a) ?? .distantFuture) < (self.due(b) ?? .distantFuture)
        }
        d.items[1] = pick(["snoozed"]).sorted { (actions[$0.id]?.until ?? 0) < (actions[$1.id]?.until ?? 0) }
        d.items[2] = pick(["done", "dismissed"]).sorted { (d.closed[$0.id] ?? .distantPast) > (d.closed[$1.id] ?? .distantPast) }
        d.groups = computeGroups(d.items[0])
        return d
    }
    /// Case-insensitive search over everything a closed card shows; every space-separated word must match.
    func matches(_ todo: Todo, _ query: String) -> Bool {
        let words = query.lowercased().split(whereSeparator: \.isWhitespace)
        guard !words.isEmpty else { return true }
        let haystack: String
        if let hit = cache.haystack[todo.id] { haystack = hit } else {
            let a = actions[todo.id]
            let fields = [todo.title, todo.detail, todo.resolution ?? "", a?.reason ?? "", a?.note ?? ""]
                + todo.sources.flatMap { [$0.chat, $0.quote, $0.senderName ?? "", $0.recipientName ?? ""] }
            haystack = fields.joined(separator: "\n").lowercased()
            cache.haystack[todo.id] = haystack
        }
        return words.allSatisfy { haystack.contains($0) }
    }
    /// Archived cards matching the query, grouped by the day they were closed (newest first).
    func archivedSections(_ query: String) -> [(title: String, tasks: [Todo])] {
        if let hit = cache.sections[query] { return hit }
        var sections: [(title: String, tasks: [Todo])] = []
        for todo in items(2) where matches(todo, query) {
            let label = dayLabel(closedDate(todo))
            if sections.last?.title == label { sections[sections.count - 1].tasks.append(todo) }
            else { sections.append((label, [todo])) }
        }
        cache.sections[query] = sections
        return sections
    }
    func dayLabel(_ date: Date?) -> String {
        guard let date else { return "更早" }
        if appCalendar.isDate(date, inSameDayAs: now) { return "今天" }
        if let y = appCalendar.date(byAdding: .day, value: -1, to: now), appCalendar.isDate(date, inSameDayAs: y) { return "昨天" }
        return dateText(date, format: "M月d日")
    }
    func workGroups() -> [WorkGroup] { cache.groups }
    private func computeGroups(_ active: [Todo]) -> [WorkGroup] {
        var today: [Todo] = [], week: [Todo] = [], waiting: [Todo] = [], confirm: [Todo] = []
        for todo in active {
            let urgentAction = todo.category != "waiting" && isUrgent(todo)
            // Unconfirmed leads are kept out of the way unless they are urgent personal actions.
            if todo.isUnconfirmed && !urgentAction { confirm.append(todo) }
            // Responsibility wins over urgency: waiting never mixes with personal actions.
            else if todo.category == "waiting" { waiting.append(todo) }
            else if urgentAction { today.append(todo) }
            else { week.append(todo) }
        }
        // Only 今天优先 is always open; the rest start folded so the window reads as today's list.
        return [WorkGroup(id: "today", title: "今天优先", tasks: today),
                WorkGroup(id: "week", title: "本周处理", tasks: week, collapsible: true, hint: "不急今天"),
                WorkGroup(id: "waiting", title: "等别人回复", tasks: waiting, collapsible: true, hint: "球在对方那里"),
                WorkGroup(id: "confirm", title: "待确认", tasks: confirm, collapsible: true, hint: "线索不确定，有空再看")].filter { !$0.tasks.isEmpty }
    }
    var mineCount: Int { workGroups().filter { ["today", "week"].contains($0.id) }.reduce(0) { $0 + $1.tasks.count } }
    var summaryText: String {
        let groups = Dictionary(uniqueKeysWithValues: workGroups().map { ($0.id, $0.tasks.count) })
        var parts: [String] = []
        if newCount > 0 { parts.append("新\(newCount)") }
        if let n = groups["today"] { parts.append("今天\(n)") }
        if let n = groups["week"] { parts.append("本周\(n)") }
        if let n = groups["waiting"] { parts.append("等回复\(n)") }
        if let n = groups["confirm"] { parts.append("待确认\(n)") }
        return parts.isEmpty ? "眼前没有待办" : parts.joined(separator: " · ")
    }
    /// Morning summary line. Counts come from the same groups as the header summary.
    var morningSummaryText: String {
        let groups = Dictionary(uniqueKeysWithValues: workGroups().map { ($0.id, $0.tasks) })
        let today = groups["today"] ?? []
        var parts = ["今天 \(today.count) 件"]
        let late = today.filter(isOverdue).count
        if late > 0 { parts.append("逾期 \(late) 件") }
        if let n = groups["week"]?.count { parts.append("本周 \(n) 件") }
        if newCount > 0 { parts.append("新 \(newCount) 件") }
        if let n = groups["waiting"]?.count { parts.append("等回复 \(n) 件") }
        return parts.joined(separator: " · ")
    }
    /// True once today's first scheduled run has merged successfully.
    var todaysFirstRunDone: Bool {
        guard inbox.sync?.state == "ok", let scanned = parseDate(inbox.sync?.scannedThrough),
              appCalendar.isDate(scanned, inSameDayAs: now) else { return false }
        let first = appCalendar.date(byAdding: .hour, value: appConfig.hours.first ?? 9, to: appCalendar.startOfDay(for: now))!
        // Same 10-minute tolerance as late-run detection: a run that starts on time may scan through a little before the slot.
        return scanned >= first.addingTimeInterval(-10 * 60)
    }
    func waitingDays(_ todo: Todo) -> Int? {
        guard todo.category == "waiting" else { return nil }
        let since = parseDate(todo.waitingSince) ?? todo.sources.compactMap { parseDate($0.time) }.min() ?? parseDate(todo.firstSeen)
        guard let since else { return nil }
        return appCalendar.dateComponents([.day], from: appCalendar.startOfDay(for: since), to: appCalendar.startOfDay(for: now)).day
    }
    /// Confirmed waiting items on the 待办 tab that have waited 3 days or more get “催一下”.
    func canNudge(_ todo: Todo) -> Bool {
        status(todo) == "active" && !todo.isUnconfirmed && (waitingDays(todo) ?? 0) >= 3
    }
    /// Who the user is waiting on: the other side of the latest message whose direction is known; otherwise the chat name.
    func nudgeTarget(_ todo: Todo, me: String = appConfig.selfOpenDingTalkId ?? "") -> String {
        let latestFirst = todo.sources.sorted { (parseDate($0.time) ?? .distantPast) > (parseDate($1.time) ?? .distantPast) }
        for s in latestFirst where !me.isEmpty {
            guard let sender = s.senderId, !sender.isEmpty else { continue }
            let name = sender == me ? s.recipientName : s.senderName
            if let name, !name.isEmpty { return name }
        }
        return latestFirst.first.map(\.chat).flatMap { $0.isEmpty ? nil : $0 } ?? "对方"
    }
    /// A ready-to-paste follow-up of at most 200 characters. Copied only; nothing is sent.
    func nudgeText(_ todo: Todo) -> String {
        let days = waitingDays(todo) ?? 0
        let head = "\(clip(nudgeTarget(todo), 20))，想跟进一下「\(clip(todo.displayTitle, 60))」，已经等了 \(days) 天，方便时回个进度吗？"
        let first = todo.sources.min { (parseDate($0.time) ?? .distantFuture) < (parseDate($1.time) ?? .distantFuture) }
        guard let first else { return clip(head, 200) }
        let label = "\n原消息" + (parseDate(first.time).map { "（\(dateText($0, format: "M/d"))）" } ?? "") + "："
        let quote = redactSecrets(first.quote).split(whereSeparator: \.isNewline).joined(separator: " ")
        let room = 200 - head.count - label.count
        return room >= 8 ? head + label + clip(quote, room) : clip(head, 200)
    }
    func isNew(_ todo: Todo) -> Bool {
        status(todo) == "active" && (parseDate(todo.firstSeen).map { $0 > seenThrough } ?? false)
    }
    var newCount: Int { items(0).filter(isNew).count }
    func markSeen(at date: Date = Date()) {
        guard newCount > 0 else { return }
        seenThrough = date; persistSeen?(date); onChange?()
    }
    /// 整理 appends each new update as its own line, newest last. Cards lead with the newest one.
    func detailParts(_ todo: Todo) -> (latest: String, earlier: [String]) {
        let parts = todo.detail.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard let last = parts.last else { return ("", []) }
        return (last, Array(parts.dropLast()))
    }
    /// What the latest write to inbox.json touched: merges stamp firstSeen, lastSeen and closedAt with the same time as updatedAt.
    var lastRun: RunRecord {
        guard let stamp = inbox.updatedAt else { return RunRecord(when: nil, added: [], updated: [], closed: []) }
        var record = RunRecord(when: parseDate(stamp), added: [], updated: [], closed: [])
        for t in inbox.tasks where t.lastSeen == stamp {
            if t.firstSeen == stamp { record.added.append(t) }
            else if t.closedAt == stamp { record.closed.append(t) }
            else { record.updated.append(t) }
        }
        return record
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
        var entry = previous ?? LocalAction(status: nil, until: nil, updatedAt: isoNow())
        entry.status = status; entry.until = until?.timeIntervalSince1970; entry.updatedAt = isoNow()
        entry.reason = reason; entry.note = (note?.isEmpty ?? true) ? nil : note
        var next = actions
        next[todo.id] = entry
        return commit(next, todo: todo, previous: previous, undoText: undoText)
    }
    /// Save and offer an 8-second undo that puts back `previous`.
    @discardableResult private func commit(_ next: [String: LocalAction], todo: Todo, previous: LocalAction?, undoText: String) -> Bool {
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
    /// “改期限…”: a new deadline, or nil to go back to整理's. Leaves updatedAt alone so platform-sync precedence is unchanged.
    @discardableResult func setDue(_ todo: Todo, _ date: Date?) -> Bool {
        let previous = actions[todo.id]
        var entry = previous ?? LocalAction(status: nil, until: nil, updatedAt: isoNow())
        entry.dueOverride = date.map(isoLocalText)
        var next = actions
        next[todo.id] = entry.isEmpty ? nil : entry
        let text = date.map { "期限改为 \(dateText($0))" } ?? "已恢复原期限" + (parseDate(todo.due).map { " \(dateText($0))" } ?? "")
        return commit(next, todo: todo, previous: previous, undoText: text)
    }
    var dueChoices: [(label: String, date: Date)] {
        let start = appCalendar.startOfDay(for: now)
        let today18 = start.addingTimeInterval(18 * 3600)
        let monday = snoozeChoices[2].date
        return (today18 > now ? [("今天 18:00", today18)] : []) + [("明天 18:00", today18.addingTimeInterval(86400)), ("下周一 09:00", monday)]
    }
    /// Menu text that shows the actual moment: “1 小时后（11:53）”, “明天 09:00”, “下周一 09:00（10/26）”.
    func choiceText(_ label: String, _ date: Date) -> String {
        let today = appCalendar.isDate(date, inSameDayAs: now)
        guard label.contains(":") else { return label + "（\(dateText(date, format: today ? "HH:mm" : "M/d HH:mm"))）" }
        let tomorrow = appCalendar.date(byAdding: .day, value: 1, to: now).map { appCalendar.isDate(date, inSameDayAs: $0) } ?? false
        return today || tomorrow ? label : label + "（\(dateText(date, format: "M/d"))）"
    }
    func restoreDueText(_ todo: Todo) -> String {
        "恢复原期限（" + (parseDate(todo.due).map { dateText($0) } ?? "没有期限") + "）"
    }
    func task(_ id: String) -> Todo? { inbox.tasks.first { $0.id == id } }
    /// Which tab shows the card right now.
    func tab(for todo: Todo) -> Int { ["active": 0, "snoozed": 1][status(todo)] ?? 2 }
    func requestFocus(_ id: String, complete: Bool = false) {
        guard task(id) != nil else { return }
        focus = FocusRequest(id: id, complete: complete)
    }
    func setMuted(_ kind: NotifyKind, _ off: Bool) {
        if off { muted.insert(kind) } else { muted.remove(kind) }
        persistMuted?(muted); onChange?()
    }
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
    /// The original DingTalk messages, oldest first, ready to paste back into a chat.
    func sourcesText(_ todo: Todo) -> String {
        todo.sources.sorted { (parseDate($0.time) ?? .distantPast) < (parseDate($1.time) ?? .distantPast) }
            .map { "\($0.senderName ?? $0.chat) · \($0.chat) · \($0.time)\n\($0.quote)" }.joined(separator: "\n\n")
    }
    func handoffPrompt(_ todo: Todo) -> String {
        var lines = ["请接手「别忘了」里的这件事（事项 ID：\(todo.id)）。", "", "事项：\(todo.title)"]
        if let deadline = due(todo) { lines.append("期限：\(dateText(deadline, format: "M月d日 HH:mm"))" + (hasDueOverride(todo) ? "（我自己改的）" : "")) }
        lines += ["现状：\(todo.detail)", "", "依据："]
        for s in todo.sources.prefix(5) {
            lines.append("- \(s.directionText) · \(s.chat) · \(s.time)：\(s.quote.prefix(240))")
        }
        lines += ["", "先核对相关项目和已有 Codex 对话，避免重复做；能直接推进的就推进。发布、对外发消息、修改正式数据之前先问我。做完告诉我完成依据。"]
        return lines.joined(separator: "\n")
    }
    func handoffURL(_ todo: Todo) -> URL? { codexNewThreadURL(handoffPrompt(todo)) }

    // MARK: Notifications
    /// Timed notifications that should be pending: 15 minutes before a personal deadline, and when a snooze ends.
    /// Identifiers carry the fire time, so a changed time is a new request and an unchanged one is left alone.
    func scheduledNotices() -> [PlannedNotice] {
        var out: [PlannedNotice] = []
        if !muted.contains(.due) {
            let horizon = now.addingTimeInterval(7 * 86400)
            for t in items(0) where t.category != "waiting" {
                guard let d = due(t) else { continue }
                let fire = d.addingTimeInterval(-15 * 60)
                guard fire > now && fire < horizon else { continue }
                out.append(PlannedNotice(identifier: "due-\(t.id)-\(Int(fire.timeIntervalSince1970))", kind: .due, todoId: t.id,
                                         fireAt: fire, title: "15 分钟后到期", body: t.displayTitle, category: nil))
            }
        }
        if !muted.contains(.snooze) {
            for t in items(1) {
                guard let until = actions[t.id]?.until, until > now.timeIntervalSince1970 else { continue }
                out.append(PlannedNotice(identifier: "snooze-\(t.id)-\(Int(until))", kind: .snooze, todoId: t.id,
                                         fireAt: Date(timeIntervalSince1970: until), title: "回到待办：\(t.displayTitle)",
                                         body: "稍后提醒到点了。可以直接完成，或再推 1 小时。", category: NoticeAction.snoozeCategory))
            }
        }
        return out
    }
    /// One-off notifications due now. Marks what it returns, so calling again returns nothing new.
    func takeImmediateNotices() -> [PlannedNotice] {
        (takeMorningSummary().map { [$0] } ?? []) + takeNudges()
    }
    func takeMorningSummary() -> PlannedNotice? {
        let day = dateText(now, format: "yyyy-MM-dd")
        guard summaryShownDay != day, todaysFirstRunDone else { return nil }
        // Handled for the day even when nothing is shown, so a later run never brings a late “morning” summary.
        summaryShownDay = day; persistSummaryDay?(day)
        guard !muted.contains(.summary), mineCount > 0 else { return nil }
        return PlannedNotice(identifier: "summary-\(day)", kind: .summary, todoId: nil, fireAt: nil,
                             title: "今天的待办", body: morningSummaryText, category: nil)
    }
    /// Waiting items that just reached 3 days, once each (recorded as nudgedAt). Daytime only.
    func takeNudges() -> [PlannedNotice] {
        let hour = appCalendar.component(.hour, from: now)
        guard !muted.contains(.nudge), hour >= (appConfig.hours.first ?? 9), hour < 21 else { return [] }
        let fresh = items(0).filter { canNudge($0) && actions[$0.id]?.nudgedAt == nil }
        guard !fresh.isEmpty else { return [] }
        var next = actions
        let stamp = isoNow()
        for t in fresh {
            var entry = next[t.id] ?? LocalAction(status: nil, until: nil, updatedAt: stamp)
            entry.nudgedAt = stamp
            next[t.id] = entry
        }
        // Not recorded means not shown, so a failed save never turns into repeated notifications.
        guard persist(next) else { return [] }
        if fresh.count > 3 {
            return [PlannedNotice(identifier: "nudge-batch-\(stamp)", kind: .nudge, todoId: nil, fireAt: nil,
                                  title: "\(fresh.count) 件等回复已满 3 天",
                                  body: clip(fresh.map { nudgeTarget($0) }.joined(separator: "、"), 120) + "。卡片上可以一键复制催办话。", category: nil)]
        }
        return fresh.map { t in
            PlannedNotice(identifier: "nudge-\(t.id)", kind: .nudge, todoId: t.id, fireAt: nil,
                          title: "\(nudgeTarget(t)) 的回复已等 \(waitingDays(t) ?? 3) 天", body: t.displayTitle, category: nil)
        }
    }

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

/// 更新日志.md, bundled as changelog.md by build_app.py. A missing file just means no section and no dot.
let changelog: [ChangelogEntry] = {
    guard let url = Bundle.main.url(forResource: "changelog", withExtension: "md"),
          let text = try? String(contentsOf: url, encoding: .utf8) else { return [] }
    return parseChangelog(text)
}()

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

enum RowMode { case normal, completing, dismissing, pickingDue, pickingSnooze }

struct TodoRow: View {
    @ObservedObject var store: TodoStore
    var todo: Todo
    var length: CardLength = .full
    @State private var expanded = false
    @State private var showAll = false
    @State private var mode = RowMode.normal
    @State private var note = ""
    @State private var celebrating = false
    @State private var finishedReason: String?
    @State private var flash = false
    @State private var pickedDue = Date()
    @State private var copiedNudge = false
    @State private var showEarlier = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var status: String { store.status(todo) }
    var archived: Bool { store.isArchived(todo) }
    var shown: CardLength { showAll ? .full : length }
    var dueText: String? {
        guard let d = store.due(todo), !archived else { return nil }
        let mark = store.hasDueOverride(todo) ? " · 已改" : ""
        if d < store.now { return "已到期 · " + dateText(d) + mark }
        let today = appCalendar.isDate(d, inSameDayAs: store.now)
        return (today ? "今天 " : "") + dateText(d, format: today ? "HH:mm" : "M/d HH:mm") + mark
    }
    var body: some View {
        VStack(alignment: .leading, spacing: shown == .compact ? 6 : 9) {
            HStack(alignment: .top, spacing: 10) {
                checkButton
                VStack(alignment: .leading, spacing: 5) {
                    Text(todo.displayTitle).font(.system(size: 14, weight: .semibold)).lineSpacing(3)
                        .strikethrough(celebrating, color: .secondary)
                        .foregroundStyle(archived || celebrating ? Color.secondary : Color.primary)
                        .lineLimit(shown == .compact ? 2 : nil)
                        .fixedSize(horizontal: false, vertical: true)
                    if let finishedReason, celebrating {
                        Label("已完成 · \(finishedReason)", systemImage: "checkmark").font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(accent).transition(.opacity.combined(with: .move(edge: .top)))
                    } else if shown == .compact, let c = store.closure(todo) {
                        Text(c.headline + (c.when.map { " · " + $0 } ?? "")).font(.system(size: 10, weight: .medium))
                            .foregroundStyle(accent.opacity(0.85)).lineLimit(1)
                    } else { badges }
                }
                .contentShape(Rectangle())
                .onTapGesture { if length != .full { withAnimation(.snappy(duration: 0.22)) { showAll.toggle() } } }
                .help(length == .full ? "" : showAll ? "点击收起" : "点击展开这张卡片")
                Spacer(minLength: 0)
                moreMenu
            }
            if mode == .completing {
                reasonPanel(title: "为什么完成？以后回看时有据可查", reasons: completeReasons) { finish($0) }
            } else if mode == .dismissing {
                reasonPanel(title: "为什么忽略？整理时会参考，减少同类误判", reasons: dismissReasons) { dismiss($0) }
            } else if mode == .pickingDue || mode == .pickingSnooze {
                timePanel
            }
            if shown != .compact {
                detailBlock
                if let c = store.closure(todo) { closureBox(c) }
            }
            if status == "snoozed", let until = store.actions[todo.id]?.until {
                Label("\(dateText(Date(timeIntervalSince1970: until))) 回到待办", systemImage: "clock")
                    .font(.system(size: 10)).foregroundStyle(accent).padding(.leading, 38)
            }
            if shown != .compact { evidence }
        }
        .padding(.horizontal, 13).padding(.vertical, shown == .compact ? 9 : 13)
        .background(celebrating ? accent.opacity(0.09) : Color(nsColor: .controlBackgroundColor).opacity(0.82), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(celebrating ? accent.opacity(0.35) : Color.primary.opacity(0.055), lineWidth: 1))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(warm, lineWidth: 2).opacity(flash ? 1 : 0).allowsHitTesting(false))
        .contextMenu { menuItems }
        .onAppear { takeFocus() }
        .onChange(of: store.focus?.token) { takeFocus() }
    }

    /// Opened from a notification: highlight this card, and open the reason panel when the notification's “完成” was used.
    func takeFocus() {
        guard let f = store.focus, f.id == todo.id, store.highlightedFocus != f.token else { return }
        store.highlightedFocus = f.token
        if f.complete && !archived { withAnimation(.snappy(duration: 0.25)) { mode = .completing; note = "" } }
        withAnimation(.easeOut(duration: 0.2)) { flash = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.8) { withAnimation(.easeOut(duration: 0.6)) { flash = false } }
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
            if store.isNew(todo) { badge("新", .white, fill: accent).transition(.opacity.combined(with: .scale)) }
            if status == "dismissed" { badge(todo.category == "stale" && store.actions[todo.id]?.status == nil ? "自动收起" : "已忽略", .secondary) }
            if !archived {
                if todo.category == "waiting" {
                    let days = store.waitingDays(todo) ?? 0
                    badge(days >= 1 ? "等回复 · \(days) 天" : "等回复", days >= 5 ? overdue : .orange)
                    if store.canNudge(todo) {
                        Button { copyNudge() } label: {
                            Label(copiedNudge ? "已复制" : "催一下", systemImage: copiedNudge ? "checkmark" : "hand.wave")
                                .font(.system(size: 9, weight: .semibold)).foregroundStyle(accent).lineLimit(1).fixedSize()
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(accent.opacity(0.10), in: Capsule()).contentShape(Capsule())
                        }
                        .buttonStyle(.plain).help("复制一句催办话，去钉钉粘贴发送；这里不会代发")
                        .accessibilityLabel("复制催办话：" + todo.displayTitle)
                    }
                }
                if todo.isUnconfirmed { badge("待确认", calm) }
                if todo.priority == 0 && todo.category != "waiting" { badge("优先", accent) }
                if store.isHandedOff(todo) { badge("Codex 跟进", calm) }
                if todo.reportTodo != nil { badge("日报", accent) }
            }
            if let dueText {
                Text(dueText).font(.system(size: 10, weight: .medium))
                    .foregroundStyle(store.isOverdue(todo) ? .orange : .secondary).lineLimit(1)
                    .help(store.hasDueOverride(todo) ? "你自己改的期限" + (parseDate(todo.due).map { "，原期限 " + dateText($0) } ?? "") : "")
            }
            if let s = todo.sources.first {
                Text(s.chat).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1).layoutPriority(-1)
            }
        }
    }

    var moreMenu: some View {
        Menu { menuItems } label: { Image(systemName: "ellipsis").frame(width: 20, height: 24) }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .help("更多操作（也可以在卡片上按右键）").accessibilityLabel("更多操作：" + todo.displayTitle)
    }

    /// Shared by “…” and the card's right-click menu. DingTalk first, then time, then hand-off; 忽略 alone at the bottom.
    @ViewBuilder var menuItems: some View {
        if archived || status == "snoozed" {
            Button("恢复到待办") { withAnimation(.easeInOut(duration: 0.3)) { store.restore(todo) } }
            Divider()
        }
        Button("打开钉钉") { openDingTalk() }
        Button("复制原消息") { copyToPasteboard() }
        if store.canNudge(todo) { Button("复制催办话") { copyNudge() } }
        if !archived {
            Divider()
            if status != "snoozed" {
                Menu("稍后提醒") {
                    ForEach(store.snoozeChoices, id: \.label) { choice in
                        Button(store.choiceText(choice.label, choice.date)) {
                            withAnimation(.easeInOut(duration: 0.3)) { store.snooze(todo, until: choice.date, label: choice.label) }
                        }
                    }
                    Divider()
                    Button("自定义时间…") { openTimePanel(.pickingSnooze) }
                }
            }
            Menu("改期限") {
                ForEach(store.dueChoices, id: \.label) { choice in
                    Button(store.choiceText(choice.label, choice.date)) { saveDue(choice.date) }
                }
                Divider()
                Button("自定义…") { openTimePanel(.pickingDue) }
                if store.hasDueOverride(todo) { Button(store.restoreDueText(todo)) { saveDue(nil) } }
            }
            Divider()
            Button(store.isHandedOff(todo) ? "再交给 Codex…" : "交给 Codex 处理…") {
                openURL(store.handoffURL(todo)); store.markHandoff(todo)
            }
            Divider()
            Button("忽略这条…") { withAnimation(.snappy(duration: 0.25)) { mode = .dismissing; note = "" } }
        }
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

    /// Date and time for “自定义…” in 改期限 or 稍后提醒; the common choices are already in the menus.
    var timePanel: some View {
        let snoozing = mode == .pickingSnooze
        let title = snoozing ? "稍后提醒 · 选回到待办的时间"
            : "改期限 · " + (parseDate(todo.due).map { "整理给的期限：\(dateText($0))" } ?? "整理没有给期限")
        return VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                DatePicker(snoozing ? "回到待办" : "期限", selection: $pickedDue, displayedComponents: [.date, .hourAndMinute])
                    .labelsHidden().datePickerStyle(.field).font(.system(size: 11))
                    .environment(\.timeZone, appTimeZone).environment(\.calendar, appCalendar)
                    .environment(\.locale, Locale(identifier: "zh_CN"))
                Button("确定") {
                    if snoozing {
                        let label = dateText(pickedDue)
                        withAnimation(.easeInOut(duration: 0.3)) { store.snooze(todo, until: pickedDue, label: label); mode = .normal }
                    } else { saveDue(pickedDue) }
                }
                .font(.system(size: 11)).buttonStyle(.bordered).keyboardShortcut(.defaultAction)
                .disabled(snoozing && pickedDue <= Date())
                Spacer(minLength: 0)
                Button("取消") { withAnimation(.snappy(duration: 0.2)) { mode = .normal } }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(10).background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 10))
        .padding(.leading, 38)
        .transition(.opacity.combined(with: .move(edge: .top)))
    }
    func openTimePanel(_ next: RowMode) {
        pickedDue = next == .pickingDue ? (store.due(todo) ?? store.dueChoices[0].date) : store.snoozeChoices[1].date
        withAnimation(.snappy(duration: 0.25)) { mode = next }
    }
    func saveDue(_ date: Date?) {
        withAnimation(.easeInOut(duration: 0.3)) { if store.setDue(todo, date) { mode = .normal } }
    }
    func copyNudge() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(store.nudgeText(todo), forType: .string)
        withAnimation(.snappy(duration: 0.2)) { copiedNudge = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { withAnimation(.snappy(duration: 0.2)) { copiedNudge = false } }
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
                        .lineLimit(expanded ? nil : shown == .standard ? 1 : 3).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(9).background(accent.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        .padding(.leading, 38)
    }

    /// Newest update only; earlier updates stay one click away so long-running items don't flood the list.
    @ViewBuilder var detailBlock: some View {
        let parts = store.detailParts(todo)
        if !parts.latest.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text(parts.latest).font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(3)
                    .lineLimit(shown == .full && !archived ? nil : 2)
                    .fixedSize(horizontal: false, vertical: true)
                if shown == .full && !parts.earlier.isEmpty {
                    Button { withAnimation(.snappy(duration: 0.2)) { showEarlier.toggle() } } label: {
                        HStack(spacing: 5) {
                            Image(systemName: showEarlier ? "chevron.down" : "chevron.right").font(.system(size: 9, weight: .semibold))
                            Text(showEarlier ? "收起更早经过" : "更早经过")
                            Text("\(parts.earlier.count) 段").foregroundStyle(.tertiary)
                        }.font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel((showEarlier ? "收起更早经过：" : "展开更早经过：") + todo.displayTitle)
                    if showEarlier {
                        VStack(alignment: .leading, spacing: 5) {
                            ForEach(Array(parts.earlier.reversed().enumerated()), id: \.offset) { _, line in
                                Text(line).font(.system(size: 11)).foregroundStyle(.tertiary).lineSpacing(2)
                                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                            }
                        }
                        .padding(.leading, 10)
                        .overlay(alignment: .leading) { Rectangle().fill(Color.primary.opacity(0.08)).frame(width: 2) }
                        .transition(.opacity)
                    }
                }
            }.padding(.leading, 38)
        }
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
                        Button("复制原消息") { copyToPasteboard() }
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
        NSPasteboard.general.setString(store.sourcesText(todo), forType: .string)
    }
    func badge(_ text: String, _ color: Color, fill: Color? = nil) -> some View {
        Text(text).font(.system(size: 9, weight: .semibold)).foregroundStyle(color).lineLimit(1).fixedSize()
            .padding(.horizontal, 5).padding(.vertical, 2).background(fill ?? color.opacity(0.10), in: Capsule())
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
    @State private var showInfo = false
    @State private var showRecord = false
    @State private var query = ""
    @FocusState private var searchFocused: Bool
    @State private var hovering = false
    // Per-tab card length: 待办 and 稍后 standard, 已收起 compact unless the user changes it.
    // New keys (v2) so the earlier “完整” default doesn't carry over.
    @AppStorage("cardLength.v2.0") private var length0 = CardLength.standard.rawValue
    @AppStorage("cardLength.v2.1") private var length1 = CardLength.standard.rawValue
    @AppStorage("cardLength.v2.2") private var length2 = CardLength.compact.rawValue
    var length: CardLength { CardLength(rawValue: [length0, length1, length2][tab]) ?? .full }
    func setLength(_ value: CardLength) {
        withAnimation(.snappy(duration: 0.25)) {
            switch tab { case 0: length0 = value.rawValue; case 1: length1 = value.rawValue; default: length2 = value.rawValue }
        }
    }
    /// Ids of folded groups the user opened, comma-separated. 今天优先 is never folded.
    @AppStorage("openGroups") private var openGroups = ""
    func isOpen(_ group: WorkGroup) -> Bool { !group.collapsible || openGroups.split(separator: ",").contains(Substring(group.id)) }
    func setOpen(_ id: String, _ open: Bool) {
        var ids = Set(openGroups.split(separator: ",").map(String.init))
        if open { ids.insert(id) } else { ids.remove(id) }
        openGroups = ids.sorted().joined(separator: ",")
    }
    var body: some View {
        VStack(spacing: 0) {
            header
            if !settings.collapsed {
                HStack(spacing: 5) {
                    tabButton("待办", 0, count: store.items(0).count)
                    tabButton("稍后", 1, count: store.items(1).count)
                    tabButton("已收起", 2, count: store.items(2).count)
                    lengthMenu
                }.padding(.leading, 17).padding(.trailing, 12).padding(.bottom, 12)
                .background {
                    // ⌘F jumps to the archive search; ⌘- / ⌘= shorten or lengthen cards on the current tab.
                    Group {
                        Button("") { tab = 2; DispatchQueue.main.async { searchFocused = true } }.keyboardShortcut("f", modifiers: .command)
                        Button("") { setLength(CardLength(rawValue: max(0, length.rawValue - 1))!) }.keyboardShortcut("-", modifiers: .command)
                        Button("") { setLength(CardLength(rawValue: min(2, length.rawValue + 1))!) }.keyboardShortcut("=", modifiers: .command)
                    }.opacity(0).allowsHitTesting(false)
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
        .onHover { inside in
            hovering = inside
            guard inside else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { if hovering { withAnimation(.easeOut(duration: 0.4)) { store.markSeen() } } }
        }
        .simultaneousGesture(TapGesture().onEnded { withAnimation(.easeOut(duration: 0.4)) { store.markSeen() } })
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
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 9) {
                    if store.items(tab).isEmpty { emptyState }
                    if tab == 0 {
                        ForEach(store.workGroups()) { group in
                            groupHeader(group)
                            if isOpen(group) {
                                ForEach(group.tasks) { todo in row(todo) }
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
                            ForEach(section.tasks) { todo in row(todo) }
                        }
                    } else {
                        ForEach(store.items(tab)) { todo in row(todo) }
                    }
                }.padding(.horizontal, 14).padding(.bottom, 14)
            }
            .onChange(of: store.focus?.token, initial: true) { jump(proxy) }
        }
    }
    func row(_ todo: Todo) -> some View {
        TodoRow(store: store, todo: todo, length: length).id(todo.id).transition(rowTransition)
    }
    /// Show the card a notification points at: its tab, the 待确认 group if needed, then scroll it into view.
    func jump(_ proxy: ScrollViewProxy) {
        guard let f = store.focus, store.scrolledFocus != f.token, let todo = store.task(f.id) else { return }
        store.scrolledFocus = f.token
        let target = store.tab(for: todo)
        if target == 2 { query = "" }
        if target == 0, let group = store.workGroups().first(where: { $0.tasks.contains { $0.id == todo.id } }), !isOpen(group) { setOpen(group.id, true) }
        withAnimation(.snappy(duration: 0.2)) { tab = target }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            withAnimation(.easeInOut(duration: 0.3)) { proxy.scrollTo(todo.id, anchor: .top) }
        }
    }

    @ViewBuilder func groupHeader(_ group: WorkGroup) -> some View {
        let open = isOpen(group)
        let content = HStack(spacing: 6) {
            if group.collapsible {
                Image(systemName: open ? "chevron.down" : "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
            }
            Text(group.title).font(.system(size: 12, weight: .semibold))
            Text("\(group.tasks.count)").font(.system(size: 10)).foregroundStyle(.secondary).contentTransition(.numericText())
            if !open, let hint = group.hint {
                Text(hint).font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Spacer()
        }.padding(.horizontal, 4).padding(.top, 8).frame(minHeight: 24).contentShape(Rectangle())
        if group.collapsible {
            Button { withAnimation(.snappy(duration: 0.25)) { setOpen(group.id, !open) } } label: { content }
                .buttonStyle(.plain).accessibilityLabel((open ? "收起" : "展开") + group.title)
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
            Button { showRecord.toggle() } label: {
                HStack(spacing: 4) {
                    Image(systemName: "clock.arrow.circlepath")
                    Text("整理记录")
                }.font(.system(size: 11, weight: .medium)).foregroundStyle(accent)
                    .padding(.horizontal, 8).padding(.vertical, 5).background(accent.opacity(0.10), in: Capsule())
            }
            .buttonStyle(.plain).help("上次整理读到哪里、改了哪些事项")
            .accessibilityLabel("整理记录")
            .popover(isPresented: $showRecord, arrowEdge: .top) { recordPanel }
            Button { showInfo.toggle() } label: {
                Image(systemName: "info.circle").font(.system(size: 14))
                    .overlay(alignment: .topTrailing) {
                        if hasNews { Circle().fill(warm).frame(width: 6, height: 6).offset(x: 2, y: -1) }
                    }
            }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .help(hasNews ? "有新版本，点开看更新日志" : "通知设置和更新日志")
                .accessibilityLabel(hasNews ? "设置，有新的更新日志" : "设置与更新日志")
                .popover(isPresented: $showInfo, arrowEdge: .top) { infoPanel }
                .onChange(of: showInfo) { if showInfo, let latest = changelog.first { changelogSeen = latest.title } }
        }.padding(.horizontal, 17).padding(.vertical, 12)
    }

    /// The dot shows once per new changelog version (its newest heading), until ⓘ is opened.
    @AppStorage("changelogSeen") private var changelogSeen = ""
    @State private var showAllChangelog = false
    var hasNews: Bool { changelog.first.map { $0.title != changelogSeen } ?? false }

    /// Last write to the list: when, how far it read, and which items it added, updated or closed (click to jump).
    var recordPanel: some View {
        let record = store.lastRun
        let sync = store.inbox.sync
        return VStack(alignment: .leading, spacing: 8) {
            Text("整理记录").font(.system(size: 12, weight: .semibold))
            if let when = record.when {
                Text("\(store.dayLabel(when)) \(dateText(when, format: "HH:mm")) 写入" + (sync?.mode == "full" ? " · 完整回扫" : sync?.mode == "incremental" ? " · 增量整理" : ""))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Text(store.syncLine).font(.system(size: 10)).foregroundStyle(.secondary)
            if sync?.state == "error" || sync?.state == "partial" {
                Label(sync?.message ?? "这次读取不完整，已保留旧待办。", systemImage: "exclamationmark.circle")
                    .font(.system(size: 11)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            } else if let message = sync?.message, !message.isEmpty {
                Text(message).font(.system(size: 11)).fixedSize(horizontal: false, vertical: true)
            }
            Divider().padding(.vertical, 2)
            if record.isEmpty {
                Text("这次没有新增、更新或收起的事项。").font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        recordGroup("新增", record.added)
                        recordGroup("有更新", record.updated)
                        recordGroup("整理时收起", record.closed)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 300)
            }
            HStack {
                if store.runURL != nil { Button("打开整理对话") { openURL(store.runURL) } }
                if let dir = appConfig.projectDirectory { Button("打开项目目录") { openURL(URL(fileURLWithPath: dir)) } }
            }.font(.system(size: 11)).buttonStyle(.bordered)
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(14).frame(width: 300, alignment: .leading)
    }
    @ViewBuilder func recordGroup(_ title: String, _ tasks: [Todo]) -> some View {
        if !tasks.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("\(title) \(tasks.count)").font(.system(size: 11, weight: .semibold)).foregroundStyle(accent)
                ForEach(tasks) { t in
                    Button { showRecord = false; store.requestFocus(t.id) } label: {
                        Text("· " + t.displayTitle).font(.system(size: 11)).foregroundStyle(.primary).lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }.buttonStyle(.plain).help("在浮窗里定位这件事")
                }
            }
        }
    }

    var infoPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("系统通知").font(.system(size: 12, weight: .semibold))
            ForEach(NotifyKind.allCases) { kind in
                Toggle(kind.label, isOn: Binding(get: { !store.muted.contains(kind) }, set: { store.setMuted(kind, !$0) }))
                    .toggleStyle(.checkbox).font(.system(size: 11))
            }
            if !changelog.isEmpty {
                Divider().padding(.vertical, 2)
                changelogSection
            }
        }
        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        .padding(14).frame(width: 300, alignment: .leading)
    }

    var changelogSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("更新日志").font(.system(size: 12, weight: .semibold))
            let entries = showAllChangelog ? changelog : Array(changelog.prefix(3))
            let content = VStack(alignment: .leading, spacing: 8) {
                ForEach(entries) { entry in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(entry.title).font(.system(size: 11, weight: .semibold)).foregroundStyle(accent)
                        ForEach(Array(entry.items.enumerated()), id: \.offset) { _, item in
                            Text("· " + item).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            if showAllChangelog { ScrollView { content }.frame(height: 280) } else { content }
            if changelog.count > 3 {
                Button(showAllChangelog ? "只看最近 3 条" : "展开全部（\(changelog.count) 条）") {
                    withAnimation(.snappy(duration: 0.2)) { showAllChangelog.toggle() }
                }.buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(accent)
            }
        }
    }

    var lengthMenu: some View {
        Menu {
            Text("卡片长度（⌘- 缩短，⌘= 加长）")
            ForEach(CardLength.allCases) { option in
                Button { setLength(option) } label: {
                    if option == length { Label("\(option.label)：\(option.hint)", systemImage: "checkmark") }
                    else { Text("\(option.label)：\(option.hint)") }
                }
            }
        } label: {
            Image(systemName: ["text.line.first.and.arrowtriangle.forward", "text.alignleft", "text.justify.leading"][length.rawValue])
                .font(.system(size: 12)).frame(width: 26, height: 28)
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .help("卡片长度：\(length.label)").accessibilityLabel("卡片长度，当前\(length.label)")
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

/// Delivers the store's planned notifications: timed ones (due, snooze) and one-off ones (morning summary, nudges).
@MainActor final class Notifier {
    let center = UNUserNotificationCenter.current()
    var authorized = false
    /// Timed requests added by this process. Delivered ones fall out of the plan, so they are never added twice.
    var scheduled: Set<String> = []
    var busy = false
    func start(_ store: TodoStore) {
        // Rebuilt from inbox and actions on every launch.
        center.removeAllPendingNotificationRequests()
        let complete = UNNotificationAction(identifier: NoticeAction.complete, title: "完成", options: [.foreground])
        let later = UNNotificationAction(identifier: NoticeAction.snoozeHour, title: "再推 1 小时", options: [])
        center.setNotificationCategories([UNNotificationCategory(identifier: NoticeAction.snoozeCategory, actions: [complete, later],
                                                                 intentIdentifiers: [], options: [])])
        center.requestAuthorization(options: [.alert, .sound]) { ok, _ in
            Task { @MainActor in self.authorized = ok; self.refresh(store) }
        }
    }
    func refresh(_ store: TodoStore) {
        // Recording a nudge saves actions, which calls back here; the outer call already covers it.
        guard authorized, !busy else { return }
        busy = true; defer { busy = false }
        let now = Date()
        let plan = store.scheduledNotices().filter { ($0.fireAt ?? now) > now }
        let wanted = Set(plan.map(\.identifier))
        let gone = scheduled.subtracting(wanted)
        if !gone.isEmpty { center.removePendingNotificationRequests(withIdentifiers: Array(gone)) }
        for notice in plan where !scheduled.contains(notice.identifier) { post(notice) }
        scheduled = wanted
        for notice in store.takeImmediateNotices() { post(notice) }
    }
    func post(_ notice: PlannedNotice) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = .default
        if let id = notice.todoId { content.userInfo = ["todoId": id] }
        if let category = notice.category { content.categoryIdentifier = category }
        // A calendar trigger keeps the exact wall-clock time across sleep.
        let trigger = notice.fireAt.map {
            UNCalendarNotificationTrigger(dateMatching: Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: $0), repeats: false)
        }
        center.add(UNNotificationRequest(identifier: notice.identifier, content: content, trigger: trigger))
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
    var notifier = Notifier()
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
        if !testMode {
            let defaults = UserDefaults.standard
            if defaults.object(forKey: "seenThrough") == nil { defaults.set(Date().timeIntervalSince1970, forKey: "seenThrough") }
            store.seenThrough = Date(timeIntervalSince1970: defaults.double(forKey: "seenThrough"))
            store.persistSeen = { defaults.set($0.timeIntervalSince1970, forKey: "seenThrough") }
            store.muted = Set((defaults.stringArray(forKey: "mutedNotifications") ?? []).compactMap(NotifyKind.init(rawValue:)))
            store.persistMuted = { defaults.set($0.map(\.rawValue).sorted(), forKey: "mutedNotifications") }
            store.summaryShownDay = defaults.string(forKey: "summaryShownDay")
            store.persistSummaryDay = { defaults.set($0, forKey: "summaryShownDay") }
        }
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
        statusItem?.button?.title = "✓ \(store.mineCount)" + (store.newCount > 0 ? " · 新\(store.newCount)" : "")
        statusItem?.button?.toolTip = "\(appConfig.appName) · \(store.summaryText)\n点击显示或隐藏，右键更多操作"
        if !testMode { notifier.refresh(store) }
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
        let todoId = response.notification.request.content.userInfo["todoId"] as? String
        let action = response.actionIdentifier
        await MainActor.run { self.handleNotification(action: action, todoId: todoId) }
    }
    /// “再推 1 小时” snoozes in the background; anything else opens the window on the card.
    func handleNotification(action: String, todoId: String?) {
        store.now = Date()
        // An old notification must not reopen something already completed or dismissed in the window.
        if action == NoticeAction.snoozeHour, let id = todoId, let todo = store.task(id), !store.isArchived(todo) {
            store.snooze(todo, until: Date().addingTimeInterval(3600), label: "1 小时后")
            return
        }
        if settings.collapsed { settings.toggleCollapse() }
        show()
        if let id = todoId { store.requestFocus(id, complete: action == NoticeAction.complete) }
    }
}
@main struct TodoApplication {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
