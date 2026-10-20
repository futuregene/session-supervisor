# 架构与数据流

> **何时读**：想知道「为什么被唤醒」「某脚本干什么」时

## 三层结构

```
调度器（macOS launchd / Linux systemd --user / Windows 任务计划程序；见
runbooks/supervisor-daemon.md）——只负责让下一行一直活着
  └── bin/watchd.sh                 每 config.scan_interval_seconds 一轮
        ├── bin/scan.py             读 session list + 变化的会话 → inbox/pending.jsonl（无模型调用）
        ├── bin/approvals.py auto   自动批准权限请求（带拒绝清单）
        └── bin/decide.py           该不该唤醒？→ state/turn_payload.json
              └── bin/wake.sh       future run --session <总管会话> --cwd <工作区> $(bin/turn_prompt.py)
                    └── 总管回合：读 POLICY/AGENTS → 取锁 → 处理事件 → 代答/追问/升级 → 记录 → ack → 放锁
```

关键点：**扫描与判断是确定性的（不烧 token）**，模型只在被唤醒的那一个回合里跑，
且回合有硬预算（`config.max_messages_per_turn` 个会话、25 次工具调用）。

### 哪些会话根本不该被看见：goal worker

`future goal` 拉起的 worker 由它自己的控制平面驱动（一轮一个 todo、claim/lease），
所以**发送与唤醒两侧都跳过**它：

- **发送**（`bin/send.py`）：命中即拒发、退出码 4、零副作用；
- **唤醒**（`bin/scan.py`）：直接不进 `active`，既不产事件也不唤醒，跳过名单写进 `logs/scan.log`。

两边都是"**默认跳、supervisor 豁免**"（supervisor 自己也可能跑在一个 goal 之下，误判就没人唤醒它了）。
判定逻辑只在 `bin/goal_sessions.py` 一处；信号强度按成本分：发送侧面向一个目标，可查绑定 + 系统提示边界；
扫描侧每轮要过全部会话，只读 goal 的 registry 与 events（一次 `open`/个）。
开关：`relay_skip_goal_sessions` / `scan_skip_goal_sessions`。

## 各脚本职责

| 脚本 | 职责 | 输出 |
|---|---|---|
| `bin/watchd.sh` | 常驻循环，串起 scan → approvals → decide | `logs/watchd.log` |
| `bin/scan.py` | 对比 `state/state.json` 基线与会话列表，产出事件；跳过 goal worker | `inbox/pending.jsonl`、`state/health.json` |
| `bin/approvals.py` | `auto` 批准待批权限（带拒绝清单与每小时上限） | `state/approvals.jsonl`、`reports/approvals.md` |
| `bin/decide.py` | 节流后决定是否唤醒，并 spawn `wake.sh`；先把被取代的事件折叠掉（`inbox.fold_superseded`） | `state/turn_payload.json`、`state/wakes.jsonl` |
| `bin/wake.sh` | 先问 `monitor_roll.py` 该唤醒哪个会话，再用 `future run` 跑一个回合 | `logs/wake-<ts>-<reason>.log` |
| `bin/monitor_roll.py` | 会话过长时用 `future session reset` **原地重置本会话历史**（不换会话）；本机没有该子命令或重置失败时才另开新会话并归档旧的 | `state/monitor_session.txt`、`state/monitor_reset.json` |
| `bin/inbox.py` | 事件台账：`list --brief/--json`、`count`、`ack`、`ack-all`、`fold` | `inbox/pending.jsonl` → `handled.jsonl` |
| `bin/lock.py` | 单飞锁：`acquire` / `release` / `status`（归属于"回合"，非 PID） | `state/agent.lock` |
| `bin/guard.py` | 防打转：`check <id>` / `record <id> "<note>"` / `show`。**口径以回读 transcript 为准**（`observed_sends()`），`state/sent.jsonl` 只是自己记的、会漏 | `state/sent.jsonl`、`state/observed_sends.json`（180s 缓存） |
| `bin/send.py` | 唯一的转达通道：抬头、两道闸、额度、待补发队列 | `state/sent.jsonl`、`state/deferred-sends.jsonl` |
| `bin/toolpath.py` | 统一发现 `future` / `python3` 可执行文件 | stdout |
| `bin/taskruns.py` | 定时任务台账 → 事件：失败/空输出/该复盘了；`digest` 写复盘摘录 | `state/taskruns.json`、`state/task-digests/` |
| `bin/selfcheck.py` | 全部静态探针（= PR 的 CI） | stdout |

## 事件种类

| kind | 优先级 | 含义 | 常规处置 |
|---|---|---|---|
| `approval_pending` | high | 会话卡在权限请求 | `approvals.py` 已处理，回合只需知悉 |
| `run_error` / `failed_run` | high | 运行失败 | 读最后记录；能重试/纠输入就补一条，否则升级 |
| `awaiting_user` + `questionLike` | medium | 停下并疑似提问 | 证据可推则代答；涉及用户私事或白名单则升级 |
| `awaiting_user` | low | 一轮刚结束 | **不要默认搁置**——查收尾是否还有缺口、是否有人在等回复 |
| `stalled` | medium | streaming 中长时间无新记录 | 判断真卡假卡，可恢复则催一次 |
| `new_session` | low | 新会话出现 | 默认积极，看是否值得介入（见 `POLICY.md` §A） |
| `task_run_failed` | high | 定时任务某一轮非成功结束 | 读错误；可改提示词避免就改，平台侧则记录（见 [task-review.md](task-review.md)） |
| `task_run_empty` | medium | 定时任务“完成”了但无任何输出 | 查是否被指示“无需动作就不输出” |
| `task_review` | medium | 定时任务已积累 N 轮未复盘 | 读 `taskruns.py digest`，改就改提示词，最后 `reviewed <id>` |

同一会话同一状态只报一次（去重键含事件类型、时间戳与 ordinal，见 `scan.py::emit`）。
`task_*` 三类来自 `taskruns.py`：**无会话可看**（任务跑完即删会话），按 run id 去重，任务第一次被看见时只建基线。

## 状态文件

| 文件 | 作用 |
|---|---|
| `state/state.json` | 去重基线、每会话最后状态、历史峰值 `peakSessions` |
| `state/health.json` | 扫描健康：`ok`、会话数、`resync`/`bulk`/`degraded` 标志、事件数 |
| `state/wakes.jsonl` | 每次唤醒的原因与当时待处理数 |
| `state/sent.jsonl` | 总管发过消息的记账（guard 写入） |
| `state/deferred-sends.jsonl` | 等待性拒发（mid-run / 无额度）的待补发队列 |
| `state/approvals.jsonl` | 自动批准的逐条明细 |
| `state/monitor_session.txt` | 总管会话 id（wake 目标；轮换后由 `monitor_roll.py` 改写） |
| `state/agent.lock` | 单飞锁（易变，gitignore） |
| `state/turn_payload.json` | 本次唤醒的载荷（易变，gitignore）。**不作为附件发给会话**——`bin/turn_prompt.py` 把它渲染成消息里的「待处理事件」小节；事件全文用 `bin/inbox.py list --json` 按需取 |

**「同一（会话, 事件类型）只留最新一条」**：更早的那条描述的是已不成立的状态（会话又停过一次）。
由 `bin/decide.py` 在造载荷前折叠进 `inbox/handled.jsonl`（带 `foldedInto` 指向存活者，可查、可回退）。
之所以要「折叠」而不是「只显示最新」：卡片上的 id 就是 `ack` 句柄，被隐藏却仍留在 `pending.jsonl`
的事件永远不会被 ack——它会一直计入 `pendingCount`、一直蹭心跳、并在之后每张卡片上重新出现。
折叠范围 = `config.fold_superseded_kinds`；`failed_run`/`run_error` 不在其中
（每次失败是一个独立事实，且优先级 high，本就会立刻唤醒）。

## 总管会话为什么会"提前"死掉

`monitor_roll.py` 的轮换**不能只看 `contextPercent`**。压缩历史时**用户消息原文永不丢弃**，
而压缩后的历史预算是固定的（`agent/src/compaction/budget.rs` 的 `MAX_EXPANDED_HISTORY`；
`compaction/semantic.rs` 在 `required > maximum_target` 时**直接报错**，不截断用户文本）。
本设施每次唤醒都追加一条**用户**卡片，于是会话寿命由「唤醒次数 × 卡片大小」决定，而不是总上下文：
可能在上下文百分比还很低时就被打死，之后每次唤醒都以 `context compaction budget exceeded` 失败，
**且失败那次仍把卡片留在历史里**（required 单调增长），所以会话不会自愈，只能重置或轮换。

因此 `monitor_roll.py` 有三个触发条件：① `contextPercent ≥ monitor_roll_context_percent`；
② 已唤醒次数 `queryCount ≥ monitor_roll_max_queries`；③ **上一次唤醒的日志里有卡死报错**（`last_wake_wedged()`）——
这是唯一早于百分比的信号。它认两个标记：压缩预算耗尽，与原地压缩后的拒写
`cannot append to a compacted settled run`。

### 命中触发：原地重置，不换会话

三个触发条件都先走 `future session reset`：把**本会话自己的历史**就地丢掉，
id／名字／cwd／模型／权限／两段 system prompt／侧栏位置**全不变**，
Agent 会先把**完整**的旧条目写一份无损备份（`~/.future/agent/session-backups/<id>-<ts>.json`）。

`config.monitor_reset_in_place`（默认 true）控制这条路径；保留的尾部用户轮数
`monitor_reset_keep_last`（默认 2）。只在下面几种情况下才回退为"另开一个会话并把旧的改名归档"：

- **本机 `future` 还没有这个子命令**（`future session reset --help` 非 0）——需要重新安装二进制；
- **CLI 已是新版、但 agent 进程还是旧构建**（dry-run 返回 `unknown command: reset_session_history`）。
  CLI 与 agent 是**两个独立升级的层**：安装只换磁盘上的二进制，**已经在跑的 agent 进程不变**，
  所以要让 reset 生效必须重启 agent。
- **重置失败**（非零退出）。卡死那条会再用更强的 `--all` 试一次（丢全部条目、仍保留身份），两条都失败才轮换。
- **会话忙不算失败**：agent 在"有 run 在跑 / 正在压缩"时以 `session_busy` + `retryable: true`
  **拒绝**而不是打断。这种拒绝反而**证明 agent 认识这个命令**，所以能力探测把它算作"支持"，
  触发时**保持会话、下次唤醒再试**，**不**回退为另开会话。

能力探测因此是**两段**：先 `--help`（问 CLI），再对当前会话做一次 **dry-run**（默认不改任何东西，问 agent）。

### 按需重置：`state/reset_requested.json`

三个触发都是"到阈值才动手"，而用户可能就想**现在**清掉历史。`monitor_roll.py` 因此还认一个一次性请求文件：

- 内容是这个会话的 id（或 `{"session": "<id>"}`），**只有 id 与当前总管会话一致才生效**；
- 命中时**不看阈值**直接原地重置，理由记为"按请求原地重置"；
- 一次性：成功后**删除**该文件；只有**忙**这一种情况保留，留到下次唤醒重试；
- 想撤回：在下次唤醒前删掉该文件。

### 重置之后的读数与计数

- 重置后 `contextPercent` 要**下一次请求**才更新（与压缩同一个坑），所以每次重置记在 `state/monitor_reset.json`：
  `RESET_SETTLE_SECONDS`（300s）内不重复请求、也不轮换；到期后若上下文**仍在阈值以上**，
  才认定为"重置没腾够"转为轮换（回退路径就是旧行为，不会走向卡死）。
- **唤醒次数触发器按"上次重置时"重新起算**：`queryCount` 在会话**记录**里、重置不碰它，
  所以标记里存下重置那一刻的 `queries`，再满 `max_queries` 才算新的一轮。

### 为什么重置优于压缩

压缩必须**逐字保留用户原文**，而本设施每轮都往历史里追加一张**用户**卡片——溢出的正是它。
重置是唯一能真正丢掉它的手段。

**「先原地压缩」这条路已关掉**（`config.monitor_compact_first: false`）：压缩本身能让 tokens 大幅下降，
但会话可能从此**再也收不下新事件**（`cannot append to a compacted settled run`），
且压缩后 `contextPercent` 读数变成 0%，阈值触发器再也不响——会话会**永久**卡死。
这种失败已加入 `monitor_roll.py` 的卡死触发器。**想重新打开它**：先修平台，
再在一次性会话上验证"压缩后仍能发言"，别直接对总管试。
`monitor_reset_in_place: false`（配合 `monitor_compact_first`）可整条回退为旧逻辑。
