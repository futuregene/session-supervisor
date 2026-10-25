# 坑 · G · goal 与 worker

> **何时读**：处理 `future goal` / worker / 票与租约之前

（分类 G；来由与真实经过见 `.future/memory/`，本文件只留机制与判据。
原来这些条目排在 `gotcha-tools.md` 的 D 系列里，按"何时读"拆出来后编号改为 G——工具类的坑仍在那边。）

### G1 租约不带 pid/心跳 —— "它在等一个租约"时必须自己核持有者是否活着
- **为什么 CLI 帮不了你**：`future goal lease status … --json` 只有
  `{todo_id, lease, owner, expires_at}`，**没有 pid、没有心跳、没有 last_seen**；
  单看租约**无法**区分"正干得欢"与"早死了"。
- **正解（两步，都便宜）**：
  1. owner → session 的映射是持久的：goal 状态目录里 `goals/<goal>/events.jsonl` 记着
     `worker_session_bound`（`agent_id` → `session_id`）；按子串先过滤再解析，读一个 goal 一毫秒。
  2. 拿那个 session 看 `isStreaming` 与最后一条记录：`future session status <id>`、`python3 bin/peek.py <id>`。
     **streaming=true 就是活着的**，别再去数进程。
- **别做的事**：不要因为"父会话在等"就去 `lease release` 或催父会话——先核持有者；活着就什么都不用发。
- **扫描器救不了你**：goal worker 会话被整类跳过（`gotcha-send.md` A15），worker 静默死亡**不会**产生任何事件。

### G2 worker **不继承**父会话的模型与思考等级
- **代码事实**：worker 的模型来自**注册表默认**（不继承），思考等级是写死的 `DEFAULT_THINKING_LEVEL="medium"`；
  编排层只在显式传 flag 时覆盖。
- **危害形态：静默**——不报错、不警告，worker 只是用了一个你没打算的模型与小思考预算。
- **规矩**：凡派 worker / 建定时任务，**显式写 `--model` 与 `--thinking`，并用 `show --json` 回读核对**。
  不要相信"默认沿用"这类措辞（[gotcha-tools.md](gotcha-tools.md) D12 是同一个坑的另一个入口）。
- **不要"修实现去贴合文档"**：让 worker 继承父会话是**行为变更**，属产品决定；该改的是文档。

### G3 散文里的"后继任务"不建票；点名的号可能已被另一张票占用
- **机制**：控制平面**不会**从交付文本里自动建票；"我在散文里声明了后继"= 一句没有执行者的陈述。
- **验收规则**：看到"后继 <票号>"时，**必须把它与内核里真实存在的票对一遍**；号已被占用 → 以内核为准并改散文；
  号不存在 → 补建，或在记录里写明"这是建议不是票"。
- **为什么值得记**：这类错不会让任何检查变红，只在几天后暴露，那时上下文已经丢了。

### G4 读 `ACTIVE_GOAL_STATE.md` 时，`<!-- future-goal:todo … -->` 标记在**正文之后**
- **形态**：文件里每张票是「正文若干行 + 一行 HTML 注释标记」。所以 `grep -A1 "todo_id=X status=open"`
  拿到的"下一行"是**下一张票的正文**，不是 X 的。
- **后果**：按这种读法给会话派单会**把票的身份张冠李戴**（实测一批 7 张票里 5 张被认成了别的票，
  于是"只有一条真没做完"的结论完全错——真实是 5 条没做完）。
- **对策**：要身份就 `future goal status --goal <id>`（控制平面的权威视图）；
  要在文件里定位某张票，用 `grep -B<n>` 往**上**找正文，或直接读 `todo_id=` 那行里的
  `status=` / `resume_when=` / `evidence=`（标记自身带结论）。
- **附带**：`is_terminal()` 只认 `Done | Superseded`；`blocked` / `deferred` **不是**终态
  （`state.rs` 的 `is_terminal` 是 `matches!(status, Done | Superseded)` 的合取）。所以"标 blocked 就算收尾"
  在控制平面里不成立——要让目标真正 `terminal: true`，未做的票只能以 `superseded` 退役、
  并在理由里写明"NOT DONE"，不许标 done 充数。

### G5 goal 台账是**项目本地**的：删掉 worktree 等于删掉那批目标的全部历史

- **机制**：`future goal` 的台账（`registry.json`、`goals/<id>/`、`runs/`）写在**其项目根的 `.future/goal/`**，
  而 worktree 是一个独立根——所以在 `.worktrees/<任务>/` 里 `goal init` 的目标，台账只存在于那棵 worktree 里。
  `git worktree remove` 之后，`future goal status --goal <id>` 只会答 `goal <id> not found`，
  而且**没有任何命令能把它找回来**：分支没推远端时，连裁决文件与终报都一起消失。
- **症状形态**：一个跑了几天的长目标，某天问"这个目标花了多少、结论是什么"，
  台账已经不在了；能拿到的只有当时抄出来的副本（如果有）。
- **判据**：**报出"目标 X 的最终数字/结论"之前，先确认它的台账还活着**：
  ```bash
  future goal status --goal <id>        # not found ⇒ 你手上只有副本，要声明证据等级
  cat <项目根>/.future/goal/registry.json | grep <id>
  ```
  台账不在时，报告里必须写明"这些数字来自 <某次读数的副本>，现在无法复核"——**不要说成当前事实**。
- **防止**（对长目标）：在 worktree 里跑目标时，定期把台账与终报复制到项目根或仓库外的持久目录
  （`.future/` 在多数仓库里被 gitignore，但那不等于持久——它随 worktree 一起走）。
- **清理的前提**：`git worktree remove` 之前问一句"这棵树里有没有 goal 台账"。有台账就先备份；
  这跟"分支上有未推送提交不能删"是同一类判断。
- **反向症状**：项目根 `registry.json` 里可能还留着 `status: active`、但 `cwd` 指向已删除 worktree 的目标。
  这类残留不会自己消失，读 `registry.json` 时要按 `cwd` 是否存在过滤一遍，别当成"还在跑"。

### G6 写工具被 `RECOVERY_UNCERTAIN` 锁住时——先 `session reset` 丢工具卡，别再急着迁会话

> 更新：原先这一节写的是「重放和 clone 都救不了 → 只能开新会话」。
> **已找到原地解锁的办法**：`future session reset … --drop tool-calls,tool-results`。
> 本文件保留原结论作为“哪些路不通”，但**首选动作改成 reset**。

### 症状

某会话的 `shell` / `write` / `edit` 一律返回

```
[RECOVERY_UNCERTAIN] The previous operation's effect could not be verified: shell (call_…)
Work is preserved; no uncertain operation was repeated.
```

而 `read` 正常。**先别信会话自己的归因**：被锁的会话常把原因说成“host 刚刚又崩了”，
实测多次都不是——它撞上的是几小时前留下的标记（见下「先定性」）。

### 先定性，一眼就能看

```bash
python3 -c "
import sqlite3,datetime
c=sqlite3.connect('file:'+__import__('os').path.expanduser('~/.future/agent/agent.db')+'?mode=ro',uri=True)
sid='<session-id>'
for p,ts,cj in c.execute('select position,timestamp_ms,substr(content_json,1,160) from entries where session_id=? and entry_type=\"run_terminal\" order by position',(sid,)):
    print(p, datetime.datetime.fromtimestamp(ts/1000).strftime('%m-%d %H:%M:%S'), cj[:140])
"
```

找 `state=interrupted_by_restart` + `interruption_cause=outcome_unknown`：**宿主在工具调用在飞时崩了**，
那个调用没记下结果，于是写工具被整体闸住。

**顺带确认 host 现在到底好不好**（它常被冤枉）：

```bash
systemctl --user show future-host -p MainPID -p ActiveEnterTimestamp -p NRestarts
ss -xlp | grep agent.sock          # 持有进程应与 MainPID 一致
journalctl --user -u future-host --since "-2h" | grep -icE "failed|panic|deadline"
```

`MainPID` 与 socket 持有者一致、`NRestarts` 不涨、最近两小时 0 命中 → host 是好的，
锁是**历史遗留**，不是刚崩的。

### 首选：`future session reset --drop tool-calls,tool-results`

```bash
# 1) 先干跑看会删什么（--dry-run 是默认，不加 --yes 不落盘）
future session reset <session-id> --keep-last 200 --drop tool-calls,tool-results

# 2) 确认无误会删掉那个悬空调用后，再落盘
future session reset <session-id> --keep-last 200 --drop tool-calls,tool-results --yes
```

它做三件事，都是我们要的：

- **删掉「有调用、无结果」的悬空调用**（连同它对应的 tool-result —— 平台明确要求成对删，
  留孤儿 result 会让之后的每个请求都失败）；
- **会话记录不动**：id / 名字 / cwd / model / 两段 prompt / 权限 / 侧栏位置全部保留；
- **先写无损备份**并打印路径（`~/.future/agent/session-backups/<id>-<ts>.json`，
  完整 entries，不是摘要），删错了可回灌。

实测：一个锁了约 6 小时的 supervisor 会话，reset 之后
**下一条命令就正常执行**（clippy 全绿、后续 merge 干净）。此前同一类会话靠“迁新会话”处理过三次，
三种代价都比 reset 高：要写 handoff、要 `goal supervisor register` 换绑、还多一个要人维护的旧会话名。

**注意**：`--keep-last N` 里的 N 是**用户回合数**，不是条目数；`--keep-last 200` 的实际含义是
「所有用户回合都保留，只丢工具卡」。丢卡之后，会话的助手文字还在，但它**引用不到任何旧命令输出**——
所以给它的接续消息里必须把关键状态（分支、commit、远端、剩余缺口）重新写一遍。

### 那三条仍然不通（别再花时间）

| 尝试 | 为什么不行 |
|---|---|
| 按平台文案“逐字节重放挂起调用” | 闸门在恢复匹配**之前**就拦住，返回的仍是同一句通用 `RECOVERY_UNCERTAIN`，永远看不到 `RECORDED`/`ABORTED` 分支 |
| `future session clone` | 克隆复制 `outcome_unknown` 记录，**锁随历史一起走**，新会话照样被锁 |
| `future doctor` / `future data resume|rollback` | 与这个状态无关（后者只管 restore fence） |

### 什么时候仍然要迁会话

`reset` 只解决「悬空调用把闸门卡住」。以下情况照旧走「开新会话 + handoff + `goal supervisor register` 换绑」：

- reset 之后**仍然**返回 `RECOVERY_UNCERTAIN`（说明这次的原因不是悬空调用）；
- 那个会话的历史本身就**必须完整保留**（审计、举证），不能丢工具卡；
- 会话的 prompt / 权限 / 模型配置也要换（reset 明确不改会话记录）。

原会话**不要删**——它是证据。改名加 `⛔已锁·` 前缀，避免人再往里发任务。

### 是什么造成的

宿主 `phase=Failed("shutdown deadline exceeded")` 的下游后果。一次崩溃会**同时**锁住所有
当时有工具调用在飞的会话，所以常见“一批会话一起挂”。要确认崩溃时刻：

```bash
journalctl --user -u future-host --since today | grep -E "starting|deadline|Failed"
```

`ActiveEnterTimestamp` 与某条 run 的 `completed_at_ms` 高度接近，就是那次重启打断了它。
（反例：有个会话把自己刚发生的锁归因为“host 刚刚又崩了”，而 host 其实几小时前就没动过，
锁根是那次重启。**先看时间戳，再信会话的叙述**。）
