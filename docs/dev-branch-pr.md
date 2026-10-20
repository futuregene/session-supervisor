# 代管别人的 `dev-<user>` 长期分支：每 3 小时把它的提交送进 main

> **何时读**：当你需要把某个仓库（不是本仓）的开发者分支定期合并进主干时；或定时任务「dev 分支定期提 PR」报错、没有按节奏产出 PR 时。

有些仓库改成「每人一条长期分支」：任务会话只 `commit` + `push` 到 `dev-<gh-login>`，
**不开 PR、不开 auto-merge**；把分支送进 `main` 由本设施按节奏代管。本文件的机制与判据
适用于任何这样的仓库，具体仓库路径与节奏只写在本部署的 `.future/memory/`。

## 0. 一句话

`bin/dev_pr.py --repo <仓>` 读远端事实 → 纯函数 `decide()` 给出唯一动作 → 执行。
**不要手写一遍 `gh` 序列**：判据里有几处反直觉的地方（见 §2），散落在提示词里迟早走偏。

## 1. 七个动作（`decide()` 的全部输出）

| 动作 | 何时 | 做什么 |
|---|---|---|
| `idle` | 远端没有该分支，或分支与 main 同级 | 什么都不做 |
| `open_pr` | 有提交且**没有 open PR** | `gh pr create --base main --head dev-<user>` → 立刻 `--squash --auto` |
| `arm` | 有 open PR 但没开 auto-merge | `gh pr merge <n> --squash --auto`（裸 PR 不允许留着） |
| `waiting` | 已开 auto-merge、检查在跑 | 什么都不做，下一轮再看 |
| `fix_behind` | PR 是 `BEHIND` | 临时 worktree 里 merge `origin/main` → push，别让它来回弹 |
| `report_red` | 必需检查红 | **另开新会话**修（见 §3），不合并、不反复重跑 |
| `repoint` | 已合并且分支内容与 main 相同 | `git branch -f dev-<user> origin/main` + `--force-with-lease` push |
| `keep_rebase` | 已合并，但分支上还有更新的提交 | 把那些提交 rebase 到新 main 上再 push（**不能丢**） |
| `report_local_ahead` | 本地 `dev-<user>` 领先远端 | 只报告：那是别人没推的工作，不许替它 push / reset |

优先级：`report_local_ahead` > （PR 存在时）`report_red` > `fix_behind` > `arm` > `waiting`
> （无 PR 时）已合并判定 > `open_pr` > `idle`。

## 2. 三件反直觉的事（判据就建在这上面）

1. **squash 合并后，分支的提交永远不是 main 的祖先。** 所以「领先 main 的提交数 > 0」
   **不等于**「没合并」。判断"已合并"只认：该 head 的最新 merged PR 的 merge commit
   `merge-base --is-ancestor origin/main` 成功。搞错这一条，就会给一个已合并的分支再开一次 PR。
2. **红了先于 BEHIND。** 给一条红的 PR 先解冲突，只是再烧一轮 CI 去看同一个失败。
3. **本地领先远端是别人的工作。** 可能是一个还没跑完的会话，也可能是用户没推的提交；
   替它 `push` 或 `reset` 都属越界，只报告。

## 3. 检查红了：开新会话，不打扰写代码的那个

`report_red` 用 `bin/send.py --new --cwd <仓> --name "PR #<n> 检查失败修复"`，正文要给：
失败的 job 名、run 链接、PR 链接，以及三条禁令——**不要开新 PR**（已经有了）、
**不要开 auto-merge**、**不要为了让检查变绿而放松断言/改测试**；flake 先 `gh run rerun --failed` 并说明。

为什么不发给写代码的那个会话：它多半正 mid-run，消息要等它这一轮结束才被读到，读到时就已
在回答被取代的旧状态（见 [gotcha-send.md](gotcha-send.md) A3）。

## 4. 节奏与守卫

- 节奏写在 `bin/config.json: dev_pr_hours`（默认 3），由定时任务「dev 分支定期提 PR」触发，
  提示词源文件是 `prompts/dev-pr.md`，任务库里存的是副本 ⇒ **改文件后必须
  `future task edit <名字> --prompt-file prompts/dev-pr.md` 同步**。
- 仓库路径**不进 git**：写在 `.future/memory/`，由任务提示词传给 `--repo`。
- 每个动作一行进 `logs/dev-pr.log`；实际发生的合并另外记 `journal/` 与
  `reports/on-your-behalf.md`（这条通道代表用户把代码合进主干，属"代表用户做的事"）。
- **没有本设施时**：被代管的仓库自己写了回落路线（任务会话自己开 PR），所以
  "没看到 PR" ≠ "活没干完"；本设施停机时不需要另行通知。
- `--dry-run` 只打印判定、不执行；排查"为什么没开 PR"时先跑它，读 `--json` 的事实。
- **脚本自己不取锁**：并发保护靠回合锁（定时任务的提示词第 1 步就是 `lock.py acquire`）。
  手工在多个回合里同时跑它时，`open_pr` 那一步是"先查后开"，两次几乎同时的检查仍可能各开一个 PR——
  真撞上了，留编号小的那个、关掉另一个并记一行。节奏是 3 小时，正常不会撞。

## 5. 边界

- 只在本仓之外执行合并；**本仓自己的改动仍走 worktree → PR → CI → squash**，不适用本文。
- 不删除被代管仓库的远端 dev 分支：它是那位开发者的连续分支，删了 PR 评审时间线也没了。
- 那个仓库里唯一允许的 force push 就是 `repoint` 的 `--force-with-lease`；其余一律不 force。
