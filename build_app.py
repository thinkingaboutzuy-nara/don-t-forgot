#!/usr/bin/env python3
"""Build and ad-hoc sign the app without launching it, then install a copy into /Applications (or ~/Applications)
so it shows up in Launchpad, Spotlight and Finder. --no-install builds only (CI, screenshots)."""
import argparse
import json
import os
import pathlib
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import time


def changelog_version(text):
    """Version = date of the newest `## YYYY-MM-DD` heading in 更新日志.md, e.g. 2026.10.23."""
    match = re.search(r'^## +(\d{4})-(\d{2})-(\d{2})', text, re.M)
    return '.'.join(str(int(part)) for part in match.groups()) if match else '1.0'


# Rounded green tile with the same checklist symbol the window header uses.
ICON_SWIFT = r'''
import AppKit
let size = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let tile = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
NSGradient(starting: NSColor(red: 0.11, green: 0.36, blue: 0.30, alpha: 1), ending: NSColor(red: 0.24, green: 0.60, blue: 0.50, alpha: 1))!
    .draw(in: tile, angle: 90)
let config = NSImage.SymbolConfiguration(pointSize: 420, weight: .semibold).applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
if let symbol = NSImage(systemSymbolName: "checklist", accessibilityDescription: nil)?.withSymbolConfiguration(config) {
    let s = symbol.size
    symbol.draw(in: NSRect(x: (CGFloat(size) - s.width) / 2, y: (CGFloat(size) - s.height) / 2, width: s.width, height: s.height))
}
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
'''


def make_icon(work, resources):
    """Render the 1024 px tile, scale it into an .iconset and pack AppIcon.icns."""
    (work / "icon.swift").write_text(ICON_SWIFT)
    subprocess.run(["swiftc", str(work / "icon.swift"), "-o", str(work / "icon")], check=True)
    master = work / "icon.png"
    subprocess.run([str(work / "icon"), str(master)], check=True)
    iconset = work / "AppIcon.iconset"
    iconset.mkdir()
    for points in [16, 32, 128, 256, 512]:
        for scale in [1, 2]:
            name = f"icon_{points}x{points}" + ("@2x" if scale == 2 else "") + ".png"
            px = str(points * scale)
            subprocess.run(["sips", "-z", px, px, str(master), "--out", str(iconset / name)], check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")], check=True)


def install(app, bundle_id):
    """Copy the built app into /Applications, or ~/Applications when /Applications is not writable."""
    targets = [pathlib.Path("/Applications"), pathlib.Path.home() / "Applications"]
    target = next((t for t in targets if t.is_dir() and os.access(t, os.W_OK)), targets[1])
    target.mkdir(exist_ok=True)
    dest = target / app.name
    if dest.exists():
        old = plistlib.loads((dest / "Contents/Info.plist").read_bytes())
        if old.get("CFBundleIdentifier") != bundle_id:
            raise ValueError(f"{dest} 是另一个应用，停止覆盖")
        shutil.rmtree(dest)
    subprocess.run(["ditto", str(app), str(dest)], check=True)
    lsregister = pathlib.Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
    if lsregister.exists():
        subprocess.run([str(lsregister), "-f", str(dest)], check=False)
    return dest


def launch_agent_hint(bundle_id, installed):
    """The login item may still start the copy in the project folder; say so instead of editing it."""
    plist = pathlib.Path.home() / "Library/LaunchAgents" / f"{bundle_id}.plist"
    if not plist.exists():
        return None
    args = " ".join(plistlib.loads(plist.read_bytes()).get("ProgramArguments", []))
    if str(installed) in args:
        return None
    return f"提示：登录启动项 {plist} 仍指向其他位置（{args}）；可改为启动 {installed}。"


def build(project, install_app=True):
    if platform.system() != "Darwin":
        raise RuntimeError("此原生应用必须在 macOS 构建")
    arch = platform.machine()
    if arch not in ["arm64", "x86_64"]:
        raise RuntimeError(f"未支持的构建架构：{arch}")
    for tool in ["swiftc", "codesign"]:
        if not shutil.which(tool):
            raise RuntimeError(f"找不到 {tool}；先检查 Xcode Command Line Tools")
    project = project.resolve()
    config = json.loads((project / "config.json").read_text())
    name = config["appName"]
    if not name or "/" in name or "\\" in name or name in [".", ".."]:
        raise ValueError("无效 appName")
    app = project / (name + ".app")
    if app.exists():
        # Refuse to remove or replace an app whose identity does not match this project.
        old = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if old.get("CFBundleIdentifier") != config["bundleIdentifier"]:
            raise ValueError("现有应用身份不一致，停止覆盖")
    with tempfile.TemporaryDirectory(prefix=".build-", dir=project) as tmp:
        staged = pathlib.Path(tmp) / app.name
        contents = staged / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        make_icon(pathlib.Path(tmp), contents / "Resources")
        shutil.copy2(project / "config.json", contents / "Resources/config.json")
        log = project / "更新日志.md"
        version = "1.0"
        if log.exists():
            shutil.copy2(log, contents / "Resources/changelog.md")
            version = changelog_version(log.read_text())
        info = {
            "CFBundleDisplayName": name, "CFBundleName": name, "CFBundleExecutable": "TodoWindow",
            "CFBundleIdentifier": config["bundleIdentifier"], "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version, "CFBundleVersion": time.strftime("%Y%m%d%H%M"),
            "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True, "CFBundleIconFile": "AppIcon",
            "LSApplicationCategoryType": "public.app-category.productivity",
        }
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(["swiftc", "-target", f"{arch}-apple-macos14.0", "-parse-as-library",
                        str(project / "TodoWindow.swift"), "-o", str(contents / "MacOS/TodoWindow"),
                        "-framework", "AppKit", "-framework", "SwiftUI"], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(staged)], check=True)
        subprocess.run(["codesign", "--verify", "--strict", str(staged)], check=True)
        if app.exists():
            backup = project / (app.name + ".previous")
            if backup.exists():
                raise FileExistsError("已有 .previous 备份；先核对其用途，不自动覆盖")
            app.rename(backup)
        staged.rename(app)
    if not install_app:
        return str(app)
    installed = install(app, config["bundleIdentifier"])
    hint = launch_agent_hint(config["bundleIdentifier"], installed)
    return f"{app}\n已安装到 {installed}" + (f"\n{hint}" if hint else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent)
    parser.add_argument("--no-install", action="store_true", help="只在项目目录构建，不复制到“应用程序”")
    args = parser.parse_args()
    print(build(args.project, install_app=not args.no_install))
