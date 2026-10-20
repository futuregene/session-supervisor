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
- **别重复劝**：同一会话 4h 内 3 条上限；建议一次没被采纳就算了。

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
