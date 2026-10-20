# 会话总管 · 每日巡检简报（08:30）

你负责给用户一份 24 小时巡检简报。**只读 + 写简报，不要改动监控设施本身**，
不要向任何会话追加消息，不要批准权限。

工作目录：`~/session-supervisor`

## 步骤

1. 自检存活：
   - `python3 bin/liveness.py` —— 唯一需要的存活判据（健康文件新鲜 + `state/watchd.pid` 存活）。
     **不要用 `launchctl print gui/$(id -u)/<你的 label>`**：在总管自己的上下文里它只回
     `125: Domain does not support specified action`，**永远看起来都一样**，因此报不出"watcher 死了"。
     （2026-10-17 实测：03:11→08:32 停摆 5h20m 期间这条命令给不出任何信号，而那两条可用的判据当时都是绿的。）
   - 任一项不满足：`python3 bin/liveness.py --ensure` 直接把它拉起来（与人工 `bash bin/watchd.sh` 同一入口），
     若仍不健康，**在简报里标红**并说明 `--ensure` 也没救活（那才是真需要人介入的情况）。
2. 汇总过去 24 小时：
   - `python3 bin/inbox.py list --json`（未处理事件）、`tail -n 50 state/wakes.jsonl`、
     `python3 bin/guard.py show`、`cat journal/$(date +%F).md journal/$(date -v-1d +%F).md 2>/dev/null`。
   - `reports/on-your-behalf.md` 与 `state/approvals.jsonl`（自动批准了哪些）。
   - `git log --oneline --since=24.hours`（本设施昨天改了什么）。
   - 统计：唤醒次数（events/heartbeat）、向哪些会话追加了几条消息、发现几类异常。
3. `reports/needs-you.md` 里**未决**的条目原文列出（这是最重要的一节，放最前面）。
   - **顺手做收件箱卫生**：同一事项只留一条（原地更新）；带沉默天数的陈旧条目若已过期，
     按默认动作结清并移入"已处理"留档。
3b. **汇总 `state/notices.jsonl`**（POLICY §A.11 即时告知制）：
   - 若有内容，在简报"需你知情"一节逐条列出（时间 / kind / 一句话 / 回退方法）。
   - 读入后**归档并截断**：`mkdir -p reports/notices && cp state/notices.jsonl reports/notices/$(date +%F).md`
     然后把 `state/notices.jsonl` 清空（避免第二天重复推）。
4. 检查是否有会话长时间卡住（`stalled` 事件、或 health 里 active 数异常）。
5. 写出 `reports/daily-$(date +%F).md`，并在最终回复里给出 **≤12 行**摘要：
   存活状态 / 昨天做了什么 / 需要用户决定的 / 风险与建议。
6. **把简报推成 Life Journal 文档**（用户在手机上一页页读，2026-10-16 起）：

   ```bash
   tail -n +2 reports/daily-$(date +%F).md > /tmp/brief-$(date +%F).md   # 去掉与 title 重复的首行 H1
   life-docs push /tmp/brief-$(date +%F).md \
     --doc-id brief-$(date +%F) \
     --title "会话总管 · 每日巡检简报 $(date +%F)" \
     --tag 会话总管 --tag 巡检 --source task
   life-docs get brief-$(date +%F) --json > /tmp/brief-check.json      # 回读核对，push 的输出不算证据
   ```

   - **`--doc-id` 固定用 `brief-<YYYY-MM-DD>`**：同一天重推是**就地更新**同一篇，不产生多篇。
   - **正文里不要重复标题**（`title` 字段已承载它）——所以推的是**去掉首行 `# …` 的副本**，
     不要直接推 `reports/daily-<date>.md` 原文。
   - **写之前先确认连的是你在看的那台服务端**（判据：`life-docs list` 的时间戳带 `+0800` 通常是生产）。
     写错实例会「成功但手机一片空白」，属**静默失败**——拿不准就**不要推**，并在简报里说明原因。
   - **不要把 token 打进任何输出或参数**；推送失败**如实写进简报与最终回复**，不要假装成功。
   - 完整约定见 `life-journal-docs` 技能（`~/.future/agent/skills/life-journal-docs/SKILL.md`）。
7. 把本次简报按仓库的分支规范送进 `main`（**不要**在主 checkout 的 `main` 上直接 commit）：
   ```bash
   cd ~/session-supervisor
   python3 bin/lock.py acquire || echo "另一个回合在跑——稍后重试，别并发写"
   git fetch origin --prune
   git worktree add --no-track .worktrees/daily-$(date +%F) -b chore/daily-$(date +%F) origin/main
   ```
   在**该 worktree 里**写 `reports/daily-$(date +%F).md`（用绝对路径），然后：
   ```bash
   cd ~/session-supervisor/.worktrees/daily-$(date +%F)
   python3 bin/selfcheck.py                      # 必须全绿
   git add -A && git commit -m "docs(daily): $(date +%F) 巡检简报"
   git push -u origin chore/daily-$(date +%F)
   gh pr create --base main --fill
   gh pr checks <n> --watch --required           # 先确认 selfcheck 已 pass
   gh pr merge <n> --squash
   ```
   合并后回到主 checkout 收尾：`git fetch origin --prune && git merge --ff-only origin/main`、
   `git worktree remove .worktrees/daily-$(date +%F)`、`git branch -D chore/daily-$(date +%F)`、
   `python3 bin/lock.py release`。
   > **为什么不能直落 main**：`main` 是集成分支，远端有保护（必须走 PR）；直接在主 checkout 提交会让
   > 本地 `main` 与 `origin/main` 分叉、把后续回合的 `merge --ff-only` 全部弄失败
   > （2026-10-16 08:32 真发生过一次，`docs/git-flow.md` §0 与
   > `docs/runbooks/operations.md`「本地与远端不一致」节有说明）。

## 输出要求

- 用中文，先结论后细节，不要罗列工具调用过程。
- 没有需要用户决定的事情时，明确写"无待决事项"。
- 不虚构：所有数字都要来自上面读到的文件。
- 最终回复里**写明 Life Journal 的 `doc_id` 与推没推成功**（失败要给原因，不要省略这一步）。
