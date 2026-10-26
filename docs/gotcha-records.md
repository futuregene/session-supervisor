# 坑 · E · 记录与提交

> **何时读**：提交与记录之前

（分类 E；来由与真实经过见 `.future/memory/`，本文件只留机制与判据。
编号有断档——条目被合并或移走时**不重排**，否则脚本与记录里的引用会指向别处。）

## 已由探针钉住，不再展开

| 坑 | 钉在哪 |
|---|---|
| 改了 `prompts/append.md` 忘了重新 apply → 每轮重发整本手册 | `bin/ensure_append.py`（`wake.sh` 每轮调）+ `check_ensure_append_selftest`；判据 `apply_prompt.py --check` 的 `mode=` |
| canary 的"复述令牌"判据假阴性 | 确认指令写进 append 本身；失败同时删 record（`selftest_ensure_append`） |
| `@file` 附件在目标侧是原始 JSON | `turn_prompt.py` 渲染成 markdown + `selftest_prompt` 断言不出现 `{"` / `<file name=` |

**两条写卡片的规矩**（代码管不了，所以留在这里）：卡片只写"这一回合"的增量，不重复常驻规则；
`append_prompt` 与 `turn_prompt` 必须用同一套哈希归一化，否则极简卡永不启用。

### E1 本仓与业务仓是两件事
- **对策**：本仓记录设施自身；讨论 push/PR/合并的**对象**要先说清是哪个仓库。
  本仓有远端且默认分支受保护（见 [git-flow.md](git-flow.md)），业务仓的默认分支要现查。

### E2 提交信息写"为什么"，一次一个意图
- **对策**：先 `git status --short` + `git diff --stat` 自查；把判断与依据写进 message，便于日后追溯。

### E3 `git` 的 author date ≠ committer date
- **表现**：`git log -1 --format=%ad` 显示的时刻不是"何时落地"（squash/cherry-pick 会不同）。
- **对策**：核对"何时发生"用 committer date（`%cd`）与会话 transcript 时间；两者混用会得出错误时间线。

### E4 判断"是否已合并/已推送"要问 git，不要问人
- **对策**：`git fetch origin && git merge-base --is-ancestor <sha> origin/main && echo YES`；
  再看 `rev-list --left-right --count origin/main...main` 判断本地是否领先。

### E5 `gh pr merge --auto` 在 required check 尚不存在时会**直接合并**
- **成因**：`--auto` 只在"合并被 block"时排队；check 尚未上报时没有 block，于是立刻执行合并。
  它不是"等 CI 绿"，是"等阻碍消失"。
- **对策**：新仓 / 新 required check 的**第一个 PR**，先 `gh pr checks <n> --watch` 看绿，再开 auto merge。
  auto merge 卡住的其它形态（冲突、GitHub 不建 run）见 [git-flow.md](git-flow.md) §3。

### E6 开 PR 前先核对"那个仓库"的默认分支
- **对策**：`gh repo view <owner>/<repo> --json defaultBranchRef`；用户的术语拿不准时**先问**，
  别用一次真实的重命名来试错（曾把 "PR" 当成分支名真的建了一条分支并设为默认）。

### E7 验收标准**必须从代码来，不能从名字来**
- **教训**：从两处名字相像的机制推断出"加载时会重建消息历史"，写进工单的验收标准——
  而那条机制**不存在**（一个重放成本记账，一个重放进行中 run 给重连客户端）。
- **后果**：为一个不存在的威胁设计验收，而真正重要的一条（重写历史后的空间回收）反而漏了。
- **对策**：写验收标准前先**打开代码确认机制存在**，并给出文件:行。名字相近不是证据。

### E8 并发回合会把 commit 落在**主 checkout 的当前分支**上
- **成因**："收尾按 `git status` 决定是否 commit"是所有回合共用的动作，而它不知道主干受保护、也不知道该走 worktree。
  **这不是一次性事件**——旧指引不改，它就会继续发生。
- **对策**：① 收尾 commit 前先 `git rev-parse --abbrev-ref HEAD`，**在主干上不要 commit**，
  要记的东西挪进 worktree 走 PR；② 已经发生的分叉**不要丢**——先 `git log --oneline origin/main..main`
  看清是什么，核对内容是否已在远端（`git show origin/main:<file> | grep`），
  再决定"转 PR"还是"确认无内容丢失后 `git reset --hard origin/main`"。

- **E8 的第二种形态**：指向同一分支的**并发**回合里，A 回合
  `git merge <feature>` 进本地 `dev-<user>`、B 回合随后 `git reset --hard origin/dev-<user>`，
  那个合并提交当场从分支上消失，只剩 reflog —— `git branch --contains <sha>` 返回空、
  `git worktree list` 干净、`git status` 干净，**看起来什么都没发生**。
  判据：`git reflog show dev-<user>` 里出现成对的
  `merge <branch>: Merge made by ...` / `reset: moving to origin/dev-<user>`。
  处置：**先把每个被 reset 掉的合并提交的 sha 记下来**（reflog 里就有），再逐个核对内容是否已在 origin
  （`git diff origin/dev-<user> <sha> -- <该合并触及的路径>` 为空即已落地）；只有在 origin 完全没有时，
  才从那个 sha 切分支补推（`git worktree add -b rescue/<name> origin/dev-<user>` + `git cherry-pick <sha>`）。
  **不要**因为 `branch --contains` 为空就判定"没丢"——内容可能在、提交可能不在，这两件事要分开核。

### E12 `git rm --cached` 只改**索引**，但合并会在**别的 checkout 删掉文件**
- **成因**：提交说的是"这些路径不存在"，合到别的 checkout 时那里的工作副本就被删了。
- **对策**：移出 git 时**要么在主 checkout 原地做**，**要么合并后立刻从父提交恢复**（`git show <旧提交>:<path> > <path>`，
  且**不要**再进索引）。判据：合并后看一眼 `git log --stat`——删除也是一种改动，一样会被合并。

### E13 清磁盘时**先 grep 记录里点名的产物**：中间产物该删，最终 pin 不该
- **规矩**：① **删之前 grep**：`grep -ohE "/tmp/[^ )\`]+" docs/ docs/testing/ | sort -u` 得到"记录点名的产物"清单，
  逐个 `test -e`；存在的、且属于已发布数字的 **先挪到稳定位置再删别的**；
  ② **只留最终 pin**：每个已发布数字留**一份** union/汇总产物，中间产物、探针树、构建目录照删。
- **记录里写产物时要带状态**（`retained at <path>` / `not retained — re-measure §x`）：
  只写路径不写状态，读者会默认它还有效。

### E14 PowerShell 双引号 here-string 会把 Markdown 代码段的**反引号当转义符吃掉**

- **成因**：`@"..."@` 是**可转义**字符串。文本里 `` `r `` `` `f `` `` `a `` `` `t `` `` `b `` `` `0 ``
  分别变成 CR / FF / BEL / TAB / BS / NUL，而它们的 `\r`、`\f` 前缀字符**一起消失**。
  于是 `` `fix/agent-ready-order-v2` `` 落盘成 `FF + "ix/agent-ready-order-v2"`，
  在编辑器里看不见、`Select-String` 也照样匹配——**直到有人渲染 Markdown 或 grep 控制字符才发现**。
- **判据**：记录文件里出现**行内控制字符**（除 `\n`/`\r\n`）。一条命令查：
  `python3 -c "import sys;s=open(sys.argv[1],'rb').read().decode('utf-8','replace');print([(hex(ord(c)),i) for i,c in enumerate(s) if ord(c)<32 and c not in '\n\r'])" <file>`
- **对策**：给 `journal/` `reports/` 这类 Markdown 记录，
  ① **首选 `write`/`edit` 工具**（不经 shell，不转义）；
  ② 非要用 shell 时用**单引号** here-string `@'...'@`（不转义反引号）或 `Add-Content -Value ([IO.File]::ReadAllText($src))`；
  ③ 已经写坏的：按逆向映射还原（FF→`` `f ``、BEL→`` `a ``、TAB→`` `t ``、BS→`` `b ``、NUL→`` `0 ``、孤立 CR→`` `r ``），
  再补上被吞掉的**收尾反引号**，最后复查控制字符为 0。
- **为什么记忆深刻**：一次会话里 4 个记录文件共 22 处代码段被打断，`git` 命令、commit 号、路径全变了样；
  而这些文件是**唯一持久记忆**（会话会被 reset），坏了没有第二份。

### E15 历史重置会删掉本会话的花费账本

`future session reset` 执行 `DELETE FROM runs WHERE session_id=?`
（`future-os/agent/src/session/sqlite_store/transcript.rs:187`；同一次还会删掉没有对应
`runs` 行的 `run_events` / `run_snapshots`）。所以**每次原地重置，本会话此前的 token
记账一起消失**。实测：8 次重置后本地只剩 42 行、`min(epoch)=4`，按它算出来是 ¥2.56，
而 app usage 显示 ¥44+ —— 报出去就是错的一个数量级。

- **判据**：回答"这个会话花了多少"以**服务端 usage** 为准；本地只能回答"自上次重置以来"。
  要用本地算，先看 `min(epoch)` 有没有断层，有断层必须说明覆盖面。
- **现在的做法**：`bin/monitor_roll.py` 在 reset 之前调 `bin/cost.py --snapshot`，把该段写进
  `state/cost-ledger.jsonl`；`python3 bin/cost.py` 输出"归档 N 段 + 当前库 = 合计"，
  并按 epoch 区间去重（重置失败时同一段不会被算两次）。
- `~/.future/agent/session-backups/*.json` 只有条目（`entries`），**不含 token，补不了钱**——
  别指望用备份还原花费。

---

## A29 · 「分支不存在」不等于「没推过」——合并后分支会被删

> **一句话**：判断一个 commit 是否已经在远端，**不要看本地分支在不在、也不要用 `gh pr list --head` 的缺省结果**。
> squash 合并后分支会被删、PR 变成 `MERGED` 就不再出现在缺省（OPEN）列表里——
> 这两个信号都会被读成「它没推过」，而结论可能正好相反。

**何时读**：要替别的会话推分支 / 开 PR 之前；或者判定「某个会话没推就收尾了」之前。

### 为什么这条值得写下来

一个 commit 明明已经合并进主干，而下面三个信号全都是「空的」：

1. `git worktree list` 里没有它的 worktree；
2. `git branch -a --list '*<名>*'` → 空；
3. `gh pr list --head <branch>` → `[]`。

**三条都对，三条也都不构成「没推过」**：squash 合并后按规程删掉了分支与 worktree，PR 变成 `MERGED`
后也不再出现在缺省（OPEN）列表里。把这三个空读成「没推过」，接着替对方重建分支、推送、开 PR——
因为内容已在主干，那个 PR 会是一个**空提交**，而主干不允许 force 改写（`POLICY.md` §C），删不掉。

同一次误判还会被转达给对方：「你提交了但没推」——那是冤枉。

### 正确判据

```bash
# ① 内容是否已在主干（squash 后 sha 变了，所以比内容、不比 sha）
git diff --stat <sha> origin/main          # 为空 = 内容已在主干

# ② PR 是否已合并（--state all 是关键，缺省只看 OPEN）
gh pr list --head <branch> --state all --json number,state,mergedAt

# ③ 分支是否仍在远端（合并后没了是正常的）
git branch -r --contains <sha>
```

**最省的一条**：`git log origin/main --oneline | grep <commit 标题片段>` ——
squash 合并会把标题带进主干（带 `(#N)` 后缀），一眼认得出。

### 衍生规则

1. **不要替别的会话推分支、开 PR**，除非确认它已停下且内容真的没上去。
   替它推的代价不只是重复 commit：它自己再推会撞分支，或者像我这次一样产生空提交。
2. **判定「它没推送」之前先跑完上面两条以上**——
   只查一次缺省 `gh pr list --head` 和 `git branch`，等于用「合并后的清理痕迹」当「没做过」的证据。
3. 已经发生就**如实记**，不要因为「没有内容损失」就略过——这次错的是方法与一次冤枉人的转达。
