# 隐私与运行态文件

> **何时读**：决定某个文件该不该提交、或处理隐私/运行态时

## 1. 隐私文件：`.private/`

- 与人隐私相关、**不能提交**的东西（凭据、个人材料、外部服务配置、临时私密数据……）
  一律放在仓库根的 `.private/` 下（worktree 里的绝对路径写法见 [git-flow.md](git-flow.md) §1）。
- `.gitignore` 里 `.private/*` 全忽略，只留一个 `!.private/README.md` 用于说明约定；
  **`.private/` 下的任何文件都不得 `git add`**，`bin/selfcheck.py` 会检查有没有被误跟踪。
- 需要临时给某个会话用的隐私文件，复制到**那个会话自己的工作区**再用——别在共享目录里散落。
- 已经有专用忽略规则的（如 `wechat-publisher/wechat-publisher.yaml` 那类）保持原样，不搬进来。

## 2. 本地 checkout 与远端的关系

- **不必让本地与远端"看起来一样"**：本仓**不需要**保留一份与远端逐字节一致的本地镜像；
  **远端（尤其默认分支）才是权威**，本地主 checkout 是**运行这个设施的工作目录**
  （`state/`、`logs/`、守护进程都在用它），它天然会被扫描器改得"不干净"。
- 所以判"同步"只需要一条：`git rev-list --left-right --count origin/main...main` = `0 0`
  **或**本地在 `main` 上但落后（拉一下即可）。本地多出来的运行态差异**不是问题**，别去追它。
- 真正要保证的是**内容别丢**：本地若出现 `origin/main` 没有的提交，先看它是什么
  （`git log --oneline origin/main..main`），是有效工作就转成 PR，确认内容已在远端后再对齐。

## 3. 运行态文件与提交节奏

- `state/` 与 `inbox/` **整目录被忽略**：前者是唤醒计数、健康、待补发队列、
  一次性指令、轮换标记与 `observed_sends.json` 这个 180 秒缓存；后者是扫描器事件流。
  它们每秒都在变、且含别的会话原文，所以**不要为它们起 commit**，也不要为它们跑 worktree/PR。
  主 checkout 因此"不干净"是正常的，不代表有工作没提交。
- **为什么不逐文件列规则**：逐条规则失败过三次（两条被漏写的换行粘成一条、
  两个新 state 文件入库时没加规则、另一次又漏两个），每一次都等于把别人的原文
  交给下一次 `git add -A`。目录级规则没有"漏一条"这种失败模式，
  `bin/selfcheck.py` 另有一条断言 `git ls-files state inbox` 必须为空——比逐个 `check-ignore` 更强。
- **它们同时含隐私**：`inbox/*.jsonl` 的 `detail.lastAssistantText`、`state/state.json` 的会话名、
  `state/msg-*.txt` 的**我发给别人的原话**，都是**其他会话／用户的原文**。
  本仓有远端，所以这些**不得被跟踪**——现在由目录级规则保证（见上），
  没有例外项，`state/SNAPSHOT.md` 也在其中。
- **历史教训（为什么不再有逐文件规则）**：被跟踪的运行态文件曾让 `git checkout -- state/`（本节推荐的
  清理手法）把**提交里的旧副本**写回工作区——轮换后的新 id 就这样被还原成**已归档**会话的 id，
  手册被装到了归档会话上。目录级忽略消掉了这一类；与之配套的那道保护**仍然要留**：
  `bin/apply_prompt.py` 从会话列表解析当前总管会话，与 `state/monitor_session.txt` 不一致时**拒绝执行**。
- **新增写盘脚本时**：只要写在 `state/`、`inbox/`、`logs/`、`journal/`、`reports/` 之内就自动被忽略；
  写在这五个目录**之外**才要加规则，并同一步加进 `bin/selfcheck.py::check_runtime_files_ignored`
  的 `probes`、再**证伪一次**（临时删规则 → 该检查必须 FAIL）。
- **隐私文件一律放 `.private/`**（见 §1）；`journal/`、`reports/` 现在也不进 git（见 §4），
  但这不等于可以把凭据写进去——它们仍是会被人读到、会被推送 Life Journal 的正文来源。
- **`git merge --ff-only origin/main` 会被这些本地改动挡住**（`error: Your local changes to the following files
  would be overwritten`）：看到的是 `Updating … Fast-forward` 紧接着 `Aborting`，`merge` 其实返回 1、HEAD **没有**前进
  （它打印 `Updating …` 在开始尝试之前）。正确做法：`git checkout -- state/ inbox/`（运行态，扫描器几秒内重生）
  再 `git merge --ff-only origin/main`；要保住别的改动就 `git stash -u` 再 merge、之后 `stash pop`。
  **判据是 `git rev-list --left-right --count origin/main...main` 必须是 `0 0`**，不能只看 merge 的打印。
- 真正要提交的是**基础设施改动**：`AGENTS.md` / `docs/` / `bin/` / `POLICY.md` / `bin/config.json`，
  一次 commit 一个意图、message 写清**为什么**（见 [gotcha-records.md](gotcha-records.md) E2）。

## 4. 机制 vs 实例：什么进 git

> **一句话**：**别人拿去能用的是机制，只有这台机器有意义的不是。**
> 三类内容的归属见 `AGENTS.md` §1（每轮注入，权威）；这里只给判断用的表。

| 问题 | 进 git | 例子 |
|---|---|---|
| 换一台机器照用还有用吗（**基础设施**）？ | ✅ | `bin/`、`docs/`、`prompts/`、`POLICY.md`、`AGENTS.md`、`bin/config.json` |
| 它记录的是某次具体经过（本机实测数字、用户原话、事故叙述）吗？ | ❌ | `journal/`、`reports/`、`.future/memory/` |
| 它记录的是**这台机器此刻**的状态吗？ | ❌ | `state/SNAPSHOT.md`、`.future/goal`、`.private/`、`logs/`、`inbox/*.jsonl` |
| 它只在**这次调试**里有意义吗？ | ❌ | `state/first_wake_verified`、一次性回滚备份 |
| 它是**某个工具顺手创建**的运行态吗？ | ❌ | `.future/memory/` 本体的运行文件（话题文件是有意维护的，另算） |

**`journal/` 与 `reports/` 属第二行**：它们记的是这台上发生过什么（哪些会话、替用户做了什么决定、
他的原话、当时的额度），换一台机器就没有意义，因此**与 `state/` 一样留在磁盘、不进 git**。
它们同时是**唯一的持久记忆**（会话会被原地 reset），所以不要删——但没有远端副本，
要备份靠这台机器自己的 Time Machine / 磁盘快照。

**不跟踪 ≠ 删除**：这些文件仍在磁盘上，运行照旧（`future goal status`、`bin/state_snapshot.py` 读写的都是磁盘上的那份）。
不跟踪只是**不再为它们产生 diff，也不再把本机事实写进历史**。

### 本机事实怎么写

- 路径：代码里用 `os.path.dirname(os.path.dirname(__file__))`（`WATCH`），`bin/config.json` 里用 `${WATCH}` 指本仓库；
  **不要写 `/Users/<名>` / `/home/<名>`**。
- 二进制：`bin/toolpath.py` 统一发现 `future` / `python3`（`config.future_bin` 写 `"future"` 表示"按 PATH 找"）。
  shell 入口只允许在 `for d in …` 的候选循环里出现 Homebrew 前缀。
- 守护进程的 launchd label（Linux 上是 unit 名）：**不写进仓库**。运行手册用
  `LABEL=$(basename "$HOME/Library/LaunchAgents/"*.session-watch.plist .plist)` 现算。
- 记录文件（`journal/`、`reports/`）里写 `~/…`、`com.<user>.session-watch` 这类**占位形式**：
  它们仍是证据，只是不再点名某台机器。

## 5. 钉住它的三条探针

`python3 bin/selfcheck.py` 里：

- `no local identity in tracked files` —— 被跟踪的文件里不得出现 `/Users/<名>`、`/home/<名>`，
  也不得出现**真的** label（`com.<名>.session-watch`）；占位形式 `com.<user>.session-watch` 与 `~/…` 允许。
  **这条判据刻意与运行者无关**：它不看当前 `$HOME` 的用户名——第一版看，于是在本机是绿的，
  而在 CI 的 runner 上（账号名 `runner`、记录里又有 "shared CI runner"）变红。**一个结论取决于谁来跑的检查，不算检查。**
- `instance state untracked` —— `.future/memory/` 与 `FUTURE.md`、`.future/goal/*`、`state/SNAPSHOT.md`、
  一次性本机备份必须**既未跟踪、又被忽略**；
- `toolchain not hardcoded` —— 除 `bin/toolpath.py` 外，`bin/` 里不得出现绝对 Homebrew 路径。

三条都做过**证伪**（故意放入违规内容 → 变红 → 撤掉 → 变绿），不是"看起来通过"。
