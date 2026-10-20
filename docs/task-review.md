# docs/task-review.md — 定时任务的运行与复盘

> **何时读**：收到 `task_review` / `task_run_failed` / `task_run_empty` 事件时；或想改某个定时任务的提示词、怀疑它白跑；或要新增定时任务并决定"跑完留不留会话"时。

## 1. 为什么需要这条通道

定时任务若是 `session retention = delete`，**跑完就把会话删了**。扫描器读的是
`future session list`，看不见它；它也不产生任何事件，于是：

- 结果其实**没丢**——台账（`task_runs`：状态、错误、提示词版本、这一轮的答复）还在；
- 但**没人会去看**。这就是"反思要靠记性"的那一半缺口。

`bin/taskruns.py` 把台账变成事件，交给已有的唤醒路径；所以复盘是**被触发的**，不是被想起的。
实测本部署：多数任务删会话，而台账里 `feedback` 一栏**从未被填过**——不要假设有人验收过任何一轮。

## 2. 事件与处置

| kind | 优先级 | 含义 | 处置 |
|---|---|---|---|
| `task_run_failed` | high | 新一轮运行非成功结束 | 读错误；能靠改提示词/参数避免就改，属平台侧（网关 5xx、磁盘）则只记录 |
| `task_run_empty` | medium | 状态 `completed` 但输出为空 | 静默空跑，最值得查：通常是指令让它"无需动作就不输出" |
| `task_review` | medium | 距上次复盘已积累 N 轮（默认 6；或连续两次失败） | 见 §4，做完 `taskruns.py reviewed <id>` |

三条都**按 run id 去重**，同一轮不会被报两次；任务第一次被看见时是**基线**
（历史全部标记为已见、不计数），避免上线瞬间对着一堆旧运行喊复盘。

`cancelled` 不算事件（用户自己停的，与 `scan.py` 对会话的处理一致）。

## 3. 命令

```bash
python3 bin/taskruns.py list                 # 每个任务：未复盘轮数 / 连续失败 / 是否待复盘
python3 bin/taskruns.py collect --force      # 立刻轮询一次（默认 120s 节流，scan.py 每次调用）
python3 bin/taskruns.py digest <任务id|名称>  # 写一份复盘摘录，打印路径
python3 bin/taskruns.py reviewed <任务id>     # 关掉这一轮：计数归零、reviewPending 清除
```

`digest` 落在 `state/task-digests/<task-id>.md`，只读、覆盖同一文件。它替你做的是**机械筛选**：

- 运行表（状态 / 提示词版本 / 用时 / 输出字数）与异常块；
- **每轮都出现的行**（在多数轮次里逐字重复 ⇒ 要么该写进提示词，要么提示词在让它反复推导）；
- **反复自报的不确定 / 待办**、**反复出现的跳过与风险**（⇒ 交替失败，或规则该改）；
- 各轮自述的截断摘录 + 三个必须回答的问题。

先读 digest，再决定要不要展开某一轮：`future task output <run-id> --tail 40`。

## 4. 复盘怎么做（一次只改一处）

1. **判断有没有白跑**：空输出、失败、或结论与上一轮逐字相同。
2. **按证据归类**：重复行 → 提示词；反复不确定 → 缺步骤或需要工具（派工单）；平台 5xx / 磁盘 → 记录即可，不改提示词。
3. **改提示词时要**：
   - **先取库里当前版本**：`future task show <id> --json | jq -r .prompt > /tmp/task.md`
     （本地副本一定过期；`task edit --prompt-file` 是整份覆盖、没有合并）；
   - 改完 `future task edit <id> --prompt-file /tmp/task.md`，并用
     `future task show <id> --json | grep -c '<关键规则特征串>'` 确认没把既有规则挤掉；
   - **改触发时间/周期会改变用户看到的节奏**，先记 `reports/decisions/` 再动。
4. **留痕**：`reports/decisions/<日期>.md` 一条（做了什么 / 依据 / 回退 =
   `future task prompt revert <id>`），有必要的用 `future task feedback <run-id> good|bad --note "…"`
   给这一轮打分——台账里那一列空着，复盘就没有历史可用。
5. **收尾**：`python3 bin/taskruns.py reviewed <id>`。不做这步，下一轮不会重新提醒（这是有意的：
   提醒本身要花一次唤醒）。

## 5. 参数（`bin/config.json`）

| 键 | 默认 | 作用 |
|---|---|---|
| `task_watch_enabled` | true | 总开关；关掉就不再轮询台账 |
| `task_watch` | `[]` | 只盯列出的任务 id；空 = 所有启用的任务（新增任务不必改配置） |
| `task_poll_seconds` | 120 | 轮询节流；扫描每 30s 一次，但台账不必那么勤 |
| `task_review_every_runs` | 6 | 攒够几轮要求复盘（每小时任务 ≈ 6 小时一次） |
| `task_review_max_hours` | 24 | 兜底：有未复盘轮次时，最久多久必须复盘一次 |
| `task_run_seen_cap` | 40 | 每个任务记住多少个已见 run id（去重窗口） |

## 6. 拿不到的东西（别假装有）

- **花费**：`task_runs` 没有 token 列，所以"这次跑了多少钱"查不到；只能按运行时长与提示词长度粗估。
- **过程**：会话已删 ⇒ 工具调用序列、被拒权限、试错轮次都不可得，只有它在答复里的自述。
  ⇒ 想让某任务的过程可复盘，就把它的 `sessionRetention` 改成 `keep`（代价是每轮留一个会话）。
