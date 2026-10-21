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
        expect(store.workGroups().last?.collapsible == true, "confirm group collapsible")
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
        // Daily report hand-off lists today's completions with reasons
        store.inbox.tasks = [task("a", "action", title: "交付周报"), task("b", "action", title: "核对编码"), task("c", "action", title: "昨天的事")]
        store.actions = ["a": LocalAction(status: "done", until: nil, updatedAt: "2026-10-21T08:10:00Z", reason: "办完了", note: "已发群里"),
                         "b": LocalAction(status: "done", until: nil, updatedAt: "2026-10-21T08:20:00Z", reason: "对方已处理"),
                         "c": LocalAction(status: "done", until: nil, updatedAt: "2026-10-20T08:00:00Z", reason: "办完了")]
        expect(store.completedToday.map(\.id) == ["b", "a"], "completed today, newest first \(store.completedToday.map(\.id))")
        let report = store.reportPrompt()
        expect(report.contains("2026-10-21") && report.contains("交付周报") && report.contains("备注：已发群里") && !report.contains("昨天的事"), "report prompt content")
        expect(report.contains("submit_report") && report.contains("工时不要猜"), "report prompt boundaries")
        expect(store.reportURL?.absoluteString.hasPrefix("codex://threads/new?prompt=") == true, "report link")
        if failed > 0 { exit(1) }
        print("PASS: state, groups, reasons, undo, snooze, late runs, hand-off link, report link and daily report")
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
