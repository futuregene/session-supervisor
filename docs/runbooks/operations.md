# 运维 runbook

> **何时读**：设施疑似不工作、要核查状态、或做只读巡检时

一切命令以工作区为根：`cd <本仓>`（本仓库 = 总管会话 cwd）。

`launchctl` 只适用于 macOS；**换平台怎么装守护**看
[supervisor-daemon.md](supervisor-daemon.md)。下面的 launchctl 命令都用 `$LABEL`，
它的值取自你安装的 plist（本机的那一个），所以这些命令不绑定某台机器的用户名：

```bash
LABEL=$(basename "$HOME/Library/LaunchAgents/"*.session-watch.plist .plist)
```

## 自检（怀疑设施没在跑时先做这个）

```bash
cat state/health.json                                          # ts 应在 5 分钟内；ok 应为 true
launchctl print gui/$(id -u)/$LABEL | grep 'state ='   # 应为 running
cat state/watchd.pid; ps -p "$(cat state/watchd.pid)"           # watchd 进程存活
tail -5 logs/scan.log logs/decide.log                           # 最近每轮扫描/唤醒决策
python3 bin/lock.py status                                      # free / 被某 pid 持有
```

判定：`health.json` 的 `ts` 落后 >5 分钟，或 `python3 bin/liveness.py` 报 FAIL ⇒ 设施停了，
按下面恢复。**存活判据一律用 `liveness.py`**（平台无关），`launchctl` 那行只是 macOS 上的旁证。

## 恢复 / 控制

```bash
launchctl kickstart -k gui/$(id -u)/$LABEL   # 重启守护（保留状态）
launchctl bootout gui/$(id -u)/$LABEL        # 停
bash bin/wake.sh heartbeat                                      # 手动唤醒一次（排队，若忙会等）
touch state/PAUSE                                               # 熔断：不唤醒、不自动批准
rm state/PAUSE                                                  # 解除熔断
```

## 只读巡检（代答前必做）

```bash
python3 bin/inbox.py list --brief --limit 20          # 待处理事件，brief 省上下文
python3 bin/inbox.py list --json --limit 5            # 只为马上要处理的事件取全文
python3 bin/approvals.py list                         # 待批准权限（auto 已跑过）
python3 bin/guard.py show                             # 已经发过什么、还剩多少额度
python3 bin/guard.py check <id>                       # 这个会话我发过几条（4h/24h，回读 transcript）
future session info <id> --json                       # 单个会话：cwd/模型/统计（**发消息前必读 cwd**）
future session transcript --session <id> --runs       # 每个 run 的状态/时长/错误（判断最后真实活动）
```

## 诊断某个会话

```bash
future session list --json | python3 -c 'import json,sys;[print(s["id"],s.get("isStreaming"),s.get("cwd")) for s in json.load(sys.stdin)["sessions"] if "关键词" in (s.get("sessionName") or "")]'
future session transcript --session <id> --limit 3 --truncate 800     # 末几条记录
future session transcript --session <id> --select user --limit 5      # 只看用户/总管发进去的话
future session transcript --session <id> --grep "gh pr merge" --select tool-call
```

注意 `future session status <id>` 是位置参数（没有 `--session`）；记录里的时间戳是 UTC，
本机时区为 +0800，`date -r <epoch>` 与 `python3 -c` 换算后再比对。

## 常见故障 → 动作

| 症状 | 先看 | 动作 |
|---|---|---|
| 一醒来几十上百条事件 | `state/health.json` 的 `bulk`/`resync` 标志、事件的 `updatedAtMs` 是否同分钟 | 判为批量刷新；`python3 bin/scan.py --baseline` 重打基线（可连跑数轮清 deferred） |
| `decide.log` 记 `agent unreachable` | 桌面端 / agent 在跑吗 | 不唤醒是预期行为；扫描继续，恢复后自动接上 |
| 唤醒迟迟不来 | `state/wakes.jsonl`、`guard.py show` | 检查是否刚唤醒过（120s 间隔）、是否触到小时上限、总管会话是否 streaming |
| 事件积压不清 | `inbox.py count` | 逐个处理或用 `--brief` 批量 triage；确认处理的才 `ack`，其余留 pending |
| 同一会话反复被唤醒 | `state/state.json` 里该会话的 `emitted` | 去重键靠 ordinal；若会话被外部持续改写记录，考虑加闸门（改动属 §B 需请示） |

## 记录与提交（每回合收尾）

**本仓有远端、默认分支受保护**——收尾提交的目标不是"当前分支"，流程见
[git-flow.md](../git-flow.md)。收尾只剩三件事：

```bash
python3 bin/inbox.py ack <event-id> ...        # 只 ack 真正处理过的
python3 bin/lock.py release
git rev-parse --abbrev-ref HEAD                # 在 main 上就不要 commit
```

- **在 `main` 上不要 commit**（并发回合共用同一个主 checkout，`git rev-parse` 先看清楚）。
  要记的东西（`journal/`、`reports/`、`docs/`）放进 `.worktrees/<name>` 的新分支，走 PR 并入 `main`。
- **`state/*.json`、`inbox/*.jsonl` 不提交**：扫描器反复改写它们，commit 完就过期，
  而且它们会让"工作区干净"这条判断永远为假（见 [privacy-and-state.md](../privacy-and-state.md) §3）。
- `git add -A` **不要再用**——它会把别人的在途改动和运行态文件一起卷进来；按路径 `git add`。
- 多行或含引号的提交信息用 `-F <文件>`，不要用 `-m "..."`（见 `gotcha-tools.md` D3）。

### 本地与远端不一致（`git status` 显示 behind / `merge --ff-only` 失败）

运行态文件的本地差异会**挡住 fast-forward**：`git merge --ff-only origin/main` 会打印
`Updating … Fast-forward` 后立刻 `Aborting`，而 `merge` 实际返回 1、HEAD 没动。
正确做法与判据：

```bash
git checkout -- state/ inbox/            # 丢掉本机运行态差异（扫描器几秒内重生）
git merge --ff-only origin/main          # 或先 git stash -u，merge 后 stash pop
git rev-list --left-right --count origin/main...main   # 必须是 0 0，才算真的同步了
```

**分支真的分叉时（不是运行态差异）**：先 `git log --oneline origin/main..main` 看清多的是什么，
**不要直接 reset**——那可能是别的回合的有效工作。按 [git-flow.md](../git-flow.md) §4 把它转成 PR 送进主干，
或逐项核对内容已在远端后再对齐（来由见 `.future/memory/incidents/`）。

## 存活判据：不要用 `launchctl print`

**何时读**：怀疑设施没在跑、或要写/改"自检存活"的检查时。

- **用** `python3 bin/liveness.py`（`--ensure` 会直接把 `bin/watchd.sh` 拉起来）。
  它只用设施自己产出的证据：`state/health.json` 新鲜（默认 180s）且 `ok=true` + `state/watchd.pid` 的进程活着。
- **不要用** `launchctl print gui/$(id -u)/$LABEL`：在总管自己的上下文里它回
  `125: Domain does not support specified action`，**输出永远一样**，因此**报不出 watcher 已死**。
  同日 `launchctl list`（97 个服务）里也看不到该 label，`launchctl load -w` 无可见效果。
  ⇒ 该 launchd 任务的监督状态**无法从总管上下文证实或证伪**；需要真人在他自己的终端里确认。
- 教训：**一个不会失败的检查不是检查**——设施曾静默停摆数小时（`scan.log` 一行都没有），
  而那套"自检存活"当时给不出任何信号。所以存活判据必须来自设施自己产出的、会过期的证据。
