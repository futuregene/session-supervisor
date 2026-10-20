# 每 3 小时：把 `dev-<user>` 长期分支送进 `main`

> 这是定时任务的提示词（源文件 `prompts/dev-pr.md`）。改完必须
> `future task edit "dev 分支定期提 PR" --prompt-file prompts/dev-pr.md` 同步，
> 否则任务跑的是库里的旧副本。

你这一轮只做一件事：跑一次代管检查，按结果执行**一个**动作，然后记一行。

## 步骤

1. **取锁**：`python3 bin/lock.py acquire || exit 0`（别的回合在跑就立刻退出，回一行）。
2. **跑判据**（不要在提示词里重写判据，全在脚本里）：
   ```bash
   python3 bin/dev_pr.py --repo "<被代管仓库的绝对路径>"
   ```
   只看判定不执行就加 `--dry-run --json`。
   **这个路径在本部署记忆里，不在本文件里**：读 `.future/memory/instance.md` 的「被代管仓库」一行，
   或由任务的 `--cwd` 显式传入。分支名（`dev-<gh login>`）由脚本运行时解析，**不要写死用户名**。
3. **按输出处置**：
   - `idle` / `waiting` → 什么都没发生，正常。**只在有实质动作时**继续第 4 步。
   - `open_pr` / `arm` / `fix_behind` / `repoint` / `keep_rebase` → 脚本已执行，核对它打印的结果。
   - `report_red` → 脚本已用 `send.py --new` 开了修复会话；**不要去打扰写代码的那个会话**，
     也不要自己去改代码。
   - `report_local_ahead` → 只记录，**不要** push、不要 reset、不要提醒开发会话；
     那是别人没推的工作，等它自己推。
   - 脚本以 `FAILED` 开头 → 读 `logs/dev-pr.log` 的同一行，判断是网络/权限/冲突哪一种。
     冲突（`merge main failed (conflict?)`、`rebase onto main failed`）**不要**自己解：
     在 `journal/` 记下分支、PR 号与冲突文件，列入 `reports/needs-you.md` 的候选（若会影响用户）。
4. **记录**（每次实质动作都要）：
   - `journal/YYYY-MM-DD.md` 一行：动作、分支、PR 号、commit（带仓库名）；
   - 实际把代码合进主干的（`open_pr` 之后的合并在下一轮发生；`repoint`/`keep_rebase` 属收尾），
     另记 `reports/on-your-behalf.md` —— 这条通道是**代表用户**把代码合进主干。
5. **收尾**：`python3 bin/lock.py release`。本任务**不改本仓文件、不开本仓的 PR**；
   若发现本设施自身的缺陷（脚本判错、日志缺失），只记录 + `bin/notice.py add`，不在这轮修。

## 硬边界

- 只操作被代管的仓库（`--repo` 指向的那个），不碰本仓的 `main`。
- **不删除**远端 `dev-<user>` 分支；那个仓库里唯一允许的 force push 是 `repoint` 的
  `--force-with-lease`。
- 不合并红的 PR，不反复重跑检查，不放宽断言换绿。
- 一轮最多一个动作；`waiting` 就结束，把等待交给 3 小时后的下一轮，**不要在同一轮里轮询**。
- 花费自律：这一轮预期 3–6 次工具调用；超过 10 次说明在手工重做脚本的活，停下来记录。
