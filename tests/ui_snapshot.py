#!/usr/bin/env python3
"""Screenshot the real window against a data copy, driving it in-process (no screen-recording permission needed).

python3 tests/ui_snapshot.py --data <dir with inbox.json/actions.json> --out <dir> \
    --steps "click,300,83;wait,0.8;snap,archived;key,cmdf;key,手机;wait,0.6;snap,search"

Steps: snap,<name> | click,<x>,<y from top, points> | key,<text> or key,cmd<char> (e.g. cmdf, cmd=) | wait,<seconds>
     | notify,<open|complete|snooze-1h>,<task id> (as if that notification, or its button, was clicked).
snap also saves other visible windows, such as the ⓘ popover, as <name>-window<N>.png.
The data dir is used as-is, so pass a copy or tests/ui_fixture.py output, never the live data directory.
"""
import argparse
import pathlib
import plistlib
import shutil
import subprocess
import tempfile

PROJECT = pathlib.Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit
import SwiftUI
@MainActor func snap(_ d: AppDelegate, _ name: String) {
    func save(_ w: NSWindow, _ file: String) {
        guard let v = w.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) else { return }
        v.cacheDisplay(in: v.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: CommandLine.arguments.last! + "/" + file + ".png"))
    }
    save(d.panel, name)
    for (i, w) in NSApp.windows.enumerated() where w !== d.panel && w.isVisible && w.frame.width > 40 { save(w, "\(name)-window\(i)") }
}
@MainActor func click(_ d: AppDelegate, _ x: CGFloat, _ yTop: CGFloat) {
    let p = NSPoint(x: x, y: d.panel.frame.height - yTop)
    for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
        if let e = NSEvent.mouseEvent(with: type, location: p, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                      windowNumber: d.panel.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) { d.panel.sendEvent(e) }
    }
}
@MainActor func key(_ d: AppDelegate, _ text: String) {
    d.panel.makeKey()
    let command = text.hasPrefix("cmd") && text.count == 4
    let chars = command ? String(text.suffix(1)) : text
    if !command, let editor = d.panel.firstResponder as? NSTextView { editor.insertText(chars, replacementRange: editor.selectedRange()); return }
    if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: command ? [.command] : [], timestamp: ProcessInfo.processInfo.systemUptime,
                                windowNumber: d.panel.windowNumber, context: nil, characters: chars, charactersIgnoringModifiers: chars, isARepeat: false, keyCode: 3) {
        if command { _ = d.panel.performKeyEquivalent(with: e) } else { d.panel.sendEvent(e) }
    }
}
@main struct Harness {
    @MainActor static func main() {
        let app = NSApplication.shared; let d = AppDelegate(); app.delegate = d
        var t = 1.5
        for step in (ProcessInfo.processInfo.environment["STEPS"] ?? "").split(separator: ";").map(String.init) {
            let parts = step.split(separator: ",", maxSplits: 2).map(String.init)
            switch parts[0] {
            case "snap": DispatchQueue.main.asyncAfter(deadline: .now() + t) { snap(d, parts[1]) }
            case "click": DispatchQueue.main.asyncAfter(deadline: .now() + t) { click(d, CGFloat(Double(parts[1])!), CGFloat(Double(parts[2])!)) }
            case "key": DispatchQueue.main.asyncAfter(deadline: .now() + t) { key(d, parts[1]) }; t += 0.2
            case "notify": DispatchQueue.main.asyncAfter(deadline: .now() + t) {
                d.handleNotification(action: parts[1] == "open" ? "com.apple.UNNotificationDefaultActionIdentifier" : parts[1], todoId: parts[2])
            }
            case "wait": t += Double(parts[1])!
            default: break
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + t + 0.5) { NSApp.terminate(nil) }
        withExtendedLifetime(d) { app.run() }
    }
}
'''


def run(data, out, steps):
    app = PROJECT / '别忘了.app'
    if not app.exists():
        raise SystemExit('先运行 build_app.py 生成 别忘了.app（截图需要它的 config.json）')
    if data.resolve() == pathlib.Path('~/Library/Application Support/DingTalkTodo').expanduser().resolve():
        raise SystemExit('不要直接用正式数据目录，请先复制一份')
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bwl-ui-') as tmp:
        tmp = pathlib.Path(tmp)
        source = (PROJECT / 'TodoWindow.swift').read_text().replace('@main struct TodoApplication', 'struct TodoApplication')
        (tmp / 'app.swift').write_text(source)
        (tmp / 'harness.swift').write_text(HARNESS)
        bundle = tmp / 'Snapshot.app'
        shutil.copytree(app, bundle, symlinks=True)
        # Own bundle ID, so card lengths or other preferences changed during a snapshot never reach the real window.
        info = bundle / 'Contents/Info.plist'
        plist = plistlib.loads(info.read_bytes())
        plist['CFBundleIdentifier'] += '.snapshot'
        info.write_bytes(plistlib.dumps(plist))
        binary = bundle / 'Contents/MacOS/TodoWindow'
        subprocess.run(['swiftc', '-parse-as-library', str(tmp / 'app.swift'), str(tmp / 'harness.swift'), '-o', str(binary),
                        '-framework', 'AppKit', '-framework', 'SwiftUI'], check=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(bundle)], check=True, capture_output=True)
        subprocess.run([str(binary), '--test-mode', '--data-dir', str(data), str(out.resolve())],
                       env={'STEPS': steps, 'PATH': '/usr/bin:/bin'}, check=True)
    print('\n'.join(sorted(str(p) for p in out.glob('*.png'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', type=pathlib.Path, required=True)
    parser.add_argument('--out', type=pathlib.Path, required=True)
    parser.add_argument('--steps', default='snap,window')
    a = parser.parse_args()
    run(a.data, a.out, a.steps)
