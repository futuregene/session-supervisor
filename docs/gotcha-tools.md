# 坑 · D · 脚本与工具

> **何时读**：用 `future` CLI / shell / git 之前

（分类 D；来由与真实经过见 `.future/memory/`，本文件只留机制与判据；goal/worker 类的坑已拆到 [gotcha-goal.md](gotcha-goal.md)。
编号有断档——条目被合并或移走时**不重排**，否则脚本与记录里的引用会指向别处。）

## 已由探针钉住，不再展开

| 坑 | 钉在哪 |
|---|---|
| 判据读 `$HOME` → 本机绿、CI 红 | `check_no_local_identity`（判据刻意与运行者无关；用 `HOME=<CI home>` 复跑一遍） |
| 用了却没 import 的模块静默走 fallback | `check_imports_complete`（`ast` 解析，不信 grep；`except Exception` 包住的调用从不报错就先怀疑它没跑） |

两条的共同点：**一个能自己检查的规矩，就不要靠人记得。**

### D1 shell 工具的每次调用都是**新进程**
- **表现**：用 PID 归属的锁在 `release` 时判定"不是持有者"，锁残留。
- **对策**：锁归属于"回合"而非进程；`release` 直接删文件。任何跨调用的状态都要落盘，不能靠变量。

### D2 `lock.py release` 打印 `not holder; nothing to do` 不等于失败
- **对策**：它 exit 0，且这正是预期（每个 shell 调用是新进程）。判锁是否残留用 `lock.py status`。
- **锁的真实语义**：`acquire` 记录的 pid 是 `python3 bin/lock.py acquire` **自己**，打印完就退出，
  所以后续 `acquire` 看到"pid 已死"不能据此抢占——**超过 `lock_ttl_seconds`（config，默认 900s）
  才可接管**。取舍：宁可偶发重叠，也不要一个崩掉的回合永久阻塞唤醒。
- **连带口径**：**给"已归档"的监督会话发消息 = 起第二个监督回合**；要用当前那条，看
  `state/monitor_session.txt`（它不跟踪，见 [privacy-and-state.md](privacy-and-state.md) §3）。

### D3 `-m "..."` 里嵌套引号会被 shell 截断
- **对策**：多行或含引号的提交信息写进文件，用 `git commit -F <file>`，提交后删掉该文件。

### D4 `future` CLI 的几个易错签名
- `future session status <id>`：**位置参数**，没有 `--session`（`future run` 才有）。
- `future run --help` 里 `--cwd` 写 "default: current directory"：对**既有会话**是误导（见 `gotcha-send.md` A1）。
- `future session set <id> --cwd <dir>`：改 cwd 的正当入口，会连带改 workspace 归属。
- `future session reset` 与 `compact` 都需要 agent 侧支持（CLI 与 agent 是两个独立升级的层）。

### D5 `rg` 的 `--glob`/`-g` 要放在模式之后
- **对策**：`rg -n 'pattern' -g '*.rs' -g '!target'`。

### D6 桌面端 workspace 列表只能新增、不能删
- **对策**：CLI 无法删除条目；要清理指向旧目录的 workspace 得请用户在桌面端侧栏删。

### D11 绝不在 worktree 里跑安装类目标（`make install` / `make install-skills`）
- **表现**：在 `.worktrees/<x>/` 里跑安装，本机技能目录下的内置技能全部变成**指向该 worktree 的悬空软链**，
  worktree 一删就成批断链——agent 每轮加载的技能当场失效。
- **成因**：链接用 `ln -s "$(cd "$skill_dir" && pwd)"`，**绝对路径**取的是"当前 checkout"。
- **对策**：安装类目标**只在主仓 checkout 里跑**；要在 worktree 里验证安装器行为，必须用**临时 `HOME`**
  （`HOME=$(mktemp -d)`），绝不用真实 `HOME`。收工前复查悬空链接数应为 0。

### D12 定时任务的 `add/edit` 不校验 `--model` 与 `--thinking`
- **表现**：不带这两个 flag 时照样打印 `Created task …` 且 exit 0，但库里存的是空 `modelId` / 空 `thinkingLevel`。
  运行期后果：**静默落到"当时的全局默认模型"**（换默认就换模型），思考预算可能是 **0**；
  写错的模型 id 也不报错，而是被替换成 replacement model，而会话、账本、日志里**全都显示你敲的那个 id**。
- **对策**：任何 `task add/edit` 都显式给 `--model <provider>/<id>`（先 `future models --json` 核对存在）
  与 `--thinking off|minimal|low|medium|high|xhigh`；建完**回读核对**（create 的输出不算证据）：
  ```bash
  future task show <id> --json | python3 -c "import json,sys; t=json.load(sys.stdin); \
    t=t.get('task',t); assert t['modelId'] and t['thinkingLevel'], 'flag 没落库'; print(t['modelId'], t['thinkingLevel'])"
  ```

### D16 `git checkout -- <file>` 会**丢掉整个文件**的未提交改动
- **场景**：为验证一条探针"能不能被证伪"而临时改坏常量，想改回去时用了 `git checkout -- <file>`——
  那一轮对它的**全部**改动一起没了，而自检仍全绿，因为改坏与丢失正好互相抵消，最容易误读成"探针被证伪了"。
- **正确做法**：撤销**一次实验性修改**用**反向 edit**，然后用 `git diff --stat` 确认改动**还在**。
  `git checkout --` 只适合整文件**丢弃**（`state/` 的运行态文件是例外：它们本就该被丢弃）。
- **判别**：验证实验做完后看 `git status --short`——你**预期**改动的文件如果不见了，就是踩了这个坑。

### D17 `future session compact` 可能把会话**打死**
- **表现**：压缩**成功完成**，但此后该会话的**每一次**请求都失败：
  `session persistence is unavailable: … cannot append to a compacted settled run`。
- **成因**：事件存储拒绝对**已有 run 快照**的 run 追加事件；压缩后历史里的 run 都成了快照，
  于是收不下新事件；随后每次请求都在 flush 栅栏处失败。
- **假象**：`session status` 一切正常，`contextPercent` 还变成 **0%**——按"上下文超阈值"的触发器永远不会响，
  卡死只能靠**失败日志**识别。
- **对策**：① 对长期会话别用 `future session compact`（`config.monitor_compact_first: false`）；
  ② 若要用，压缩后**马上**发一句话验证；③ `monitor_roll.py` 的卡死触发器已认这条报错。

### D19 证明"某个修复真的在产物里"：符号检查 + 旧包对照
- **做法（双层证据）**：① 构建侧 `gh run view <run> --json headSha` 必须是目标提交；
  ② 产物侧解开 bundle，`strings -a … | grep -c <新引入的符号>`。
- **关键**：**旧包要一起测**——只有"新包有"不知道是不是巧合；"旧包 0 / 新包 1"才证明这个检查能区分。
- **判据必须来自 `git show <fix>^` 与 `git show <fix>` 的差集**，不能凭"这词看着像这次加的"去猜。
- **不可用作判据**：函数参数名、局部变量名、类型名（会被压缩器改名）；已有符号（两边都有）。
- **判据不可用时的诚实做法**：改用能证明的链条并写明——headSha = 目标提交、下载 sha256 与源一致、
  bundle 哈希与上一版不同。**符号层缺失就说明缺失**，别拿别的数字冒充。

### D20 `future session reset` **不重置** token 与成本统计，但会删掉被丢弃轮次的 per-run 明细
- **累计值**（`session_info` 里的 `tokens_in/out/cache_*`、`total_cost`、`cost_split`）**从原值继续往上加**，
  不会归零——`cost_split` 是逐请求按当时模型价累加，不是用当前模型回头重算。
- **会真的少的是 `runs` 表**：reset 时整表按会话删除，随后只从**保留下来的**条目的 `run_terminal` 标记重建。
  ⇒ 被丢掉的轮次没有 runs 行了。
- **推论**：**任何"对 runs 求和"的用量视图，在 reset 后会比 `session_info` 的累计值小**。两个数不一致不是丢数据，
  而是口径不同（会话累计 vs 可重建的 per-run 明细）。**报数字时必须说明用的是哪个。**
- **恢复**：per-run 明细可从 agent 的 session 备份里重建。

### D25 屏幕锁着时 `git push` / `gh` 会**无限期挂住**（不是慢，是在等一个不会出现的钥匙串弹窗）
- **判别**：网络正常（`curl https://github.com` 秒回），但 git/gh 无输出直到超时；
  `security show-keychain-info` 回 `User interaction is not allowed` ⇒ **不是仓库问题**。
- **`gh` 会误导**：同一状态下 `gh auth status` 说 token invalid——**不是过期，是读不到**。别照着它去 `gh auth login`
  （同样会挂，还会覆盖掉能用的凭据）。
- **处置**：① 所有网络 git/gh 调用都套 `timeout`；② 挂住时把工作留在**本地已提交**的分支，在 `journal/` 写明
  "待解锁后推送"，下次唤醒再试；③ **不要**把 token 塞进 remote URL 或环境变量（属凭据，禁止）。
- **为什么值得记**：只有**需要凭据的操作**会被它打到，本地命令全都照常——表现很像"偶发网络故障"。

### D26 「0 个测试」不等于「夹具不运行」——模块可能根本没编译进来
- **判据（照做即可）**：看到 `0 tests` 先分三件事——
  1. **模块在那个 flavour 里编译进来了吗**（`#[cfg(feature = …)]`、`cfg(not(test))`、`cfg(target_os)`）；
  2. **测试名过滤器**是否写错（命中 0 个也会静默通过）；
  3. 以上都排除后，才谈"夹具没触发"。
- **为什么贵**：把"没编译"当成"覆盖不到"会多写豁免；当成"已覆盖"则给一个**根本没跑过**的夹具挂上"已验证"。

### D27 Windows 上这套设施的五个静默坑（第一次在 Windows 装时逐条踩到）
- **CRLF shebang**：`core.autocrlf=true` 下 checkout 出来的 `bin/*.sh` 是 CRLF，Git Bash 对每一行都回
  `$'\r': command not found`，`watchd.sh` 起不来，唯一症状是 `state/health.json` 一直旧。
  药方是仓里的 `.gitattributes`（`*.sh text eol=lf`）+ `check_shell_syntax` 对 CRLF 直接判红。
  **老 checkout 要 renormalize**（`git rm --cached -r . && git reset --hard`），否则属性文件改不动盘上已有的字节。
- **`subprocess(..., text=True)` 用 locale 解码**：Windows 简体中文机器上是 gbk，而 `future` 输出 UTF-8，
  于是**只要哪个会话标题带中文，解析就抛 `UnicodeDecodeError`**（`session list` 一挂，扫描器就瞎）。
  药方：所有 `text=True` 一律带上 `encoding="utf-8", errors="replace"`。
- **bash 不在 PATH 上**：`bin/watchd.sh`、`wake.sh`、`send.py` 的中继、`liveness --ensure` 都要真 bash。
  Git Bash 在 `C:\Program Files\Git\bin`，默认不进 PATH；**而且已启动的进程看不到之后改的 PATH**（改完不重启不生效）。
  药方：`toolpath.bash_exe()`（`$BASH` → PATH → Git Bash 前缀 → `/bin/bash`），脚本里别再写 `"bash"`。
- **`watchd.sh` / `wake.sh` 的"干净 PATH"在 Windows 上把解释器也清掉了**：那串
  `/opt/homebrew/bin /usr/local/bin …` 在 Windows 上一个都不存在，`command -v python3` 落空后回退到
  `/usr/bin/python3`（也不存在）——守夜进程起得来，但什么都不跑。药方：`uname` 判 MINGW/MSYS/CYGWIN 时
  **追加继承来的 PATH**（Git Bash 里它已经是转换过的 Windows PATH；只按冒号切，Windows 路径带空格）。
- **同一个目录有两种写法**：`session new` 回 `D:/repo`，`session list` 回 `D:\repo`。
  `exclude_cwd_prefix` 原来是裸 `startswith`，于是**总管会扫到自己的会话**（自我唤醒的死循环）。
  药方：`scan.cmp_path()`（两侧都过 `normcase`+`normpath`），比较还要求分隔符边界，
  免得 `D:\repo` 顺手把 `D:\repo2` 也排掉。
- **推论**：`.sh` 不能直接当 `argv[0]` 起（Windows 上不是可执行文件），`wake.sh` 必须由 bash 拉起来。
- **pid 有两套编号**：Git Bash 的 `$$` 是 MSYS pid（这台机器上是 311 这种小数），Windows 进程表里
  是 23452——`liveness.py` 拿文件里的数去 `OpenProcess`，必然说"没在运行"，而扫描器明明每 30 秒在写日志。
  药方：`watchd.sh` 写 `/proc/$$/winpid`（没有就退回 `$$`），重复启动的判断也用 `ps -W` 的第 4 列
  （WINPID）而不是 `kill -0`；升级时用 `_any_alive` 同时按两套编号查，免得旧 pid 文件让新进程误判后起第二份。

### D28 管道里的 stdout 是**本机代码页**，不是 UTF-8（唤醒卡因此整卡乱码）
- **症状**：手机端唤醒卡是一整片乱码（`# 一次唤醒回合` 变成 `# һ´λ½ÐѻغÏ`），而回复本身正常。
  模型那边收到的也是乱码——坏在**生成与传递之间**，不在模型。
- **成因**：Windows 上 Python 的 stdout 若不是终端而是管道，编码取**本机 ANSI 代码页**（简体中文即 cp936），
  与 `sys.getfilesystemencoding()` 无关、与 `PYTHONUTF8` 无关（只是没设）。实测
  `b'# \xd2\xbb\xb4\xce...'` —— 这就是 "一次唤醒" 的 GBK 字节。
  `wake.sh` 用 `"$("$PY" .../turn_prompt.py")"` 把这些字节捕获成参数，Git Bash 的 msys 运行时按 **UTF-8**
  转 UTF-16 交给 `future.exe`，非法字节被就地毁掉，于是整段中文成了乱码。
- **药方（两层，缺一不可）**：
  1. 打印中文的脚本自己在 `main()` 开头调 `toolpath.utf8_stdout()`（`stdout.reconfigure(encoding="utf-8",
     newline="\n")`，stderr 用 `backslashreplace` 免得诊断自己崩）；
  2. `wake.sh` 里 `export PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`，覆盖这条链路上被它拉起的其它脚本。
  只做第 2 层不够：脚本还会被 shell、定时任务、自测各自以不同环境启动，"谁打印谁负责"才闭合。
- **别只看字面**：`print()` 成功不代表对。判据是**字节**——`python3 bin/turn_prompt.py | xxd` 或
  子进程 `capture_output=True` 后 `.decode("utf-8")`。`bin/selftest_prompt.py` 现在按这条判据锁住它
  （剥掉 `PYTHONIOENCODING` 起子进程，断言字节可 UTF-8 解码且等于 `compose()`）。
- **同一根源的另一个表现**：`bin/peek.py` 在没设 `PYTHONIOENCODING` 时，打印中文标签直接
  `UnicodeEncodeError` 崩掉（cp936 编不出 `\ufffd`），看到的是"脚本坏了"而不是"编码错了"。

### D29 Windows 上「守护进程静默死掉」：控制台一关，整棵树收到 CTRL_CLOSE_EVENT
- **症状**：总管**完全不醒**，也不报错。用户问"你怎么没被唤醒"。`liveness.py` 说
  `watchd pid N is not running`、`state/health.json` 老了几百秒，而 `logs/watchd.log` 最后一行只是
  正常启动记录，没有 `stop`、没有异常。
- **两条判据，分开看**：
  1. 任务计划程序的 `LastTaskResult` = `3221225786`（`0xC000013A`）⇒ 这是 `STATUS_CONTROL_C_EXIT`，
     即**控制台被关掉/Ctrl-C**，不是脚本逻辑错。正常退出是 `0`。
  2. 进程是**从 agent 的 shell 里**被拉起来的（`liveness.py --ensure` 在场），而不是由调度器直接拉起
     —— 那条链上它继承了那个控制台。
- **成因**：`subprocess.Popen(..., start_new_session=True)` 在 Windows 上是**静默忽略**的
  （POSIX 参数），所以 `--ensure` 拉起的 watchd 与调用者同一个控制台；那个控制台消失时，
  Windows 给整棵树发 `CTRL_CLOSE_EVENT`，bash 直接死，**不执行任何 trap**。
  同一台机器上同一天连起的三个实例都是这么没的。
- **药方 1（代码，已修）**：Windows 上用 `creationflags=CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS`，
  子进程根本没有控制台可丢；POSIX 仍用 `start_new_session=True`。
- **药方 2（调度器，必须做）**：`RestartOnFailure` **兜不住这种死法**——任务被判为已结束，
  停在 `Ready`、`NextRunTime` 为空，999 次重启一次都没触发。让**调度器判活**：登录触发器加
  `PT5M` 重复间隔 + `MultipleInstancesPolicy=IgnoreNew`（活着的那次被直接跳过）。
  这是唯一一个**不依赖本进程自己**的兜底；`liveness.py --ensure` 只能在有人唤醒时救场，
  而"没人醒"恰好就是它死掉的后果（死锁）。
- **判据用退出码，不要用"日志没有报错"**：`Get-ScheduledTaskInfo` 的 `LastTaskResult` 是唯一能区分
  "被杀"与"自己退"的字段。`0xC000013A` 基本等于"控制台没了"。

### D30 同一份任务可能在多台机器上跑：**别用远端分支当租约**，用本机锁 + 让 push 撞车
- **场景**：一个定时任务在多台桌面端各自触发，操作同一个远端分支 / 同一个 PR。
- **先纠正一个曾经写在这里的错误药方**：本文件早先推荐"远端分支 `<prefix>-lease-<user>`，tip 一个空
  commit 记持有者，TTL 到期可接管"。**这条已废弃**，业务仓 `AGENTS.md` 现在明确不要跨机租约，
  原因是它自己引入了新的失败模式：
  - 持有者死掉后，别的机器要空等一个 TTL；TTL 又必须小于触发间隔，于是**跑得久的合法一轮会在自己
    还活着时租约过期**，别人接管、两边同时动同一个分支；
  - 租约的"认自己"依赖机器名/标记的编码一致性，机器名经过 PowerShell → git 参数编码后可能变形，
    认错就把自己的租约当成别人的，每轮都自锁跳过；
  - 它是个**额外的分布式状态**，要自己实现原子性与过期，而 git 已经有一个：push 本身。
- **现在按业务仓 `AGENTS.md` 的做法**：
  1. **本机串行化用文件锁**，且只锁必须串行的那一段：`$(git rev-parse --git-dir)/finish.lock`，
     用 `set -o noclobber`（PowerShell 用 `[System.IO.File]::Open(..., 'CreateNew', 'Write', 'None')`）
     覆盖"合并进 `dev-<user>` + push"这一个临界区。实测：两个会话同时 `git merge --no-ff`，
     六轮里零轮干净（`index.lock` 冲突）。锁等 10 分钟就放弃并报告，超过 10 分钟的锁视为死掉的
     会话，删掉并说明。
  2. **跨机器靠 push 撞车来解决**：`git push origin dev-<user>` 被拒 ⇒ `git fetch origin`、
     rebase 到 `origin/dev-<user>`、重跑受影响的检查、再 push。**永远不 force**（合并后的 post-merge
     reset 除外，且必须 `--force-with-lease`）。这条不需要任何共享状态：远端 ref 本身就是 CAS。
- **所以本设施派出的这类任务（如巡检）判定"要不要动手"仍然只需要看本地事实**：`git worktree list`、
  分支与 `origin` 的差异、有没有 open PR。**不要**再去读 `pr-sweep-lease-*` 之类的东西——
  `origin` 上不该有它。
- **PowerShell 5.1 的两个必踩点**（写这类脚本时真踩到）：
  - **`$HOST` 是保留变量**（自动变量 `$Host`），赋值直接报 `Cannot overwrite variable Host`；
    同一族还有 `$HOME`、`$PID`、`$?`。用 `$SWEEP_HOST` 这类带前缀的名字。
  - **原生命令的 stderr 会被 PS 当成错误记录**（`To <remote>`、`Everything up-to-date` 都会红屏），
    用 `$null = git … 2>&1` 收掉，判据只看 `$LASTEXITCODE`——不要靠输出里有没有 "error" 来判胜负。

### D31 改定时任务提示词前**先读库里那份**，不要从 `.private/` 的旧副本改

**症状**：把另一个会话刚改进的规则整段抹掉，而且没有任何报错——任务照常跑，只是少了那几条规则。

**实测（我自己踩的）**：要给「dev 分支 PR 合并」加一条租约规则，
我从 `.private/pr-sweep-prompt-v13.md`（我上午自己存的副本）改出新文本再 `future task edit --prompt-file`。
而**另一个会话在同一小时把任务库里的版本升到了 v15**（加了「`cancelled` 不是红」等一整段，还把
"每 3 小时值班"改成"由触发器决定"）。我的写入按"整份替换"生效 ⇒ v15 的内容被 v13 基线覆盖，
版本号跳到 16，**只有从 v15 才开始存在的段落消失**。

**为什么这是个通用坑，不只是粗心**：
- `future task edit --prompt-file` 是**整份覆盖**，没有合并、没有"基于最新版本"的前提检查；
- 任务库是**多会话共享的可变状态**（另一个会话/桌面面板/手机都能改），而 `.private/` 的副本是**快照**；
- 失败完全静默：语法合法、长度合理、任务照常运行。

**规定**：
1. 改任何定时任务提示词，**源文本先用 `future task show <id> --json | jq -r .prompt` 取库里当前的**，
   改完再写回；`.private/` 里只留**改完后**的版本，别拿旧副本当基线。
2. 写回后立刻**逐条核对**你想要的规则与**你不该丢的规则**都在：
   `future task show <id> --json | grep -c '<每条关键规则的特征串>'`。
3. 发现版本号跳了不止 1（例如你以为会变 14、结果变 16），**立即怀疑有人插进来改过**：
   ```bash
   sqlite3 ~/.future/tasks/tasks.db \
     "select version,status,length(prompt) from task_prompt_revisions
      where task_id='<id>' order by version desc limit 8;"
   sqlite3 ~/.future/tasks/tasks.db \
     "select prompt from task_prompt_revisions where task_id='<id>' and version=<丢失的版本>;" > /tmp/recovered.md
   ```
   **每一次写入都会留一个 revision 行**，所以被覆盖的版本能从库里原样取回——这是补救的唯一依靠，
   也是这条规定值得写成流程的原因。

### D32 相邻字符串字面量会**静默拼接**：`"--note" "…"` 是一条参数，不是两条
- **表现**：`future session approve <sid> <rid>` 永远返回 `unknown option: --noteauto-approved …`（rc=1），
  而调用方照旧把这条请求记成「已批准」——于是**自动批准从上线起一次也没成功过**，账本却全是「→ approve」。
  同一行里 `approved += 1` 写在 `if not dry:` 里、与 `rc2` 无关，失败因此没有出口。
- **判据**：凡把参数列表写成多行、又在中间漏掉逗号的地方，`python3 -c "print([...])"` 打一次就看得见；
  **更可靠的是给 argv 单独一个函数**（`approvals.py: approve_argv`）让自测钉住形状，
  而不是靠读代码时眼睛不滑。
- **配套规矩**：外部命令失败（`rc != 0`）时不要计作成功，也不要只写进「已批准」那种单面账本——
  要么写 `failed` 并算进预算，要么重试，二者都不能悄悄吞掉。
- 探针：`bin/selftest_deny.py` 的 `approve argv` 一条（19 个用例里的最后一个）。
- **同一处的下一层**：修好 `--note` 之后，让 approve 失败的换成了**请求 id 的键名**——agent 给的 payload 是
  `approval_request_id`（snake_case），而脚本读的是 `requestId`/`id`，于是 argv 里送下去的是字符串 `None`，
  CLI 回 `approval request `None` is not pending`，账本记 `failed`。判据同上：**拿一次真实的 payload 核键名**，
  不要按命名习惯猜；`bin/selftest_approvals.py` 现在同时钉住三种拼写与“payload 用哪一种”。
- **成本一层**（同一轮发现的）：`auto` 原来对**每个会话**跑一次 `future session approvals`，几百个会话就是几百个
  CLI 子进程/轮，每个都留一对 `~/.future/cli/logs/cli.<pid>.log`。取数已收窄到“可能变过的会话”
  （`approvals.py: candidates()`）；两条信号必须**实测**而不是推理得出：停在权限请求上的会话 `isStreaming` 仍为 true，
  请求出现时该会话的 `updatedAtMs` 会跳变。取数方式可以省，决策规则一条都不能省。

### D34 `cd` 只改 `$PWD`；`.NET` 的 `[System.IO.File]` 仍按**进程启动目录**解析相对路径

本机的 shell 工具每次调用都是新进程，且**进程 cwd 固定为调用方的会话目录**，`cd` 改的只是 PowerShell
自己的 `$PWD`。于是同一条命令里会出现两套解析规则：

```
cd $env:TEMP
$PWD                                  → C:\Users\me\AppData\Local\Temp
[System.IO.Directory]::GetCurrentDirectory()
                                      → D:\<会话 cwd>        ← 没跟着 cd 变
[System.IO.File]::WriteAllText('rel.txt', 'x')      → 落在 D:\<会话 cwd>\rel.txt
Select-String -Path 'rel.txt'                      → 读的是 $PWD 下的那个
```

**两边都"成功"**，所以脚本不会报错——它写进了 A 仓、读的却是 B 仓的文件。

- **表现**：批量改文件"全都改成功了"，但目标目录里没有改动，另一个仓（常是主 checkout）多出未提交修改；
  别人的会话随后看到脏工作树，还以为是自己的。
- **实测**（Windows + PowerShell 5.1，可复现）：`cd $env:TEMP` 后 `[System.IO.File]::WriteAllText('rel-probe.txt','x')`，
  文件出现在**会话 cwd** 而 `$PWD` 指向 TEMP；`Test-Path 'rel-probe.txt'` 返回 `False`——PS cmdlet 找不到它。
- **对策**：**凡是 `.NET` 静态方法 / `[IO.*]` / 某些库的 API，一律传绝对路径**。
  要在脚本里批量改文件，先 `Push-Location` 并按 `$PWD` 拼绝对路径，或直接用 `Join-Path` 得到绝对路径再传。
- **连带**：`cd <不存在的路径>` 只打印错误、**不终止脚本**，后续相对路径继续落到旧目录上——
  这类失败不会让退出码变红，所以"命令没报错"不能证明它跑在你以为的目录里。
- **自检**：收尾时 `git -C <目标仓> status --short` 与主 checkout 各看一次；
  批量写之后**必须**回读目标文件（例如 `grep` 新内容），不能只看命令没报错。

### D33 用户**正在现场驱动**的会话不要替他批准
- **表现**：用户在某个会话里逐条试「哪些请求不该通过自动审批」，而总管那层的 yolo 自动批准
  会把请求放行——既覆盖他刚点下的拒绝，又让被测对象失去意义。
- **做法**：`state/approval-exclude.jsonl` 一行一条 `{"session": "…", "until": "<本地时间>"}`，
  `approvals.py` 读到就跳过该会话（到期自动失效）。放 `state/` 而不放 `bin/config.json`：
  它点名的是一台机器上的某次会话，不该进 git。
- **反例**：`bin/config.json: exclude_sessions` 只放**长期**排除（例如总管自己）。

### D-假红 报"仓库里有个既有的红"之前，先用 CI 用的工具链复现一次

- **现象**：有人（或两个会话各一次）报告某文件在主干上 clippy 报错、说会让下一个 PR 的 lint job 变红，
  其中一个准备开修复分支。用**仓库 pin 的那一版**工具链跑同一条命令，却是零告警。
- **机制**：`cargo`/`clippy-driver` 会被包管理器装的版本遮蔽。更新的一版**不一定**报得更多——
  旧的 Homebrew 版可能报新版不报的 finding，而 CI 用的是 pin 的版本，于是"主干是红的"在 CI 里根本不成立。
  这类误报的代价不是白干一次：**会有人为不存在的问题改主干**；反过来，真红若被当成"上次也报了"的噪音就会被忽略。
  两侧都贵。
- **判据**：任何"xx 在主干上是红的"的结论，必须在 **CI 用同一个工具链**下复现过才成立。
  ```bash
  # 版本号以仓库的 rust-toolchain / CI 配置为准，别凭记忆写
  export PATH="$HOME/.rustup/toolchains/<pin>-$ARCH-apple-darwin/bin:$PATH"
  which cargo clippy-driver            # 确认没落到 /opt/homebrew 或 /usr/local
  cargo clippy -p <crate> --all-targets --no-deps -- -D warnings
  ```
  `--no-deps` 只编这一个 crate，几秒到几十秒；`--all-targets` 必须带上，CI 跑的就是它。
- **附**：报红时把命令、工具链版本、`Finished`／告警原文一起贴出来；只说"我这边 clippy 报错"，
  接收方无法判断是真红还是版本差。

### D-app 探活 别用 System Events：它把 TCC 弹窗记到终端头上，而且"点了不允许"不会停

- **症状**：用户的终端应用（Ghostty/iTerm/…）反复弹「"'X' 想要控制 'System Events'"」。点"不允许"，
  下一轮换个会话还会再弹。用户以为是终端有问题。
- **机制**：`osascript … tell application "System Events" …` 发的是 Apple Event，而 TCC 把**调用者**归因到
  承载 shell 的终端应用，于是弹窗写的是终端名。拒绝只对**那一次**请求生效，调用方（agent 会话）并不知道自己被拒，
  下次照样再问；而它要的答案（某个 app 是否在可见应用列表里）本来不需要 Apple Events。
- **判据**：探活/枚举窗口用不需要授权的接口——`lsappinfo info -only bundleid <name>`、`CGWindowListCopyWindowInfo`、
  或直接看进程表与 `open -Ra <app>` 的退出码。**只有确实要驱动 UI（点击/敲键/读控件）时才用 System Events**，
  那时应当由用户当场授权，而不是让它在后台反复弹。
- **别用 PATH 遮蔽去修**：把 `~/.local/bin/osascript` 放前面只会让"你自己那个工具 shell"里不弹；
  用户的登录 shell 里 `which -a osascript` 仍先命中 `/usr/bin`（`~/.zshrc` 后面还会有若干行继续 prepend，
  把先前的目录挤到后面）。**判据要写成 `zsh -lc 'which -a osascript'` 的第一行**，不是"我这边跑通了"。
  往 PATH 里插一个遮蔽系统工具的 shim，还会影响所有脚本和用户自己的手动调用——代价大于收益。
- **真正的修法在指令侧**：写给会话的话里不要出现"用 System Events 看可见应用列表"；改成 `lsappinfo` 或进程表。
  一条会反复触发的弹窗，根因通常是**我们自己的措辞**，而不是用户机器上的某个开关。

### D-cli 换掉 `~/.future/bin/future` 不会换掉正在跑的 `future host`：全部 CLI 报 "Incompatible Future Agent"

- **症状**：所有 `future …` 子命令（`session list`、`status`、`send`、`peek`、`task`、`life-docs`）
  一起失败，错误是同一条：
  `Incompatible Future Agent: Agent version <A> does not match client <B>; local startup is forbidden`。
  会话本身**还在正常跑**（它们的 run 由已在运行的 Agent 提供），所以看起来"一切正常、只是我看不见东西"。
- **机制**：`~/.future/bin/future` 被替换（重新构建/安装，例如本地开发装了个 `+local` 构建）时，
  已经在跑的 `future host` 进程仍是**旧镜像**——`/proc/<pid>/exe` 会显示 `… (deleted)`。
  新 CLI 与旧 Agent 的协议版本对不上，于是拒绝连接，并且因为已有 Agent 在跑，也不肯另起一个本地 Agent。
  实测：host 起于 08:41（旧构建），磁盘上的二进制 09:10 被换 → 09:08 起 `session list` 全红。
- **判据**：`tr '\0' ' ' < /proc/$(cat ~/.future/agent/agent-instance.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["pid"])')/cmdline` 与
  `stat -c '%y' ~/.future/bin/future` 一比：进程启动时间**早于**二进制 mtime，就是它。
  `ls -l /proc/<pid>/exe` 显示 `(deleted)` 是决定性证据。
- **修法**：重启 host（停掉那个 `future host` 再起 / 重开 FutureOS 应用），让 Agent 与 CLI 用同一个二进制。
  **没有环境变量能绕过版本握手**，也不要绕过：Agent 与 CLI 协议可能真变了，硬连有写坏 `agent.db` 的风险。
  重启会杀掉**所有在跑的 run**（含正在答用户的那一条），所以要么让用户动手，要么先回一句再等下一轮。
- **对总管的后果：静默变瞎**。唤醒链是 `scan.py → decide.py → wake`，scan 全红 → 事件扫不进来 → 不唤醒，
  而 `liveness_minutes`（180）即使到点也要靠 CLI 才能醒。所以 `bin/blind.py` 在 `watchd.sh` 里紧跟 `scan.py`：
  连续 2 次同错误就 `notice.py add` + 重写 `reports/blind.md`，恢复时只打一行。
  **排查入口顺序**：`reports/blind.md` → `state/health.json`(带 stderr 原文) → `grep -i blind logs/scan.log`。

## `taskruns.py digest` 的输出是**快照**，不是实时状态

`bin/taskruns.py digest <task-id>` 只把摘要**写进** `state/task-digests/<task-id>.md` 就返回路径，
之后文件不会自己更新。隔一段时间再 `cat` / `sed` 那个文件，看到的是**它上次被生成时**的台账，
里面那条 `running` 可能早已完成——实测因此把一轮 16 分钟就跑完的任务误判成"卡了 54 分钟"，差点为它派工单。

- 要现在的状态：**重新跑一遍 `bin/taskruns.py digest <task-id>`**，或直接问权威来源
  `future task runs <task-id> --limit 2 --json`（字段含 `status` / `startedAt` / `finishedAt` /
  `resultSummary` / `errorMessage` / `finishReason`）。
- 判断"卡住"的门槛要用**同一任务的历史用时**做基线（本例 12–40 分钟都出现过），
  别用"我这个模型觉得久"。
- 凡是"我在某个文件里读到它还没结束"这类结论，先问一句：**这个文件是谁在什么时候写的**。
