# 一次唤醒回合

> 手册在本会话的 system prompt 里（`prompts/append.md`）；本卡只写"这一回合"的增量。

## 本回合

1. `python3 bin/lock.py acquire || exit 0` —— 取不到就立刻退出、回一行。
2. 按下面「待处理事件」逐条处置（kind→处置对照：`docs/architecture.md`），然后
   `python3 bin/inbox.py ack <id> …`。要事件全文（含 `lastAssistantText`）：
   `python3 bin/inbox.py list --json --limit 5`。
3. 收尾：`journal/` 一行、替他做的决策进 `reports/decisions/`、`python3 bin/lock.py release`、
   `git status --short`，有改动就走 worktree + PR（**PR 与合并自动，不问用户**）。
