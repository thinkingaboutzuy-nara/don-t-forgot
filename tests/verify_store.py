#!/usr/bin/env python3
"""Compile the window's state logic (everything above `// MARK: - UI`) with synthetic checks and run them.
Uses a temporary data directory; never reads or writes live task state."""
import argparse
import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CANDIDATES = [HERE.parent / 'TodoWindow.swift', HERE.parent / 'assets/native-app/TodoWindow.swift']

CHECKS = r'''
@main struct StoreCheck {
    @MainActor static func main() throws {
        var failed = 0
        func expect(_ ok: Bool, _ name: String) { if !ok { print("FAIL \(name)"); failed += 1 } }
        let dir = URL(fileURLWithPath: CommandLine.arguments[1])
        let store = TodoStore(root: dir, live: false)
        store.now = parseDate("2026-10-21T10:00:00+08:00")!
        func task(_ id: String, _ category: String, priority: Int = 1, due: String? = nil, title: String? = nil,
                  sources: [Source] = [], resolution: String? = nil) -> Todo {
            Todo(id: id, title: title ?? id, detail: "", category: category, priority: priority, due: due,
                 sources: sources, resolution: resolution, needsConfirm: nil, waitingSince: nil, dispatch: nil,
                 firstSeen: nil, lastSeen: "2026-10-20T07:58:26.869695+00:00", closedAt: nil)
        }
        func act(_ status: String?, _ offset: Double? = nil, reason: String? = nil) -> LocalAction {
            LocalAction(status: status, until: offset.map { store.now.timeIntervalSince1970 + $0 }, updatedAt: "2026-10-21T01:00:00Z", reason: reason)
        }
        // Status precedence (mirrors update_inbox.effective_status)
        let cases: [(String, String, LocalAction?, String)] = [
            ("expired snooze on resolved", "resolved", act("snoozed", -1), "done"),
            ("expired snooze on action", "action", act("snoozed", -1), "active"),
            ("future snooze", "resolved", act("snoozed", 3600), "snoozed"),
            ("explicit restore", "resolved", act("active"), "active"),
            ("manual done", "action", act("done"), "done"),
            ("manual dismiss", "resolved", act("dismissed"), "dismissed"),
            ("auto resolved", "resolved", nil, "done"),
            ("auto stale", "stale", nil, "dismissed"),
            ("handoff only keeps auto state", "resolved", act(nil), "done"),
            ("plain action", "action", nil, "active"),
        ]
        for (name, category, action, expected) in cases {
            store.actions = action.map { ["case": $0] } ?? [:]
            expect(store.status(task("case", category)) == expected, name)
        }
        // Groups
        store.actions = [:]
        store.inbox.tasks = [
            task("urgent", "action", priority: 0),
            task("due-today", "action", due: "2026-10-21T18:00:00+08:00"),
            task("overdue", "action", due: "2026-10-19T18:00:00+08:00"),
            task("later", "action", due: "2026-10-24T18:00:00+08:00"),
            task("waiting-urgent", "waiting", priority: 0, due: "2026-10-20T18:00:00+08:00"),
            task("confirm-waiting", "waiting", title: "待确认：陈盈是否到货"),
            task("confirm-urgent", "action", priority: 0, title: "待确认：真机测试"),
            task("confirm-plain", "action", title: "待确认：备用金方案"),
        ]
        let groups = Dictionary(uniqueKeysWithValues: store.workGroups().map { ($0.id, Set($0.tasks.map(\.id))) })
        expect(groups["today"] == ["urgent", "due-today", "overdue", "confirm-urgent"], "today group \(groups["today"] ?? [])")
        expect(groups["week"] == ["later"], "week group")
        expect(groups["waiting"] == ["waiting-urgent"], "waiting stays separate")
        expect(groups["confirm"] == ["confirm-waiting", "confirm-plain"], "confirm group")
        expect(store.workGroups().map(\.collapsible) == [false, true, true, true], "only 今天优先 stays open")
        expect(store.mineCount == 5, "menu bar counts own actions only")
        expect(task("x", "action", title: "待确认：备用金方案").displayTitle == "备用金方案", "display title strips prefix")
        // Waiting days from local source time
        let src = Source(chat: "c", time: "2026-10-18 09:30", quote: "q", messageId: "m", conversationId: "c")
        expect(store.waitingDays(task("w", "waiting", sources: [src])) == 3, "waiting days from source time")
        expect(parseDate("2026-10-20T07:58:26.869695+00:00") != nil, "python microsecond timestamps parse")
        // Closure explanations
        store.actions = ["a": act("done", reason: "对方已处理"), "b": act("done")]
        expect(store.closure(task("a", "action"))?.headline.contains("对方已处理") == true, "manual reason shown")
        expect(store.closure(task("b", "action"))?.headline.contains("未记录理由") == true, "legacy tick explained")
        expect(store.closure(task("c", "resolved", resolution: "已上线"))?.detail == "已上线", "auto resolution shown")
        expect(store.closure(task("d", "stale", resolution: "7 天无进展"))?.headline.contains("自动收起") == true, "stale explained")
        expect(store.closure(task("e", "action")) == nil, "open task has no closure")
        // Persisted reason, note, hand-off and undo
        store.actions = [:]
        let t = task("p", "action")
        store.markHandoff(t)
        expect(store.complete(t, reason: "办完了", note: "已发给财务"), "complete saves")
        let saved = try JSONDecoder().decode([String: LocalAction].self, from: Data(contentsOf: dir.appendingPathComponent("actions.json")))
        expect(saved["p"]?.status == "done" && saved["p"]?.reason == "办完了" && saved["p"]?.note == "已发给财务", "reason and note persisted")
        expect(saved["p"]?.handoffAt != nil, "hand-off survives completion")
        store.undoLast()
        expect(store.actions["p"]?.status == nil && store.actions["p"]?.handoffAt != nil, "undo restores previous entry")
        // Snooze choices
        let choices = Dictionary(uniqueKeysWithValues: store.snoozeChoices.map { ($0.label, $0.date) })
        expect(choices["明天 09:00"] == parseDate("2026-10-22T09:00:00+08:00"), "tomorrow 09:00")
        expect(choices["下周一 09:00"] == parseDate("2026-10-26T09:00:00+08:00"), "next Monday 09:00")
        // Menu text names the actual moment
        let menuText = store.snoozeChoices.map { store.choiceText($0.label, $0.date) }
        expect(menuText == ["1 小时后（11:00）", "明天 09:00", "下周一 09:00（10/26）"], "snooze menu text \(menuText)")
        expect(store.choiceText("1 小时后", parseDate("2026-10-22T00:30:00+08:00")!) == "1 小时后（10/22 00:30）", "late-night hour names the day")
        expect(store.restoreDueText(task("x", "action", due: "2026-10-25T18:00:00+08:00")) == "恢复原期限（10/25 18:00）"
               && store.restoreDueText(task("y", "action")) == "恢复原期限（没有期限）", "restore text shows original deadline")
        // Late run detection (schedule 09:00, 15:00)
        store.inbox.updatedAt = "x"
        store.inbox.sync = SyncInfo(state: "ok", message: nil, scannedThrough: "2026-10-21T09:00:43+08:00", scope: nil, schedule: nil, mode: nil, runThreadId: nil)
        store.now = parseDate("2026-10-21T16:00:00+08:00")!
        expect(store.staleText == nil, "15:00 run still within grace")
        store.now = parseDate("2026-10-21T16:40:00+08:00")!
        expect(store.staleText?.contains("10/21 15:00") == true, "missed 15:00 run flagged")
        store.now = parseDate("2026-10-22T08:00:00+08:00")!
        expect(store.staleText?.contains("15:00") == true, "overnight still flags missed 15:00")
        store.inbox.sync?.scannedThrough = "2026-10-21T15:01:00+08:00"
        expect(store.staleText == nil, "overnight gap after 15:00 run is normal")
        // Hand-off link round trip
        let url = store.handoffURL(task("h&1", "action", title: "核对 A+B=C"))!
        let prompt = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "prompt" }?.value ?? ""
        expect(url.host == "threads" && url.path == "/new", "codex new-thread link")
        expect(prompt.contains("事项 ID：h&1") && prompt.contains("核对 A+B=C"), "prompt survives encoding")
        // Report platform link: the later of platform change and manual action wins
        store.now = parseDate("2026-10-21T17:00:00+08:00")!
        var linked = task("r", "resolved", resolution: "日报平台已勾选")
        linked.reportTodo = ReportTodo(id: "t1", version: 4, done: true, reportId: nil, workDate: nil, changedAt: "2026-10-21T08:00:00Z")
        store.actions = ["r": LocalAction(status: "active", until: nil, updatedAt: "2026-10-21T07:00:00Z")]
        expect(store.status(linked) == "done", "platform tick newer than manual restore wins")
        store.actions = ["r": LocalAction(status: "active", until: nil, updatedAt: "2026-10-21T08:30:00Z")]
        expect(store.status(linked) == "active", "manual restore newer than platform tick wins")
        expect(Source(chat: "工作汇报", time: "2026-10-19", quote: "q", messageId: "todo:t1", conversationId: "report:x", conversationType: "report").directionText == "日报待办", "report source label")
        // Cards lead with the newest update; the run record lists what the last write touched
        var long = task("l1", "action")
        long.detail = "10月23日承诺优化脚本。\n\n27日10:56 二维码链接被拒绝，承诺重新取链。\n  10月27日17:39 544 笔已核对，剩余仍需结果。  "
        let parts = store.detailParts(long)
        expect(parts.latest == "10月27日17:39 544 笔已核对，剩余仍需结果。" && parts.earlier.count == 2 && parts.earlier[0] == "10月23日承诺优化脚本。", "newest update first \(parts)")
        expect(store.detailParts(task("e", "action")).latest.isEmpty, "empty detail")
        let stamp = "2026-10-21T01:05:00.123456+00:00"
        var added = task("ra", "action"); added.firstSeen = stamp; added.lastSeen = stamp
        var touched = task("ru", "action"); touched.firstSeen = "2026-10-19T01:00:00+00:00"; touched.lastSeen = stamp
        var closedNow = task("rc", "resolved", resolution: "已上线"); closedNow.lastSeen = stamp; closedNow.closedAt = stamp
        let untouched = task("rx", "action")
        store.inbox.tasks = [added, touched, closedNow, untouched]
        store.inbox.updatedAt = stamp
        let record = store.lastRun
        expect(record.added.map(\.id) == ["ra"] && record.updated.map(\.id) == ["ru"] && record.closed.map(\.id) == ["rc"] && record.when == parseDate(stamp), "run record")
        store.inbox.updatedAt = "2026-10-21T02:00:00+00:00"
        expect(store.lastRun.isEmpty, "a later sync-only write lists nothing")
        let msgs = task("m", "action", sources: [Source(chat: "采购群", time: "2026-10-20 10:00", quote: "第二条", messageId: "2", conversationId: "c", senderName: "叶龙"),
                                                   Source(chat: "采购群", time: "2026-10-19 09:00", quote: "第一条", messageId: "1", conversationId: "c")])
        expect(store.sourcesText(msgs) == "采购群 · 采购群 · 2026-10-19 09:00\n第一条\n\n叶龙 · 采购群 · 2026-10-20 10:00\n第二条", "copied messages oldest first \(store.sourcesText(msgs))")
        // Archive search and day sections
        store.now = parseDate("2026-10-22T18:00:00+08:00")!
        var withQuote = task("q1", "action", title: "核对成都编码", sources: [Source(chat: "设备采购群", time: "2026-10-20 10:00", quote: "请把 20 台编码发我", messageId: "m", conversationId: "c", senderName: "叶龙")])
        withQuote.detail = "出入库单待补"
        store.inbox.tasks = [withQuote, task("q2", "action", title: "交付周报"), task("q3", "resolved", title: "旧事", resolution: "Codex 已发布")]
        store.actions = ["q1": LocalAction(status: "done", until: nil, updatedAt: "2026-10-22T02:00:00Z", reason: "对方已处理", note: "叶龙确认收到"),
                         "q2": LocalAction(status: "done", until: nil, updatedAt: "2026-10-21T02:00:00Z", reason: "办完了")]
        func found(_ q: String) -> [String] { store.archivedSections(q).flatMap { $0.tasks.map(\.id) } }
        expect(found("") == ["q1", "q2", "q3"], "empty query lists all, newest first \(found(""))")
        expect(found("叶龙") == ["q1"] && found("编码 出入库") == ["q1"] && found("对方已处理") == ["q1"], "matches quote, sender, detail and reason")
        expect(found("codex") == ["q3"] && found("编码 周报").isEmpty, "case-insensitive; all words must match")
        expect(store.archivedSections("").map(\.title) == ["今天", "昨天", "10月20日"], "day sections \(store.archivedSections("").map(\.title))")
        // “新” badge: merged after seenThrough, only while active; cleared by markSeen
        store.actions = [:]
        var fresh = task("n1", "action"); fresh.firstSeen = "2026-10-22T01:00:00.123456+00:00"
        var older = task("n2", "action"); older.firstSeen = "2026-10-20T01:00:00+00:00"
        var closedNew = task("n3", "resolved", resolution: "x"); closedNew.firstSeen = "2026-10-22T01:00:00+00:00"
        store.inbox.tasks = [fresh, older, closedNew]
        store.seenThrough = parseDate("2026-10-21T00:00:00Z")!
        var persisted: Date?
        store.persistSeen = { persisted = $0 }
        expect(store.isNew(fresh) && !store.isNew(older) && !store.isNew(closedNew) && store.newCount == 1, "new = active and merged after last look")
        expect(store.summaryText.hasPrefix("新1"), "summary leads with new count \(store.summaryText)")
        store.markSeen(at: parseDate("2026-10-22T02:00:00Z")!)
        expect(store.newCount == 0 && persisted == parseDate("2026-10-22T02:00:00Z"), "markSeen clears and persists")
        // Own deadline: wins for groups, ordering and due notices; clear and undo go back to整理's due
        store.now = parseDate("2026-10-22T10:00:00+08:00")!
        store.actions = [:]
        store.seenThrough = .distantFuture
        let mine = task("d1", "action", due: "2026-10-25T18:00:00+08:00")
        store.inbox.tasks = [mine, task("d2", "action", due: "2026-10-24T18:00:00+08:00")]
        expect(store.workGroups().map(\.id) == ["week"] && store.items(0).map(\.id) == ["d2", "d1"], "deadline from 整理")
        expect(store.setDue(mine, parseDate("2026-10-22T17:00:00+08:00")!), "set own deadline")
        expect(store.actions["d1"]?.dueOverride == "2026-10-22T17:00:00+08:00", "override stored in app time zone \(store.actions["d1"]?.dueOverride ?? "nil")")
        expect(store.workGroups().first?.tasks.map(\.id) == ["d1"] && store.workGroups().first?.id == "today", "override moves card to 今天优先")
        expect(store.items(0).map(\.id) == ["d1", "d2"], "override changes ordering")
        let dueNotice = store.scheduledNotices().first { $0.todoId == "d1" }
        expect(dueNotice?.kind == .due && dueNotice?.fireAt == parseDate("2026-10-22T16:45:00+08:00"), "due notice 15 minutes before new deadline")
        expect(store.handoffPrompt(mine).contains("10月22日 17:00（我自己改的）"), "hand-off shows own deadline")
        store.undoLast()
        expect(store.actions["d1"] == nil && store.workGroups().map(\.id) == ["week"], "undo removes override")
        store.setDue(mine, parseDate("2026-10-23T09:00:00+08:00")!)
        store.markHandoff(mine)
        expect(store.complete(mine, reason: "办完了", note: ""), "complete with override")
        expect(store.actions["d1"]?.dueOverride == "2026-10-23T09:00:00+08:00" && store.actions["d1"]?.handoffAt != nil, "completion keeps override and hand-off")
        store.restore(mine)
        store.setDue(mine, nil)
        expect(store.actions["d1"]?.dueOverride == nil && store.due(mine) == parseDate("2026-10-25T18:00:00+08:00"), "clear goes back to original deadline")
        store.actions = [:]
        store.setDue(mine, parseDate("2026-10-23T09:00:00+08:00")!)
        store.setDue(mine, nil)
        expect(store.actions["d1"] == nil, "clearing an override-only entry removes it")
        expect(store.dueChoices.map(\.label) == ["今天 18:00", "明天 18:00", "下周一 09:00"], "deadline quick choices")
        // Snooze notice: fires at until, carries the snooze actions, cancelled by undo, restore and completion
        store.actions = [:]
        let later = task("s1", "action")
        store.inbox.tasks = [later]
        func snoozeNotices(_ s: TodoStore = store) -> [PlannedNotice] { s.scheduledNotices().filter { $0.kind == .snooze } }
        store.snooze(later, until: store.snoozeChoices[1].date, label: "明天 09:00")
        let sn = snoozeNotices()
        expect(sn.count == 1 && sn[0].fireAt == parseDate("2026-10-23T09:00:00+08:00") && sn[0].todoId == "s1", "snooze notice fires at until")
        expect(sn.first?.category == NoticeAction.snoozeCategory && sn.first?.title == "回到待办：s1", "snooze notice offers 完成 / 再推 1 小时")
        let reopened = TodoStore(root: dir, live: false)
        reopened.now = store.now; reopened.inbox.tasks = [later]
        expect(snoozeNotices(reopened) == sn, "notices rebuilt from actions.json after restart")
        store.undoLast()
        expect(snoozeNotices().isEmpty, "undo cancels snooze notice")
        store.snooze(later, until: store.snoozeChoices[0].date, label: "1 小时后")
        store.restore(later)
        expect(snoozeNotices().isEmpty, "restore cancels snooze notice")
        store.snooze(later, until: store.snoozeChoices[0].date, label: "1 小时后")
        store.complete(later, reason: "办完了", note: "")
        expect(snoozeNotices().isEmpty, "completion cancels snooze notice")
        store.snooze(later, until: store.now.addingTimeInterval(1800), label: "")
        store.snooze(later, until: store.now.addingTimeInterval(3600), label: "1 小时后")
        expect(store.actions["s1"]?.until == store.now.timeIntervalSince1970 + 3600 && snoozeNotices().count == 1
               && snoozeNotices().first?.fireAt == store.now.addingTimeInterval(3600), "再推 1 小时 writes a new until and one notice")
        store.muted = [.snooze]
        expect(snoozeNotices().isEmpty, "snooze notices can be turned off")
        store.muted = [.due]
        store.inbox.tasks = [later, mine]
        expect(store.scheduledNotices().allSatisfy { $0.kind != .due } && snoozeNotices().count == 1, "due notices off independently")
        store.muted = []
        // Morning summary: first successful run of the day, once a day, own work only, same numbers as the header
        store.actions = [:]
        store.now = parseDate("2026-10-22T09:05:00+08:00")!
        store.inbox.tasks = [task("m1", "action", priority: 0), task("m2", "action", due: "2026-10-21T18:00:00+08:00"),
                             task("m3", "waiting"), task("m4", "action", due: "2026-10-24T18:00:00+08:00"), task("m5", "action", title: "待确认：线索")]
        var shownDay: String?
        store.persistSummaryDay = { shownDay = $0 }
        store.summaryShownDay = nil
        store.inbox.sync = SyncInfo(state: "ok", message: nil, scannedThrough: "2026-10-21T15:00:40+08:00", scope: nil, schedule: nil, mode: nil, runThreadId: nil)
        expect(store.takeMorningSummary() == nil && shownDay == nil, "no summary before today's run")
        store.inbox.sync = SyncInfo(state: "error", message: nil, scannedThrough: "2026-10-22T09:00:40+08:00", scope: nil, schedule: nil, mode: nil, runThreadId: nil)
        expect(store.takeMorningSummary() == nil && shownDay == nil, "failed run shows no summary")
        store.inbox.sync?.state = "ok"
        let morning = store.takeMorningSummary()
        expect(morning?.body == "今天 2 件 · 逾期 1 件 · 本周 1 件 · 等回复 1 件", "summary counts \(morning?.body ?? "nil")")
        expect(store.summaryText == "今天2 · 本周1 · 等回复1 · 待确认1", "header agrees \(store.summaryText)")
        expect(shownDay == "2026-10-22" && store.takeMorningSummary() == nil, "only once a day")
        store.now = parseDate("2026-10-22T15:05:00+08:00")!
        store.inbox.sync?.scannedThrough = "2026-10-22T15:00:40+08:00"
        expect(store.takeMorningSummary() == nil, "later run the same day stays quiet")
        store.now = parseDate("2026-10-23T09:05:00+08:00")!
        store.inbox.sync?.scannedThrough = "2026-10-23T09:00:20+08:00"
        store.inbox.tasks = [task("m3", "waiting")]
        expect(store.takeMorningSummary() == nil && shownDay == "2026-10-23", "nothing of my own: no summary, day still handled")
        // 催一下: confirmed waiting items from 3 days, copied text within 200 characters without secrets, notified once
        store.now = parseDate("2026-10-22T10:00:00+08:00")!
        store.actions = [:]
        let asked = Source(chat: "设备采购群", time: "2026-10-18 09:30", quote: "请确认到货，密码：Abc12345，表单 https://x.cn/f?token=zzz\n谢谢",
                           messageId: "m1", conversationId: "c", senderName: "我", senderId: "self", recipientName: "叶龙")
        let replied = Source(chat: "设备采购群", time: "2026-10-19 11:00", quote: "收到", messageId: "m2", conversationId: "c",
                             senderName: "叶龙", senderId: "u-ye", recipientName: "我")
        let w4 = task("w4", "waiting", title: "等叶龙确认到货", sources: [asked, replied])
        let w2 = task("w2", "waiting", sources: [Source(chat: "c", time: "2026-10-20 09:00", quote: "q", messageId: "m", conversationId: "c")])
        let wc = task("wc", "waiting", title: "待确认：陈盈是否到货", sources: [asked])
        store.inbox.tasks = [w4, w2, wc]
        expect(store.canNudge(w4) && !store.canNudge(w2) && !store.canNudge(wc), "nudge only for confirmed waiting from 3 days")
        expect(store.nudgeTarget(w4) == "设备采购群", "unknown own ID: chat name")
        expect(store.nudgeTarget(w4, me: "self") == "叶龙" && store.nudgeTarget(task("x", "waiting", sources: [asked]), me: "self") == "叶龙", "other side of latest known message")
        let nudge = store.nudgeText(w4)
        expect(nudge.count <= 200 && nudge.hasPrefix("设备采购群，") && nudge.contains("等了 4 天") && nudge.contains("原消息（10/18）"), "nudge text \(nudge)")
        expect(!nudge.contains("Abc12345") && !nudge.contains("token") && !nudge.contains("\n谢谢"), "nudge text has no secrets \(nudge)")
        let longOne = task("wl", "waiting", title: String(repeating: "长", count: 140),
                           sources: [Source(chat: String(repeating: "群", count: 80), time: "2026-10-10 09:00", quote: String(repeating: "字", count: 600), messageId: "m", conversationId: "c")])
        expect(store.nudgeText(longOne).count <= 200, "long nudge clipped to 200")
        store.now = parseDate("2026-10-22T07:00:00+08:00")!
        expect(store.takeNudges().isEmpty && store.actions.isEmpty, "no nudge notification before the first run hour")
        store.now = parseDate("2026-10-22T10:00:00+08:00")!
        let firstNudges = store.takeNudges()
        expect(firstNudges.map(\.todoId) == ["w4"] && firstNudges.first?.title == "设备采购群 的回复已等 4 天", "nudge notice \(firstNudges.map(\.title))")
        expect(store.actions["w4"]?.nudgedAt != nil && store.status(w4) == "active", "nudgedAt recorded without changing status")
        expect(store.takeNudges().isEmpty, "each item notified once")
        store.actions = [:]
        store.inbox.tasks = (1...5).map { i in task("b\(i)", "waiting", sources: [asked]) }
        let batch = store.takeNudges()
        expect(batch.count == 1 && batch.first?.title == "5 件等回复已满 3 天" && store.actions.count == 5, "many at once: one combined notice")
        store.actions = [:]
        store.muted = [.nudge]
        expect(store.takeNudges().isEmpty && store.actions.isEmpty, "muted nudges are not recorded")
        store.muted = []
        // Notification click target
        store.inbox.tasks = [w4]
        store.requestFocus("missing")
        expect(store.focus == nil, "unknown card is ignored")
        store.requestFocus("w4", complete: true)
        expect(store.focus?.id == "w4" && store.focus?.complete == true && store.tab(for: w4) == 0, "focus request")
        // Changelog
        let log = parseChangelog("# 更新日志\n\n说明文字\n\n## 2026-10-23\n- 稍后到点会弹窗\n  并可再推 1 小时\n- 可以自己改期限\n\n## 2026-10-21\n* 首版\n")
        expect(log.map(\.title) == ["2026-10-23", "2026-10-21"] && log[0].items == ["稍后到点会弹窗并可再推 1 小时", "可以自己改期限"] && log[1].items == ["首版"], "changelog parse \(log)")
        expect(parseChangelog("").isEmpty && parseChangelog("没有标题\n- 孤立条目").isEmpty, "no changelog is fine")
        if failed > 0 { exit(1) }
        print("PASS: state, groups, reasons, undo, snooze, late runs, hand-off link, report link, newest update, run record, copied messages, archive search, new badge, own deadline, snooze/due notices, morning summary, nudge, focus, changelog")
    }
}
'''


def verify(source):
    text = source.read_text()
    marker = '// MARK: - UI'
    if marker not in text:
        raise SystemExit(f'{source} 缺少 {marker} 分隔')
    with tempfile.TemporaryDirectory(prefix='todo-store-check-') as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / 'check.swift').write_text(text.split(marker)[0] + CHECKS)
        subprocess.run(['swiftc', '-parse-as-library', str(tmp / 'check.swift'), '-o', str(tmp / 'check'),
                        '-framework', 'AppKit', '-framework', 'SwiftUI'], check=True)
        data = tmp / 'data'
        data.mkdir()
        subprocess.run([str(tmp / 'check'), str(data)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=pathlib.Path, default=next((p for p in CANDIDATES if p.exists()), CANDIDATES[0]))
    verify(parser.parse_args().source)
