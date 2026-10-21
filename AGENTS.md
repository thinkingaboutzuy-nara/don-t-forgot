# 别忘了（本机钉钉待办悬浮窗）

Mac 原生悬浮窗，显示从钉钉聊天整理出的个人待办；Codex 每天 09:00、15:00 整理一次。本文件是维护入口，`CLAUDE.md` 是指向它的链接。

## 文件

| 路径 | 作用 |
|---|---|
| `TodoWindow.swift` | 窗口全部源码。`// MARK: - UI` 以上是数据与状态逻辑（测试只编译这一段），以下是界面 |
| `update_inbox.py` | inbox 唯一写入口：校验、加锁、原子替换、封存；`--open` 给整理用的精简视图 |
| `build_app.py` | 构建并签名 `别忘了.app`，旧版保留为 `.app.previous` |
| `config.json` | 本机实例配置（账号、路径、时区、排程、日期下限），构建时打包进 app |
| `整理规则.md` | 定时整理的唯一现行规则 |
| `使用说明.md` | 给用户看的操作说明 |
| `tests/` | `verify_store.py`（窗口状态逻辑）、`verify_merge.py`（合并器），都只用临时合成数据 |
| `package_skill.sh` | 重新生成 `技能分享/钉钉待办悬浮窗-skill.zip` |
| `README.md`、`config.example.json`、`.gitignore` | 仓库首页、配置模板、不进仓库的本机内容清单 |
| `work/runs/` | 每轮整理的增量和摘要，只留最近 6 轮 |
| `历史/` | 2026-10-20 首版的规则、计划、交接说明和旧技能包，仅供追溯 |

## 运行时

- 数据目录：`~/Library/Application Support/DingTalkTodo/`
  - `inbox.json`：整理端写，只能经 `update_inbox.py`
  - `actions.json`：窗口写，记录完成、忽略、稍后的状态和理由，以及“交给 Codex”的时间
  - `archive/`：收起超过 30 天的事项
- 登录启动：`~/Library/LaunchAgents/local.vn.dingtalk-todo.plist`，Label 与 bundle ID 都是 `local.vn.dingtalk-todo`。设为 `KeepAlive.SuccessfulExit=false`：异常退出会被拉起，用户从菜单退出则不会。
- 日报平台：经 Codex 已登记的 `work-reports` MCP（本机转接程序 `工作日報自動化/var/report-mcp-production-candidate-20261020/mcp_client.py`）读写本人待办。`~/.codex/config.toml` 只对 `get_todos`、`set_todo_done` 设了 `approval_mode = "approve"`，让定时整理无人值守也能同步；存草稿、提交仍需核准。平台端的 `set_todo_done` 于 2026-10-21 发布，记录在 `工作日報自動化/evidence/20261021-别忘了待办同步/`。
- 定时任务：`~/.codex/automations/automation-8/automation.toml`，`kind = "cron"`、`auto_archive = true`、模型 `gpt-6.1-sol`，每轮新开对话执行，prompt 只指向 `整理规则.md`。旧的承载对话 `01a15832-…` 已不再接收定时任务。

## 技能包与源码

已安装技能 `~/.codex/skills/mac-dingtalk-todo-window` 的这些文件是指向本目录的符号链接，源码只有本目录这一份：

- `assets/native-app/TodoWindow.swift`、`assets/native-app/update_inbox.py`
- `scripts/build_app.py`、`scripts/verify_merge.py`、`scripts/verify_store.py`

技能包里的通用说明（`SKILL.md`、`references/`）不含本机账号。改了这些文件或源码后，运行 `./package_skill.sh` 更新分享包；打包时会把链接展开成真实文件。

## 修改流程

1. **改界面或状态逻辑**：编辑 `TodoWindow.swift`。状态优先级在 Swift 的 `TodoStore.status` 和 Python 的 `effective_status` 各有一份，两边要一起改。
2. **验证**：运行 `python3 tests/verify_store.py && python3 tests/verify_merge.py`。界面改动另用 `--test-mode --data-dir <临时目录>` 启动副本，只检查受影响的操作。
3. **构建并重启**：运行 `python3 build_app.py`；如有旧的 `别忘了.app.previous`，确认无用后先删除。然后运行 `launchctl kickstart -k gui/$(id -u)/local.vn.dingtalk-todo`。
4. **改整理规则**：直接改写 `整理规则.md` 的对应条目，不追加补丁段落；定时任务的 prompt 不需要动。
5. **同步到 Gitea**（用户要求每次修复都同步）：`git add -A && git commit && git push`。远端地址见 `git remote -v`。
   - 提交前用 `git status` 确认没有 `config.json`、`整理规则.md`、`work/` 等本机内容；这些由 `.gitignore` 排除，不要强行加入。
   - 改了 `config.json` 的字段时，同步更新 `config.example.json`。

日常整理不改这里的任何文件（见 `整理规则.md` 的边界）。

## 当前状态（2026-10-21）

- 2026-10-21 已完成长期化改造：
  - 迁到本目录；源码统一为一份
  - 定时任务改为每轮新开对话，并从每次全量回扫改为增量（每周一完整回扫）
  - 新增完成/忽略理由与完成动画、待确认分组（7 天无进展自动收起）、等回复天数、到期前 15 分钟通知、“交给 Codex”按钮、30 天封存
- 已与日报平台连动：日报待办双向同步完成状态（事项上的 `reportTodo`，平台改动较晚时以平台为准），底部“整理日报”把今天完成的事交给 Codex 写成日报草稿。
- 自动派发已停用，改为浮窗按钮由用户发起。此前已派发的事项（Holy蟹 `dingtalk-todo-58113415434` 等）保留 `dispatch` 字段，整理时继续核对其进度。
- 旧路径 `Codex/2026-10-20/i-ll-start-by-loading-the/outputs/钉钉待办` 是指向本目录的链接，旧对话里的引用仍可打开。
