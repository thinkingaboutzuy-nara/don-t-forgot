# 别忘了

Mac 原生悬浮窗，显示从钉钉聊天整理出的个人待办。整理由 Codex 定时任务每天执行两次；窗口的显示、勾选和恢复在本机独立运行。

- 怎么用：[使用说明.md](使用说明.md)
- 怎么维护：[AGENTS.md](AGENTS.md)（`CLAUDE.md` 指向同一文件）

## 构建

需要 macOS 14+、Xcode Command Line Tools、Python 3.9+。

```sh
cp config.example.json config.json   # 填写本机路径、dws profile 与本人 ID
python3 tests/verify_store.py         # 窗口状态逻辑
python3 tests/verify_merge.py         # 清单合并器
python3 build_app.py                  # 生成并签名 别忘了.app
```

## 不在仓库里的内容

- `config.json`：本机账号身份和路径
- `整理规则.md`：含个人业务事实的整理规则
- 运行数据：位于 `~/Library/Application Support/DingTalkTodo/`

以上内容都只保存在使用者本机。整理规则的通用写法见 Codex 技能 `mac-dingtalk-todo-window` 的 `references/接入与定时整理.md`。
