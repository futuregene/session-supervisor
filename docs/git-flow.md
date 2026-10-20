# Git 工作规范：worktree / 分支 / PR

> **何时读**：要在本仓改文件、开 PR、或收尾同步时（**必读**）

（来源：原 `git-flow.md`）

## 0. 分支模型


| 分支 | 是什么 | 谁往里写 |
|---|---|---|
| 默认分支（`main`） | **集成分支**。仓库里所有会话看到的"最新"就是它 | **只**通过 PR 合并写入（远端加分支保护） |
| `<type>/<name>` | 功能分支，如 `chore/publish`、`fix/scan-resync-log`；名字随便起，别用主干名 | 本次任务的 worktree |

- **默认分支已加分支保护**（`enforce_admins=false`，以便本设施走 PR 流程）：
  必须走 PR、required check = CI 的 job 名（本仓是 `selfcheck`）、禁止 force push。
- **绝不直接向主干提交或 push**（`bin/guard.py` 与 `bin/approvals.py` 的拒绝清单都会拦），
  要进去必须走 PR 合并。
- **绝不 `--force` / `--force-with-lease`。**
- 本仓的具体远端名、默认分支名与保护规则参数写在 `.future/memory/instance.md`（不进 git）。





## 1. 修改与工作区：先建 worktree，且必须在 `.worktrees/` 下


```bash
cd <本仓主 checkout>                 # 主 checkout：只做同步与收尾，不在里面改文件
git fetch origin
git worktree add --no-track .worktrees/<name> -b <type>/<name> origin/main
cd .worktrees/<name>
```

- `.worktrees/` 已进 `.gitignore`（嵌套工作树不得被提交），`git worktree add` 时会自动创建父目录。
- `<type>` 用 Conventional Commits 类型（`feat` `fix` `chore` `docs` `refactor` `test` `ci` `perf`），
  `<name>` 是短 slug，与目录同名——与 `future-os` 同一口径，便于跨仓一致。
- **`--no-track` 必须写**：否则新分支会跟踪 `origin/main`，一条裸 `git push` 就打到集成分支上。
- **一切文件读写用绝对路径**（`<本仓绝对路径>/.worktrees/<name>/...`）。
  `write` / `edit` 的相对路径解析到主 checkout（CWD），会把改动写进 `main`——`future-os` 已踩多次。
  每个 `write`/`edit` 之后顺手 `git -C <本仓> status --short`，主 checkout 必须是空的
  （运行态文件 `state/*.json` 例外，见 [privacy-and-state.md](privacy-and-state.md) §3）。



## 2. 建分支之前先 pull，保持最新


```bash
git fetch origin && git merge --ff-only origin/main        # 在 worktree 里，让分支跟上集成分支
```

- 基线永远取自 `origin/main`，不用本地可能过期的 `main`。
- 长任务期间 `origin/main` 前进了：**再 fetch + merge 一次**（冲突就在自己的 worktree 里解），
  不要靠"合并前再补"。



## 3. 完成后：push 自己的工作分支 → 开 PR → 立刻开 auto merge


```bash
python3 bin/selfcheck.py                 # 本地检查必须实际跑过且全绿（AGENTS.md §4）
git push -u origin <type>/<name>
gh pr create --base main --fill
gh pr merge <n> --squash --auto          # 每个 PR 都要开 auto merge，不留裸 PR
gh pr checks <n> --watch --required      # 同步等检查，**不要** sleep 轮询
```

- 合入目标永远是 `main`。
- 仓库已开 `allow_auto_merge` + `delete_branch_on_merge`；合并方式只允许 **squash**
  （`allow_merge_commit` / `allow_rebase_merge` 已关）。
- **`gh pr merge --auto` 在 checks 还"不存在"时会直接合掉**：首次开 PR 时 GitHub 对
  required check 仍是 `UNKNOWN`，此时 `--auto` 会**立即执行**合并，等于没等 CI。
  首次推送后先 `gh pr checks <n>` 确认 `selfcheck` 已经 `pass`，再开 auto merge
  （或是等第一轮 CI 完成后 `gh pr merge <n> --squash`）。**首次 PR 必踩**：结果对了不等于流程对。
- `auto merge` 有时**不自己触发**（checks 全绿、`mergeStateStatus=CLEAN`，但 PR 仍 OPEN、
  `autoMergeRequest` 为空）：`--watch` 返回后若仍 OPEN，直接 `gh pr merge <n> --squash`。
- **PR 与主干冲突时 auto merge 会静默卡住**：它"在等一个永远不会出现的 check"，而不会提示真正原因是冲突。
  **判别**：`gh pr view <n> --json state,mergeable,mergeStateStatus,autoMergeRequest`——`DIRTY` 就是冲突；
  处置是在最新主干上重建分支，别干等。所以新分支**先 push 再立刻开 PR**，别只推分支不开 PR
  （workflow 只监听 `pull_request` / `push: main`，只推分支时根本没有事件）。
- **GitHub 偶尔不给新 PR 建 workflow run**：`gh pr checks <n>` 报
  `no checks reported on the '<branch>' branch`，按 `head_sha` 查 API 也是 `total_count=0`，
  而同一分钟内别人的 PR 都正常触发 → PR 停在 `mergeStateStatus=BLOCKED`（required check `selfcheck` 缺失）。
  **判别**：先按 `head_sha` 查 API 确认不是"跑过了但没关联"；确实是 0 再处理。
  **处置**：`gh pr close <n> && sleep 3 && gh pr reopen <n>`（重新触发 `pull_request` 事件），检查随即正常跑起来。
  **红线**：`BLOCKED`／缺 required check 时**不要**合并——`enforce_admins=false` 让"能合"不等于"该合"。



## 4. 合并后：清 worktree + 清分支 + 把 `main` 同步到最新


```bash
cd <本仓>
git fetch origin --prune
git merge --ff-only origin/main                      # main 同步到最新（主 checkout 里执行）
git worktree remove .worktrees/<name>              # 先确认 git -C <path> status --short 干净
git branch -d <type>/<name>                        # squash 合并后 -d 会拒：先按下面判据确认再 -D
git worktree prune && git fetch --prune
```

- squash 合并后 `git branch -d` **必然拒绝**（上游是另一个 SHA）。判据：
  `git diff --stat <branch> <PR 的 squash sha>` 为空 **且** `git merge-base --is-ancestor <squash sha> origin/main`
  成功 ⇒ 内容确已合入，可 `-D`。别跳过判据直接强删。
- 远端分支由 `delete_branch_on_merge` 收掉；没删干净再 `git push origin --delete <type>/<name>`。
- **只清自己的** worktree / 分支；别人正在用的原样保留。
- 收尾核对：`git worktree list` 只剩主 checkout、`git status --short` 干净、
  `git log --oneline -1 origin/main` = 刚合并的结果。



## 7. 无 CI 的仓也要有闸门


本仓没有 `Makefile`，PR 的 CI 就是 `.github/workflows/ci.yml` → `python3 bin/selfcheck.py`
（等价于 `future-os` 的 `make fmt-check && make lint` 这一步）。**push 之前本地先跑一遍**，
PR 上再跑一遍；两处都绿才允许合并。

### 7.1 合并这件事**不问用户**

push 自己的功能分支 → `gh pr create` → CI 全绿 → **squash 合并** → 清理，**整条链自动**，
不要停下来问"要不要开 PR / 要不要合并"，也不要设"等用户看过再合"的闸（`POLICY.md` §A.8）。

- 用户**偶尔**说"先不要提交 PR / 等我 review"是**一次性**的：只对那次改动有效，**不要**总结成默认口径。
- 真正需要停下来的是另一类：发版/部署/`make install`、改远端保护规则、强推、直推主干、删数据、付费、对外发送
  （`POLICY.md` §B），以及 **CI 红**（先重跑；仍红就查是既有还是本 PR 引入，**不许削断言或顺手改无关代码**）。
