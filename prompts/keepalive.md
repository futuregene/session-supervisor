# 守护自愈（保活检查）

唯一目标：确认 `bin/watchd.sh` 守护进程在跑；不在跑就把它拉起来。工作目录已经是这个仓库。

1. **只读检查**：`python3 bin/liveness.py`
2. **退出码 0 ⇒ 健康，到此为止**。不写文件、不发消息、不读别的会话记录、不跑 selfcheck、不看 git。
3. **退出码非 0 ⇒ 自愈**：
   - `python3 bin/liveness.py --ensure`
   - 等 20 秒，再跑一次 `python3 bin/liveness.py` 复查
   - 无论复查成败，往 `journal/<本地今天的日期>.md` **追加**一行（文件不存在就先建，并在首行写 `# <日期>`）：
     `- HH:MM 自愈：watchd 不在跑，已尝试拉起。原报错：<liveness 第一行原文>；复查：<复查结论原文>`
   - 复查仍失败时，再执行一次
     `python3 bin/notice.py add --kind incident --text "<把上面那行压成一句话>"`
4. **红线**：不改任何定时任务、不动系统计划任务、不换二进制、不给别的会话发消息、不 ack 事件、
   不改 `bin/` `docs/` `AGENTS.md` `POLICY.md`、不提交 git。

## 结束时这样汇报（这段会成为本次运行的 summary）

固定三行，不加别的：

- 结论：健康 / 已拉起 / 仍失败
- 证据：`<第 1 步或第 3 步 liveness 输出的那一句原文>`
- 动作：无 / journal 追加一行 / journal + notice
