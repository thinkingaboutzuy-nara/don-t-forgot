#!/usr/bin/env python3
"""Build and ad-hoc sign a generated native macOS project without launching it."""
import argparse
import json
import pathlib
import platform
import plistlib
import shutil
import subprocess
import tempfile


def build(project):
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
        shutil.copy2(project / "config.json", contents / "Resources/config.json")
        info = {
            "CFBundleDisplayName": name, "CFBundleName": name, "CFBundleExecutable": "TodoWindow",
            "CFBundleIdentifier": config["bundleIdentifier"], "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "14.0", "LSUIElement": True, "NSHighResolutionCapable": True,
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
    return str(app)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent)
    print(build(parser.parse_args().project))
