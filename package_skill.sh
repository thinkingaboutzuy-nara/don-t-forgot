#!/bin/zsh
# Rebuild the shareable skill zip. zip follows symlinks, so the package carries real files from this project.
set -euo pipefail
cd -- "${0:A:h}"
skills="$HOME/.codex/skills"
out="$PWD/技能分享/钉钉待办悬浮窗-skill.zip"
mkdir -p "${out:h}"
rm -f "$out"
(cd "$skills" && zip -qr "$out" mac-dingtalk-todo-window -x '*/__pycache__/*' '*/.DS_Store')
shasum -a 256 "$out"
