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

