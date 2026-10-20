# INSTALL — 把这套「会话总管」装到一台机器上

> **给 agent 读**：用户会让你读这份文件，然后**你来装**。按顺序做完，每一步都有可验证的结果；
> 卡住时**停下来说清卡在哪一条**，不要绕过检查继续。凡标了 **[问用户]** 的地方必须问，
> 其余自己做。

装完的东西是什么：一个 7×24 的守护进程（`bin/watchd.sh`），它每 30 秒扫描用户**全部**
FutureOS 会话，把"有人停下等回复 / 运行失败 / 有新会话"变成事件；满足条件时唤醒一个
**总管会话**（就是一个普通 FutureOS 会话），让它在那一回合里处理这些事件。
扫描与判断不烧 token，只有被唤醒的那一回合烧。

---

## 0. 前置条件（先核，缺了就停）

```bash
future --version          # 需要支持 session reset / set --append-system-prompt
python3 --version         # 3.9+
git --version
gh --version              # 可选：只有要走 PR 流程时才需要
```

**必须能调用一次模型**（装的过程中 `apply_prompt.py` 会发一条极小的 canary 请求）。
`future models --json` 有输出即可。

平台：macOS / Linux 原生；Windows **需要 Git Bash 或 WSL**（`bin/watchd.sh` 是 bash 脚本）。
详见 [docs/runbooks/supervisor-daemon.md](docs/runbooks/supervisor-daemon.md)。

## 1. 把仓库放到本地磁盘

```bash
git clone <本仓远端> <WATCH>        # 或直接复制一份目录；下文的 <WATCH> 指它的绝对路径
cd <WATCH>
```

**不要**放在网络盘、同步盘（Dropbox/iCloud）或 WSL 的 `/mnt/c` 上——扫描器每 30 秒写一次
`state/`，这些位置会造成文件锁与 mtime 抖动。

跑一次自检，确认基础没坏：

```bash
python3 bin/selfcheck.py            # 期望 checks=N failed=0
```

## 2. 配置 `bin/config.json`

只改这几项，其余保持默认（每项的含义见 [docs/throttle-and-budget.md](docs/throttle-and-budget.md)）：

| 键 | 装的时候怎么定 |
|---|---|
| `future_bin` | 一般写 `"future"`（按 PATH 找）；PATH 里没有就写绝对路径 |
| `scan_interval_seconds` | 默认 30，`bin/watchd.sh` 启动时读它。别低于 15——每轮都要跑一次 `future session list` |
| `exclude_cwd_prefix` | 已含 `${WATCH}`；**把用户不想被巡检的目录前缀加进来**（例如某个实验仓） |
| `exclude_sessions` | 明确的会话 id 列表，默认空 |
| `max_wakes_per_hour` / `max_wakes_per_day` | 默认 12 / 400。**[问用户]** 他愿不愿意为此花额度 |
| `auto_approve` | 自动批准权限请求的开关。**[问用户]** 是否照开（默认 `true`，带拒绝清单） |
| `monitor_session_name` | 总管会话的名字前缀，默认 `会话总管`。改名要三处一致（见 §3） |

`${WATCH}` 是占位符，`bin/` 里的脚本会替换成仓库根；**不要把绝对路径写进仓库文件**。

## 3. 建总管会话，并登记它的 id

```bash
future session new --name "会话总管" --cwd <WATCH> --json   # 从输出里取 sessionId
echo "<SESSION_ID>" > <WATCH>/state/monitor_session.txt
python3 bin/apply_prompt.py --check      # 这一步就会核对 id 文件与实况是否一致
```

`future session new` 也可以带 `--append-system-prompt`，但**不要手写**——第 4 步的
`apply_prompt.py` 会做 canary 验证并记录哈希，手写的内容不会进那份记录。

- 名字**必须以 `config.monitor_session_name` 开头**：`apply_prompt.py` 靠这个前缀从会话列表里
  认人，对不上就**拒绝执行**（这是防止把手册装到已归档会话上的保护，别绕过）。
- `state/monitor_session.txt` 记的是"当前总管会话"，轮换时会自动改写；它**不进 git**。
- **[问用户]** 这个会话用什么模型。它是长跑会话，模型决定每次唤醒的成本；
  建议显式指定，别留空（留空 = 静默取当时的全局默认）。

## 4. 装"每轮不变的手册"

```bash
python3 bin/apply_prompt.py --apply     # 先发 canary 验证，通过才写入
python3 bin/apply_prompt.py --check     # 期望 applied=True fresh=True mode=minimal
```

这一步把 `prompts/append.md` 写进总管会话的 **system prompt**：唤醒卡里的用户消息会一直留在
上下文里（对话历史只增不减），system prompt 每轮重建、不累积。所以这一步直接决定长期成本。

- canary 失败 = 当前 `future` 二进制不支持 append，**不要用 `--skip-canary` 糊过去**；
  这种情况下系统会退化为"每轮把整本手册塞进用户消息"（`mode=full`），能跑但更贵。
- 以后**改了 `prompts/append.md` 必须重新 apply**；`bin/wake.sh` 每轮会调
  `bin/ensure_append.py` 自动兜底，但不要依赖它。

## 5. 建基线，避免历史事件洪水

```bash
python3 bin/scan.py --baseline
python3 bin/scan.py --baseline          # 连跑到 logs/scan.log 里 "deferring N sessions" 归零
python3 bin/inbox.py count
```

第一次扫描会把最近 `lookback_hours`（默认 72 小时）内所有"变化过"的会话当成事件。
基线模式只记"见过"，不产出事件。

## 6. 装守护进程（三个平台）

照 [docs/runbooks/supervisor-daemon.md](docs/runbooks/supervisor-daemon.md) 做：

| 平台 | 一句话 |
|---|---|
| macOS | `~/Library/LaunchAgents/<label>.plist` → `launchctl bootstrap gui/$(id -u) …` |
| Linux | `~/.config/systemd/user/session-watch.service` → `loginctl enable-linger "$USER"` + `systemctl --user enable --now …` |
| Windows | 任务计划程序（登录时触发）→ 动作必须是 `bash.exe <WATCH>/bin/watchd.sh` |

调度器**只负责让 `bin/watchd.sh` 一直活着**，不含任何业务逻辑。

## 7. 验证（缺一条都算没装好）

```bash
python3 bin/liveness.py                 # ok watcher / ok scanner；退出码 0
python3 bin/selfcheck.py                # failed=0
bash bin/wake.sh heartbeat              # 手动唤醒一次，看 logs/wake-*.log 有没有跑完
python3 bin/inbox.py list --brief --limit 5
```

四条全绿之后，再确认一次"空闲时不会自己唤醒"：`logs/wake.log` 在没有事件时不应每 5 分钟长一条
（`skip_idle_heartbeat` 开启时，心跳只在"有新事件"或长静默兜底时才唤醒）。

`bash bin/wake.sh heartbeat` 成功的样子：`logs/wake-<ts>-heartbeat.log` 末尾有 `=== exit=0 ===`，
并且总管会话里多了一条"一次唤醒回合"的卡片。失败最常见的原因是**总管会话 id 写错**或
**模型调不通**——日志里会直说。

## 8. 种记忆与隐私（这是"装完能用"和"装完乱来"的分界）

三样东西决定它像不像一个了解这台机器的助手：

1. **`FUTURE.md`**（会话每轮都会读的索引）。照文件里的格式写 3–8 行：这台机器叫什么、
   仓库在哪、用户怎么称呼自己、有哪些长期偏好。**[问用户]** 先问，别自己编。
2. **`.future/memory/instance.md`**：本机事实——`future` 二进制路径与版本、总管会话 id、
   定时任务 id、远端仓库、守护进程的 label / unit 名。这些只在这台机器上有意义，**不进 git**。
3. **`.private/profile/`**：关于用户的稳定事实（沟通风格、口味、禁区）。**[问用户]** 问他愿意
   记什么；**永不记录凭据**，永不把凭据写进任何文件。

同时**审一遍 `POLICY.md`**：它是这台机器的授权来源，里面的 §B 白名单决定了它会不会自己
对外发消息、发版、删数据。**[问用户]** 逐条确认；他不表态就按文件里的默认执行，并在
`reports/needs-you.md` 里记一条"授权口径待确认"。

## 9. 可选：每日简报任务

`prompts/daily.md` 是给定时任务用的提示词（存活自检 + 24 小时汇总 + 待决定事置顶）。

```bash
future models --json | head          # 先确认要用的模型 id 存在
future task add --name "会话总管·每日巡检简报" --daily --time 08:30 \
  --cwd <WATCH> --prompt-file prompts/daily.md \
  --model <provider>/<model> --thinking <level>
future task show <TASK_ID> --json    # 必须回读：modelId / thinkingLevel 不能为空
```

`--model` 与 `--thinking` **必须显式给**：不给会静默存空值（运行时取当时的全局默认、思考预算 0），
命令仍然打印成功。改触发时间/周期会改变用户看到的节奏，改之前记 `reports/decisions/`。

## 10. 收尾：第一次真跑

```bash
touch state/PAUSE      # 先暂停，确认配置无误
rm state/PAUSE         # 放开
```

然后**观察一到两个唤醒周期**：`logs/scan.log` 有每轮汇总、`state/health.json` 的 `ts` 在 5 分钟内、
空闲时不会唤醒（`skip_idle_heartbeat`）。真有事件时，总管会写 `journal/<日期>.md`。

告诉用户三件事就装完了：

- 总管会话是哪一个（名字 + id），可以直接跟他对话，也可以让它自己巡检；
- 需要他拍板的事会出现在 `reports/needs-you.md`；
- `touch state/PAUSE` 是一键停。

---

## 装的时候不要做的事

- **不要读、不要复制任何凭据**（钥匙串、`auth.json`、token）。装这套东西不需要它们。
- **不要把 `.private/` 下的东西提交**，不要为了"让检查通过"去改 `.gitignore` 的忽略规则。
- **不要为了跑通而放宽权限**：不要给仓库加管理员绕过、不要 `--skip-canary`、
  不要把 `AGENTS.md` / `POLICY.md` 改成"什么都能做"。
- **不要在没有问过用户的情况下**：装/升级二进制、注册开机自启以外的系统服务、发版、打 tag、
  对外发送任何消息、删除已有数据。
- **不要顺手提交 `journal/` `reports/` `state/` `inbox/` `logs/`**：它们按设计就在本地磁盘，
  `bin/selfcheck.py` 会拦；`git add -A` 之前先看 `git status`。

## 装完之后按什么维护

| 事情 | 谁管 |
|---|---|
| `logs/` 体积 | `bin/prune.py`（`wake.sh` 每轮调）：按天日志留 7 天，追加型日志超 1 MiB 就地轮转 |
| 总管会话上下文 | `bin/monitor_roll.py`：超 `monitor_roll_context_percent` 就**原地重置历史**，不换会话 |
| 手册与文件的漂移 | 改 `prompts/` 后必须 `apply_prompt.py --apply`；改 `prompts/daily.md` 后必须 `task edit --prompt-file` |
| 设施是否活着 | `python3 bin/liveness.py`（`--ensure` 会自己拉起来） |
| 已知限制与未验证项 | [docs/runbooks/limits.md](docs/runbooks/limits.md) |
