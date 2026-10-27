# 别忘了

Mac 原生悬浮窗，显示从钉钉聊天整理出的个人待办。整理由 Codex 定时任务每天执行两次；窗口的显示、勾选和恢复在本机独立运行。

- 怎么用：[使用说明.md](使用说明.md)
- 怎么维护：[AGENTS.md](AGENTS.md)（`CLAUDE.md` 指向同一文件）
- 改了什么：[更新日志.md](更新日志.md)
- 接下来要做：[开发计划.md](开发计划.md)

## 构建

需要 macOS 14+、Xcode Command Line Tools、Python 3.9+。

```sh
cp config.example.json config.json   # 填写本机路径、dws profile 与本人 ID
python3 tests/verify_store.py         # 窗口状态逻辑
python3 tests/verify_merge.py         # 清单合并器
python3 tests/verify_review.py        # 整理续接与完成检查
python3 tests/verify_stop_guard.py    # 自动续接、真实阻塞与无进展边界
python3 build_app.py                  # 生成并签名 别忘了.app
```

## 整理入口

流程分为读取、判断、回填。`review_run.py start`恢复或创建原轮次；fetch阶段用`fetch-command --run <run>`生成原生DWS命令并原样执行，直到进入review。聊天分段完整阅读并ack后，只填写UTF-8判断JSON：summary、tasks中的原ID及变更字段、引用的messageIndexes、checks中的实际核对结论。

```sh
python3 review_run.py stage --run <run> --input <本轮decisions.json>
python3 review_run.py finish --run <run>
```

stage固定保留旧字段与来源、补齐消息依据和方向并生成候选，不写清单；finish检查完整性后合并并同ID读回。无需临时编写业务拼接程序。账号、范围或输入错误会保留原轮次与水位；AI仍需真正阅读和判断，不能用程序自动确认未读消息。

### 自动结束检查

本机安装并信任Codex `Stop` hook后，将实例配置的`requireAutoCompletion`设为true。hook命令为`python3 <项目绝对路径>/review_stop.py --project <项目绝对路径>`；配置示例默认关闭，避免未安装hook却误称已启用。

`start`用运行时的`CODEX_THREAD_ID`绑定本轮。结束检查只处理该会话，其他聊天不会续接。未完成时返回原run的下一步；完成旧断点后只补一次至本次启动时间。每次有分页、审阅或核对进展会重置无进展计数；连续三次没有进展则保留断点并报告。

真正的外部故障用`fail --run <run> --reason <原因和下一动作> --evidence <本轮错误JSON>`登记。去敏JSON须包含`kind`（authentication/source/environment/identity/permission）、`operation`、`status: "error"`及实际`error`。单纯的“尚待审阅”不构成停止依据。证据改变、进度变化或换了接手会话后，旧故障不能直接解除结束检查。

## 不在仓库里的内容

- `config.json`：本机账号身份和路径
- `整理规则.md`：含个人业务事实的整理规则
- 运行数据：位于 `~/Library/Application Support/DingTalkTodo/`

以上内容都只保存在使用者本机。整理规则的通用写法见 Codex 技能 `mac-dingtalk-todo-window` 的 `references/接入与定时整理.md`。
