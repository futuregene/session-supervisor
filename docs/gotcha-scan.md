# 坑 · B · 扫描与事件

> **何时读**：怀疑扫描器报错/误报/漏报时

（分类 B；来由与真实经过见 `.future/memory/`，本文件只留机制）

## 已由 `bin/scan.py` 自己处理，不再展开

这些判据现在都在扫描器里，并有 `bin/selftest_scan.py` 钉住。
文档只留**代码管不了**的部分（下面 B2/B4/B5/B7/B8/B11/B13）。

| 坑 | 现在怎么处理 |
|---|---|
| `updatedAtMs` 被批量刷新 | 事件时间与新鲜度一律取 transcript 末条记录时间；另有 resync / bulk / 新鲜度三道闸 |
| 裸问号误判提问 | `looks_like_question` 只看末尾一段，且要求末行是问句或含短语标记 |
| 去重键含 ordinal 导致同一失败重报 | 去重键改用 **run id**（同一失败实测 4 条 → 1 条） |
| 去重键掺时间戳，同一失败又重报 | `scan.py::event_key(..., stable=True)`：标识（run id）已足够定位事件时，不再把「会话最后活动」掺进去（10-20 实测 11 分钟内 4 条同一 502，每条都触发 high 唤醒） |
| `cancelled` 被算进 `failedRuns` | 失败数改从 run ledger 派生，只认 `failed/error/aborted` |
| 深检预算之外的会话被反复误报 `new_session` | 延迟时即写 `sig`，欠下的公告记进 `pendingNew` |

### B2 冷启动 / 迁移后会一次性吐出全部历史事件
- **对策**：先 `python3 bin/scan.py --baseline` 建基线（可连跑数轮把 deferred 清空），再让守护接管。

### B4 每轮 deep-check 有预算，超出会 deferred
- **表现**：会话多时一轮扫不完，事件分散在多轮才出现。
- **对策**：批量场景连跑 `scan.py`；判断是否清空看 `logs/scan.log` 的 `deferring N sessions` 是否归零。

### B5 判断"这个会话最后的真实活动"用 `--runs`，不要看列表时间
- **对策**：`future session transcript --session <id> --runs` 逐 run 给状态/时长/输入量；
  `--json` 字段名可能变化，解析要防御式（缺字段不要崩）。

### B7 `future run` 不生成标题，但会话名会被另外两处改写
- **表现**：`future session new --name X` 之后名字变成**首条用户消息**，`--name` 丢了（不是模型生成）。
- **成因（三处，只有第三处是模型）**：
  1. **agent 兜底**：`session_name` 为空时用首条用户消息截断当名字；
  2. **桌面端回写**：import 时优先取首条用户消息（前 40 字）并同步回 agent——**只在桌面端在跑时发生**；
  3. **桌面端模型生成**（B8）。
- **对策**：别指望 `--name` 在桌面端开着时存活；名字有语义就事后 `future session set --title`，
  或在 prompt 里把意图写清楚。

### B8 「首答后自动生成标题」只属于桌面端
- **事实**：该功能在桌面端 Tauri 层，不在 agent、也不在 CLI/loop。
  触发条件严格（该 thread 恰好 1 个 run、会话恰好 1 条 user 消息、agent 已空闲），
  用与"重命名对话框"同一个**无工具**生成器，CAS 保证只生成一次；生成与保存都**不**追加消息、**不**压缩上下文。
  开关是桌面设置 `autoTitleFirstTurn`（手机端只是远程改这个**桌面**设置）。
- **`future run` 没有**：CLI 路径从不调用标题生成，唯一入口是手动的 `future session title <id> [--apply]`。
- **`future goal` 没有**：会话标题是显式传的（objective 截断）。（`future loop` 这个组已不存在，改名为 `future goal`。）
- 桌面端**不会**给它只是"导入"的会话生成标题（import 不经过触发点）。

### B11 `session list` 可能只回一部分会话，而扫描器"看不出来"
- **表现**：一次 `session list failed rc=1: Timeout expired` 之后，总数从两百多稳定降到几十，
  且此后每轮都报 `ok=true`。直接跑 `future session list --json` 也回同样少，且不是 cwd 过滤。
- **危害**：不是误报，是**静默漏报**——扫描器只是少看一批，`ok` 仍为 true。
- **对策**：`degraded` 标记——会话数比**历史峰值**少很多时记警告、写进 `state/health.json`
  （`state_snapshot.py` 单列一行），并在该轮**跳过 state 裁剪**，避免"列表缺失"被当成
  "会话消失→重新出现=新会话"。
  **只判"骤降的那一轮"不够**：事故被发现时往往已是"几十比几十"。所以 `state.json` 记历史峰值
  `peakSessions`，只要仍低于峰值的 60% 就一直保持 `degraded`（纯函数 `scan.peak_degraded()`），
  恢复或 `python3 bin/scan.py --adopt-peak`（"这些会话确实是我删的"）才清除。
  判定逻辑是纯函数 `scan.list_shrank()`，单测 `bin/selftest_scan.py`（`selfcheck` 的一环）。
- **仍未做**：为什么 agent 会少回；出现 `degraded` 时先跑
  `future session list --json | jq '.sessions|length'` 与 `ls -la ~/.future/agent/agent.db*` 对照。

### B13 会话数长期低于峰值**不等于** agent 出错——用户会自己在桌面端删会话
- **第一假设是"用户自己删的"**，不是最后一个假设。
- **判据**：三表（`sessions`/`entries`/`runs`）都没有行、且没有孤儿行 ⇒ 更像**应用层删除**；
  真正可疑的是"主行没了但 `entries`/`runs` 还有孤儿行"或"数量抖动/时有时无"。
  桌面端删除走应用库的 outbox 表，处理完即清空，所以事后"什么都没有"是正常的。
- **处置**：确认是用户删的，就 `python3 bin/scan.py --adopt-peak` 更新基线，否则 `DEGRADED`
  会永久刷屏、把真正的事故淹掉。
- **派工单前必须至少有一步能区分"用户删除"与"程序误删"的证据**，否则不要动用"停手/不要重启"这类重话——
  误报的代价（用户额度 + 我的可信度）比漏报更值得防。

