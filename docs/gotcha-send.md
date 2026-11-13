# 坑 · A · 会话与发送（往别的会话发消息）

> **何时读**：往别的会话发消息时（`send.py` 之前/之后）

（分类 A；来由与真实经过见 `.future/memory/`，本文件只留机制）

## 已由 `bin/send.py` 自己拦下，不再展开

这些坑现在**工具会在发生的那一刻拒绝并说明原因**，判据在代码与测试里；展开留着没有增量。
要问"为什么当初会踩"，去 `.future/memory/incidents/`；要改判据，先读对应探针。

| 坑 | 钉在哪 |
|---|---|
| 抬头格式（A11–A14：`frame()`、`非用户本人`、只一行主题） | `bin/send.py::frame()` + `check_relay_format` |
| 脚本互调不能经 `future` 前缀（曾让护栏静默失效） | `run_py()` + `check_send_guard_invocation` |
| 被拒发的消息不静默丢（进 `state/deferred-sends.jsonl`，下轮卡片重提） | `defer()` / `bin/turn_prompt.py::_pending_sends()` + `selftest_prompt` |

**写消息时要记住的三件事**（其余交给工具）：只写正文、主题走 `--note`；
被拒是零副作用，改一句话重发即可；正文别放 `/tmp`（重启会清）。

### A1 `future run --session <id>` 会改写那个会话的 cwd
- **表现**：给既有会话发一条消息，它的 cwd 变成**调用者**的目录，并重新归到调用者的 workspace 列表。
- **成因**：`run` 子命令把 `cwd` 兜底成 `current_dir()`，所以运行配置里的 cwd 永远非空，
  客户端据此对既有会话调用 `set_cwd`。服务端本身不覆盖会话元数据。
- **对策**：发送**永远**带 `--cwd <对方的 cwd>`（先 `future session info <id> --json` 读，读不到就不发）；
  已被改写的用 `future session set <id> --cwd <原值>` 复原。
- **所以永远用 `bin/send.py`，不要手写 `future run`。**

### A2 发进去的消息在对方那里是**裸用户消息**
- **表现**：对方无法分辨这条来自另一个会话，会当成用户本人下的指令。
- **对策**：由 `bin/send.py` 的 `frame()` 加统一抬头（见 A12）；涉及用户个人事实/偏好的不代答（见 C1）。

### A3 拦不住一个**正在跑**的回合：更正会迟到
- **成因**：`busy_policy = enqueue_if_busy`（append）。被唤醒的会话一轮可能跑很久，
  更正只在其运行结束后才被记录——那时代码已在按旧指令执行。
- **对策**：**对方还在跑就不要发**。`send.py` 自己查 `session status --json` 的 `isStreaming`，
  仍为 `true` 就**拒发并返回退出码 3**，零副作用（不写 `relay-out.txt`、不记护栏、不占额度），
  并把这条排进待补发队列（A21）。等下一次唤醒再发——事件留在 `inbox/pending.jsonl`，下轮按真实进展重判。
  唯一例外是真要中止/纠偏：显式 `--force`（此时也仍**禁止** `--steer`）。

### A4 同一会话有发送额度，且必须在发送前 check
- **对策**：`python3 bin/guard.py check <id> || skip`，发送后 `record`。额度见 `config.max_messages_per_session_*`；
  单回合最多 `max_messages_per_turn` 个会话；同一会话同一问题只发一次，不重复、不争论。

### A5 会话的 cwd 决定它在桌面端归到哪个 workspace
- **对策**：把 cwd 当会话的**对外可见状态**看待，不做静默修改；`future session set --cwd` 是唯一正当入口。

### A15 不要往 `future goal` 启动的会话里发消息，也不要唤醒它
- **口径**：目标是 `future goal` 拉起的 worker 就不发；`scan.py` 也跳过它（不产事件、不唤醒）。
  理由：worker 由自己的控制平面驱动（一轮一个 todo、claim/lease），再掺一个驱动者会搅乱节奏；
  发现的问题该进 `journal/`/`reports/`。
- **识别**（`bin/goal_sessions.py`，三个信号强→弱）：
  1. **平面自己的绑定**：`goals/<goal>/events.jsonl` 的 `worker_session_bound` 事件写着 `agent_id → session_id`。
     精确、不怕改名；按子串预过滤再解析，一个 goal 一个文件，扫描器付得起。**主力信号。**
  2. 会话记录下来的 system prompt 里有 goal 边界标记。对真实 worker 常为空，是兜底；每人一次 `session status`，
     扫描器付不起，所以只在 `send.py` 里用。
  3. 会话名 = goal objective 的截断（带尾部 `…`）。免费，但一改名就全瞎。
- **roots**：固定表**加上会话自己的 cwd**——goal 的 registry 常在 worktree 里。
- **行为**：命中则**拒发、退出码 4**，零副作用；`--force` 是例外。开关 `config.relay_skip_goal_sessions`
  与 `config.scan_skip_goal_sessions`。
- **supervisor 会话被豁免**（比对 `state/monitor_session.txt`）：总管自己就跑在一个 goal 之下。
- **可见性**：跳过的会话写进 `logs/scan.log`；读不到 supervisor id 时写 warning（意味着豁免没生效）。

### A16 代答前先读该会话**最近的 user 消息**：提问可能早被用户答了
- **表现**：扫描器把"assistant 收尾带问号"标成 `awaiting_user`，但用户可能**已经回过**。
- **判别**：`future session transcript --session <id> --select user --all --truncate 300`（几百字节），
  把**最后一条 user 消息的时间戳**与该事件的 `detectedAt` 比：user 更晚 ⇒ 事件已过期，**只 ack，不发消息**。
- **两道独立的闸**：busy 闸只在对方跑着时有效；"用户已经答了"的场景对方恰好是**空闲**的，两者不能互相替代。

### A17 结论是"不用做任何事"的消息**不要发**
- **判据（唯一一条）**：**对方是否需要因此改变或执行下一步动作？**
  - 需要（去执行、改口径、**停手**、补证据）→ 发。
  - 不需要（保持现状/不必再做/已过期/纯验收，包括"要不要做 X"而答案是"不做"）→
    **只记录**：`journal/` + `reports/decisions/`（需要用户知道再 `bin/notice.py add`），事件照常 ack。
  - 只有它**已经在做**某件必须打断的事时才是动作 → `send.py --force` 发"停手"。
- **机器闸**：`send.py` 判为只知悉的消息**直接拒发、退出码 5、零副作用**；探针读 `frame()` 推出的 intent，
  所以抬头与闸不会打架。`selfcheck` 的 `relay format` 钉住 `quiet_intent`/`QUIET_LABEL`/`return 5`。
- **这不是让人变消极**："积极处置"仍成立，改的只是**载体**——判清 + 写记录，而不是把每条都变成对方的一次 run。

### A18 刚 `session new` 出来的会话**不在会话列表里**
- **表现**：`session new` 立刻返回 id，但 `session info` 报找不到、`session list` 也没有——直到它跑完第一次 run。
- **正解**：同一时刻 `session status <id> --json` **能读**（问的是活着的会话，不是库里的行）且带 `cwd`。
  `send.py` 的 `resolve_cwd()` 先试 `session info`、失败回退 `session status`；
  用 `python3 bin/send.py --new --cwd <仓> --name "<功能>" <file>` 走这条通道。

### A19 长跑会话反复"停下等 worker"时，要改的是**它的收尾时机**
- **成本**：每次回合结束都要把整份上下文重读一遍才继续，反复停就是反复重建——那才是真成本。
- **有效的转达**：点明它自己的代价数字；给可执行的办法（在**同一次工具调用里**阻塞等待，空闲就接着干、
  只有确实仍在跑才收尾）；附带可复用零件（owner→session 绑定读进它自己的脚本）；提醒上下文风险。
- **判据没变**：改收尾时机属于动作，值得发；"核实后你等得对"不属于动作，不发。
- **别重复劝**：同一会话有 4h / 每天的条数上限（`bin/config.json` 的
  `max_messages_per_session_4h` / `_day`，现为 8 / 24）；建议一次没被采纳就算了。
  放宽的是"别反复唠叨"，**不是**防打转——那一条是每 10 分钟最多碰 3 个会话的突发限制
  （`max_messages_per_turn`），它没动。

### A20 打包/构建这类**机械动作**自己 dispatch，不要为推动它而 `--force`
- 护栏语义是"纠偏才强发"；"再干一次它已经在干的事"不是纠偏，`--force` 会把护栏磨钝。
- 构建是机械动作（触发 → 等 → 下载 → 报 sha256）⇒ 总管自己做（例如 `gh workflow run <wf> --ref <分支>`，
  再核 `head_sha`）；需要它**判断**改哪个 workflow / 修构建失败时才发消息。
- 成本校准：先确认构建确实被立刻消费，再谈"攒几条一起打"。

### A21 被拒发的消息会**静默消失**——`send.py` 零副作用
- **机制**：目标还在跑（退出码 3）与护栏无额度（退出码 1）是**等待性**拒绝，`send.py` 自己把
  `{"ts","session","file","note","reason","expires"}` 追加/更新到 `state/deferred-sends.jsonl`
  （`defer()`，`file` 存绝对路径，同一 `(session,file)` 原地更新）；发成功后由 `resolve_deferral()` 删行。
  `bin/turn_prompt.py:_pending_sends()` 把未过期且文件存在的行渲染成卡片里的「## 待补发」小节。
- **正文文件别放 `/tmp`**（重启会清）。
- **拒绝不等于入队**：`quiet_intent`（退出码 5）与 goal worker（退出码 4）**不入队**——那是"不该发"，不是"晚点发"。
- **不要 `--force` 绕过**：拒发是既定规矩；例外只有真要它停手/纠偏。

### A22 静默闸会误拒"别问我，直接干"这类指令——它只认词，不认上下文
- **表现**：一条真正的放行指令（开头写「往下推…不用再问」）被 `send.py` 拒发，退出码 5，理由是"判为只知悉"。
- **成因**：`frame()` 只探**开头一行**，而 `QUIET` 里含 `不用再`——它命中的是"别再来问我"这个**关于对话**的短语，
  不是"关于工作"的短语。整句的语义被一个子串压掉了。
- **对策（已做）**：新增 `ANTI_QUIET`（`不用再问` / `不要再问` / `别等我` …）并在扫 `QUIET` **之前把它们从探针里剥掉**，
  每个反例只中和它自己：`「不用再问了，保持现状。」` 剥掉前段后仍含 `保持现状` ⇒ 仍是安静消息。
  两条用例钉在 `bin/selftest_send.py`（已证伪：把剥离逻辑去掉，第一条立刻红）。
- **写正文时的习惯**：要对方动手就**别用"不用再…"开头**；若已经写了，闸会拦，改一句话重发即可——
  被拒是零副作用，正文文件还在。

### A23 `sent` 打印出来不等于送达：中继本身也会失败，而它失败在"已清队列"之后
- **症状**：`send.py` 打印 `sent to <sid> (cwd kept = …); log …`、`guard: OK`、待补发行也被删掉，
  但目标会话**从没收到消息**——用户发现并问「那个会话没有收到你发送的消息」。
- **成因（两层，各自独立）**：中继当时是一条 bash 一行命令
  `bash -c "timeout 1800 <future> run --session <sid> --cwd \"…\" \"$(cat <file>)\""`。
  1. 这台机器的 Git Bash 里 `/usr/bin/timeout` **Permission denied**（`exit:126`），`future run` 从未启动；
  2. 更早一次，`--cwd` 经过 `json.dumps` 再进 bash 双引号，反斜杠被吃掉两次，变成
     `C:Usersunmer.futurebinfuture.EXE`（`exit:127`）。
  两次都是在 `main()` 已打印 `sent`、已 `guard record`、已 `resolve_deferral` **之后**才失败，
  所以屏幕、guard 计数、待补发队列三者都显示"已送达"。
- **判据：不要相信 `sent`，去看日志的 `exit:` 行**。
  `grep -o 'exit:[0-9]*' logs/send-<sid>-*.log`：只有 `exit:0` 才是成功；
  `126`＝命令不可执行、`127`＝找不到可执行文件/参数被 shell 改坏、`124`＝超时被杀。
  **日志文件存在 ≠ 有内容**：失败时可能只有一两行。
- **对策（已做）**：`bin/send.py` 新增 `deliver()`，用**参数列表**启动
  `future run`（不经 shell，超时交给 `subprocess.run(timeout=)`），
  并把子进程**分离**（Windows 用 `DETACHED_PROCESS`，`start_new_session` 在这里被静默忽略）；
  `spawn_delivery()` 会在几秒内回读子进程状态：**立即失败就返回 2**，
  这时**不记 guard、不删待补发**，把消息重新排队并打印日志尾部。
  `bin/selftest_send.py` 新增两条用例（正常投递 exit 0 / 启动即失败 exit 2 且消息进队列）。
- **反过来也不成立**：`exit` 非 0 不等于没送达。重发时遇到过一次
  `Error: Event stream timed out waiting for an event` + `exit:1`，而目标会话里
  **已经有那条消息、`Queries` 也从 1 变 2**（run 在服务端继续跑，只是发起端的 SSE 流先断了）。
  所以判据的层次是：
  1. **最硬的证据＝目标会话的 transcript 里出现你发的那段文字**
     （`python3 bin/peek.py <sid> --grep "<正文里的特征串>"`，命中即送达）；
  2. 其次是目标会话的 `Queries` 从 n 变 n+1 且 `activeRun.state=running`；
  3. 日志 `exit:0` 只说明**发起端的客户端**正常收尾，`exit:126/127` 说明它根本没启动；
     两者都不是"对方收到了"的直接证据。
- **通用教训**：任何"发出去"的动作都要有一条**来自对方或传输层**的证据
  （日志 exit 码、目标会话 `Queries` 从 1 变 2、`isStreaming` 转 true），
  不能只看自己打印的那一行。

### A24 派信里必须写明**在 worktree 里改**，否则对方会直接改共享 checkout

- **症状**：一封信让业务仓的会话"修 CI 探针"，只写了"走临时分支或 PR 都行"，没写边界。对方就地在
  **主 checkout**（`~/future-os`）里编辑并新建文件。用户当场发现并让它撤回（"你是不是在主工作区做修改了"），
  撤回后才把之前被挡住的合并做完——那期间别的会话的 `git merge` 一直是堵住的。
- **机制**：主 checkout 是**全机共享**的单一工作树。它一旦有未提交改动，任何会话的合并
  （`git merge` 需要干净的树）都会失败或被迫等；而被派的会话只看自己的信，看不出"这棵树还归别人用"。
- **派信模板**：凡是要改仓库的指令，正文里固定三段，缺一段对方就可能越界：
  1. `git worktree add .worktrees/<任务> -b <分支> origin/<基线>` 建自己的树，**所有**编辑/构建/测试都在里面；
  2. 主工作区一个字节不要动：不 checkout、不 stash、不写文件、不 `git add`；看到脏文件是别人的，**报告不要清**；
  3. 收尾贴出主工作区的 `git status --porcelain`（必须为空）＋新的 `origin/<基线>` tip。
- **基线用"此刻的"**：信里写死的 commit 会过期（这次派的 `dev-huichen` 在等待期间前进了两笔）。
  写"从当前的 `origin/dev-huichen` 起"，而不是写死 sha。
- **残留物也一并交代**：为一次性校验 `brew install` 之类要明确"留还是卸"——不留一句，对方会来问，
  又多一个回合；默认倾向"留着别卸"（为一个工具重跑包管理器的风险大于收益）。

### A25 派单前把"缺口清单"对着**当前 tip** 重新验一遍：审计报告是快照，不是现状

- **症状**：把一份审计报告的「留给 supervisor 决定」清单原样变成 7 项工单派出去，
  对方做完回报：**其中 5 项在该分支上早已关闭**（另一个 worker 的 `wiki-and-dist` 修复与审计同期落地，
  审计报告成文更早、没回写状态）。7 项只做出 2 项，其余预算花在逐条核实"确实已经做了"上。
- **机制**：审计/普查类报告是**某个 commit 时点的快照**，而它列出的往往是"缺口"——
  恰恰是别人最可能在同时段修掉的东西。审计报告自己的 `Summary` 会写 `Files changed: 0` /
  `New docs added: 0（缺口交 supervisor 决定）`，那句话**只对它自己那一轮成立**，
  不等于"到现在还没写"。
- **判据（派单前必做，逐条 30 秒）**：对每一条缺口，在**当前** `origin/<基线>` 上做一次
  "它是否还存在"的检索，而不是读报告里的描述：
  - 缺章节 → `git show origin/<基线>:<文件> | grep -n '^### <标题>'`；
  - 缺索引行 → `git show origin/<基线>:<索引文件> | grep -i <关键词>`；
  - 数字不符 → 直接数 `git show origin/<基线>:<文件>` 里的值，别信报告里的那个数。
  仍然存在的才进工单，并在信里**写"我已在 <sha> 上验过它仍缺失"**——对方就不必再自己怀疑一遍。
- **顺带**：报告的缺口清单会包含"需要 supervisor 决定"的项，其中不少是**代码行为**问题
  （退出码同质、过渡语义不一致），那不是文档/记录活，也不该由管理者单方面定产品语义：
  挑出来单独记录，不要混进文字类工单。

### A26 未知参数必须报错；`--dry-run` 才是"测试"，发出去不是

- **来由（一次真实误投）**：把 `--action-class observe` 发给一个**还没有闸**的副本，旧版
  `send.py` 把不认识的参数当噪声丢掉，于是照常发信——一条"受限"的调用变成了一次真实动作。
  **静默忽略等于把闸变成建议**：拼错的 flag、旧副本，都能让限制失效。
- **现在的行为**：`bin/send.py` 与 `bin/dispatch.py` 的任何未知参数都是**退出码 2 + 用法**，
  零副作用（`strict_argv` 用 argparse 的默认严格性，不另写宽松解析）。
- **`send.py --dry-run`** 走完全部判定（target / `action_class` / intent / cwd / goal owner /
  mid-run / guard 额度），打印每条结论与"会被哪条闸拦、用哪个退出码"，**退出 0**，
  不建会话、不投递、不写 `state/sent.jsonl`、不进 `deferred-sends.jsonl`。
  `--new --dry-run` 同样只报告"会建哪个会话"，不会建。
- **两个用法**：
  1. 做注入测试（想验证"这条外部事件能不能推动一次发送"）——**必须先 `--dry-run`**，而且要在
     **有闸的副本**里跑（worktree / 刚合并的主 checkout），不要在旧副本上"发出去看看"；
  2. 拿不准 guard 额度、对方是否 mid-run、`action_class` 会不会拦时，先跑一次再决定。
- **它唯一可能写的东西**：`bin/guard.py check` 的 180 秒观察缓存
  （`state/observed_sends.json`）——那不是台账、队列或会话；三个台账文件都逐字节不变。
- **判据**：dry run 的结论必须与真实调用一致（`selftest_send.py` 用同一组 argv 断言两者
  的拒绝码相通），所以它不能变成"绕过闸的第二条路"。

## 派 CI 修复工单前，先看 PR 巡检是否已经开了修复会话

「dev 分支 PR 合并」这条定时任务**自带修复路径**：PR 红时它会开一个名为
`修复 dev 分支 CI：#<PR> <失败项>` 的会话，工单正文写在 `/tmp/prsweep-repair-<PR>.md`，
并在 `.worktrees/<slug>` 里改。实测它先开了、我几分钟后又派了一条同样主题的工单——两条会话改同一处，
add/add 同样的行必然冲突，最后是我那条**改成纯核验**才算没白跑一轮。

- **判据**：`future session list --json | grep 修复` 或 `ls /tmp/prsweep-repair-*`，
  以及那条任务的台账（`future task runs <id> --json`）里有没有 `修复会话` 字段被填。
- **顺序**：先确认巡检已经开过（那就只等它）、或确认它这一轮**不打算**修（PR 已绿/无 PR），再派自己的。
- **为什么值得先查**：重复的补丁不会「多一道保险」，而是互相把对方顶掉；重复的会话本身也是一整个 run 的钱。

## 被护栏拦下排队的正文文件，在补发成功前不许删

- **坑**：`send.py` 因"目标还在跑"把消息排进 `state/deferred-sends.jsonl` 时**只记路径、不存正文**——正文在
  `file` 字段指向的那个临时文件里。收尾清理顺手 `Remove-Item state/tmp-*.txt` 就把它删了：下一轮唤醒卡只能
  报「正文已丢失，重新写一份再发」，那条消息**从未送达**（不是"发出去了"）。
- **判据**：删任何 `state/tmp-*.txt` 之前先查 `state/deferred-sends.jsonl` 里有没有引用它的行；命中就不删。
  发成功后 `send.py` 自己删行，那时文件才可删。
- **为什么值得记**：清理脚本与发送队列**共享同一批临时文件**，名字都是 `tmp-*`、语义却相反——一边是"用完就删"，
  一边是"还欠着、别动"。这类冲突不会报错，只会让一条已经排好的消息静默消失。
