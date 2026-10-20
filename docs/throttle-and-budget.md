# 节流、预算与防误报闸门

> **何时读**：调唤醒频率/预算，或怀疑被限流、被误报刷屏时

本机现取值见 `bin/config.json`（它就是运行时读的那份）与 `.future/memory/instance.md`；
花费实测与字节预算见 `.future/memory/budget.md`。
本文件只讲**机制**：每个闸门拦什么、由谁执行、坏了怎么发现。

**改参数只改 `bin/config.json`**：它就在读它的脚本旁边（`bin/toolpath.py::config_path()`
是唯一拼这个路径的地方）。**每个键都可缺省**——脚本各自带默认值，缺了就用默认；文件里
列出的偏"当前取值清单"，不是"必须存在的契约"。唯一要小心的是 `future_bin`：
写成绝对路径就绑死了这台机器，一般保留 `"future"` 让 `toolpath.py` 去发现。

## 唤醒与发送的闸门

| 项 | 机制 | 位置 |
|---|---|---|
| 扫描间隔 | 无模型调用的巡检循环 | `config.scan_interval_seconds`、`bin/watchd.sh` |
| 事件唤醒最小间隔 | 一个事件唤醒后，下一轮至少隔这么久 | `config.min_wake_gap_seconds` |
| 唤醒上限 | 每小时/每天封顶，超出即 suppressed 并记日志 | `config.max_wakes_per_hour/day`、`bin/decide.py` |
| high/medium 事件 | 立刻唤醒，只受最小间隔约束 | `bin/decide.py`、`NOTIFY_PRIORITY` |
| **事件所属会话正在跑** | **不作为唤醒理由**：`awaiting_user` 的前提（已停下等回复）在用户回一句后就失效，此刻也发不出消息（只会排队）。事件**不 ack、留在 pending**，会话空闲后下一轮照常唤醒 | `bin/decide.py` 的 `streaming_sessions()`、`config.max_status_checks_per_cycle` |
| low 事件 | 只有**新** low 事件才唤醒（`has_new_pending`），另受抖动闸限制，最坏延迟 `config.low_wake_minutes` | `bin/decide.py`、`bin/selftest_decide.py` |
| 心跳 | **空闲不发**、**没有新事件也不发**：`pending` 里出现上次唤醒未见过的 id 才唤醒 | `config.skip_idle_heartbeat`、`has_new_pending()` |
| 存活响声 | 即使一直没有新事件，也每隔 `liveness_minutes` 醒一次，避免长静默变成失联 | `config.liveness_minutes` |
| 单回合外发 | 会话数与工具调用数双上限 | `prompts/turn.md`、`config.max_messages_per_turn` |
| 同会话外发 | 时间窗与每日上限，`bin/guard.py` 计数 | `config.max_messages_per_session_*` |
| 扫描深度预算 | 每轮最多深查若干会话，超出 deferred 到下一轮（deferred 的会话不在下一轮被"陈旧"闸门误杀） | `config.max_deep_checks_per_scan`、`bin/scan.py` |
| 熔断 | 存在 `state/PAUSE` 即不唤醒、不自动批准 | `touch state/PAUSE`（删掉即恢复） |

## goal worker 的两道豁免

| 开关 | 作用 | 行为 |
|---|---|---|
| `relay_skip_goal_sessions` | **发送**闸 | `bin/send.py` 拒发到 `future goal` 拉起的 worker（退出码 4）；supervisor 会话始终豁免 |
| `scan_skip_goal_sessions` | **唤醒**闸 | `bin/scan.py` 直接跳过（不产事件、不唤醒），跳过名单写进 `logs/scan.log` |

**注意这是真节流**：worker 卡死也不会再被发现。判定同在 `bin/goal_sessions.py`。

## 扫描器的三种"不正常"标记（`scan.py`）

| 标记 | 含义 | 处置 |
|---|---|---|
| `resync` | 停机超过 `resync_gap_seconds` 后的第一轮 | 只报 high，避免恢复瞬间的伪变化刷屏 |
| `bulk` | 一轮内变化比例过半（时间戳批量刷新） | 该轮只报 streaming 中的会话 |
| `degraded` | **会话列表骤降**（比历史峰值少很多），且**保持到列表恢复** | 记警告 + 跳过 state 裁剪；不把"列表缺失"当成"会话消失/新会话"。确认是主动删除后用 `python3 bin/scan.py --adopt-peak` 清除 |

事件侧还有两道闸门：**新鲜度**（会话真实最后活动时间超出 `fresh_window_hours` 的事件不报，时间戳一律以
transcript 末条为准，不用 `updatedAtMs`）与**基线回退**（事件自带时间戳早于上轮扫描 → 视为重置而非新闻）。

> **为什么 `degraded` 必需**：扫描器只对比"它看得见的会话"。agent 少回了会话时，它只是**默默少看一批**，
> 而 `ok=true`——这种失败没有任何别的信号。判定逻辑是纯函数 `scan.list_shrank()`，
> 单测在 `bin/selftest_scan.py`（`selfcheck` 会跑）。

## 手册为什么要放进 system prompt

唤醒卡是**用户消息**，而压缩历史时用户原文永不丢弃，所以每张卡都按原尺寸永久留在上下文里；
system prompt 每个 run 重建、不进历史，只付缓存读。

切换**不由意图决定、由证据决定**：`python3 bin/apply_prompt.py --apply` 先起一个临时金丝雀会话、
往它的 system prompt 注入随机标记、让它复述——模型真的复述出来才算验证通过，写下
`state/append_applied.json`（含 `prompts/append.md` 的 sha256 与二进制版本），然后删掉金丝雀会话。
只有该记录存在、`canary_ok`、且 sha256 与今天的 `prompts/append.md` **逐字节相同**时，
`bin/turn_prompt.py` 才发"极简卡"；其它任何情况（记录缺失/损坏、手册改过、canary 未过）都
**回退为完整消息**（手册 + 卡片）。`bin/ensure_append.py` 由 `bin/wake.sh` 每轮调用，
发现"文件变了、记录没跟上"就自己重跑一次 canary + apply。
回退：`python3 bin/apply_prompt.py --revert`。
