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
