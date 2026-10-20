# 让守护跑起来（换平台时读这个）

> **何时读**：在新机器上部署、或把设施迁到 Linux / Windows 时

（**第一次安装**从 [../../INSTALL.md](../../INSTALL.md) 开始；这份只管"谁看着 watchd 进程"。）

**调度器只有一个职责：让 `bin/watchd.sh` 一直活着。** 扫描（`scan.py`）、自动批准
（`approvals.py`）、唤醒判断（`decide.py`）都在脚本里，调度器不参与任何逻辑。
所以换平台只是换"谁看着这个进程"，`bin/` 与 `bin/config.json` 不用改。

存活判据与平台无关，用 `python3 bin/liveness.py`（两条证据：`state/watchd.pid` 的进程
活着 + `state/health.json` 新鲜）；`--ensure` 会在它没跑时按人手动的方式启动一次。

## 平台对照

| 平台 | 载体 | 关键设置 |
|---|---|---|
| macOS | launchd · 用户级 LaunchAgent | `RunAtLoad` + `KeepAlive`，日志重定向到 `logs/` |
| Linux | systemd · `--user` unit | `Restart=always`、`WantedBy=default.target`，另需 `loginctl enable-linger` 让用户会话退出后仍跑 |
| Windows | 任务计划程序（登录时触发）或 WSL 内的 systemd | 动作必须是 `bash.exe <本仓>/bin/watchd.sh`，**需要 Git Bash 或 WSL**（见下） |
| 任意 | 无调度器 | `nohup bash bin/watchd.sh &`；机器重启后要手动再来一次 |

### macOS（launchd）

```bash
# ~/Library/LaunchAgents/<本机 label>.plist
#   ProgramArguments = [/bin/bash, <本仓>/bin/watchd.sh]
#   RunAtLoad = true, KeepAlive = true
#   WorkingDirectory = <本仓>
#   StandardOutPath / StandardErrorPath = <本仓>/logs/launchd.out.log / .err.log
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
launchctl kickstart -k gui/$(id -u)/<label>     # 重启（保留状态）
launchctl bootout gui/$(id -u)/<label>          # 停
```

label 是**本机事实**，不写进仓库；runbook 里一律用
`LABEL=$(basename "$HOME/Library/LaunchAgents/"*.session-watch.plist .plist)` 现算。

### Linux（systemd --user）

```ini
# ~/.config/systemd/user/session-watch.service
[Unit]
Description=FutureOS session supervisor
[Service]
Type=simple
WorkingDirectory=%h/session-supervisor
ExecStart=/bin/bash %h/session-supervisor/bin/watchd.sh
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
```

```bash
loginctl enable-linger "$USER"        # 不 enable 的话注销后会被停掉
systemctl --user daemon-reload
systemctl --user enable --now session-watch.service
journalctl --user -u session-watch -n 50
```

### Windows

`bin/watchd.sh` 是 bash 脚本（用到 `command -v`、`kill -0`、`mkdir -p`、`date`），
所以 Windows 上**必须有 bash**：

- **Git Bash**：装 Git for Windows 后 `bash.exe` 在 PATH 上。任务计划程序里
  动作 = `C:\Program Files\Git\bin\bash.exe`，参数 = `<本仓>\bin\watchd.sh`，
  "触发器"选"登录时"，**并且给它加一个 5 分钟重复间隔**（见下），"设置"里再加
  "如果任务失败，按以下频率重新启动"（10 分钟）。
- **WSL**：在 WSL 里照上面 Linux 那套装 systemd 用户服务，仓库放在 WSL 的文件系统里
  （放在 `/mnt/c` 上会和 Windows 侧的进程抢同一批文件锁）。
- **"重启频率"不够——必须有重复触发器**：`RestartOnFailure` 只在任务**以失败退出**时生效，
  而守护进程最常见的死法是**没有报错的死**。实测：任务 `LastTaskResult`
  = `3221225786`（`0xC000013A`，`STATUS_CONTROL_C_EXIT`，即启动它的那个控制台被关掉），
  任务停在 `Ready`、`NextRunTime` 空，**十几分钟没人发现**——因为本会报告"它死了"的那个进程
  正是它自己。药方是让**调度器**来判活：给登录触发器加
  `<Repetition><Interval>PT5M</Interval></Repetition>` + `MultipleInstancesPolicy=IgnoreNew`，
  于是每 5 分钟调度器就试启动一次，活着的那次被 `IgnoreNew` 直接跳过（`watchd.sh` 自己的
  pid 判重是第二道闸）。`watchd.log` 里会因此每 5 分钟多一行 `start`，那是正常的。
  ```powershell
  $t = Get-ScheduledTask -TaskName '<任务名>'
  $t.Triggers[0].Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
  $t.Settings.MultipleInstances = 'IgnoreNew'
  Set-ScheduledTask -TaskName '<任务名>' -Trigger $t.Triggers -Settings $t.Settings
  ```
- **`liveness.py --ensure` 拉起来的那个必须与调用者的控制台脱钩**：`start_new_session=True` 是
  POSIX 专用、在 Windows 上被静默忽略，于是子进程继承了调用者的控制台，控制台一关
  （agent 回合结束、终端窗口关闭）整棵进程树收到 `CTRL_CLOSE_EVENT` 就没了。
  代码里已用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`（Windows）修掉，
  见 [../gotcha-tools.md](../gotcha-tools.md) D29。

**Windows 上必须先核对的三件事**（第一次装时逐条踩过，细节见
[../gotcha-tools.md](../gotcha-tools.md) D27）：

1. `bin/*.sh` 必须是 **LF**（仓里的 `.gitattributes` 负责；老 checkout 要 renormalize），
   否则 Git Bash 对每一行都回 `$'\r': command not found`。
2. `bash.exe` 与 `future.exe` 都要能被找到：`python3 bin/toolpath.py` 应当打印出三个真实路径，
   `bash` 那行不是 `None`。
3. `state/watchd.pid` 里必须是 **Windows 原生 pid**：Git Bash 的 `$$` 是 MSYS  pid，
   Windows 侧查不到，`liveness.py` 会把活着的守护进程报成"未运行"。
   `bin/watchd.sh` 用 `/proc/$$/winpid` 写原生 pid；看到 `pid=XXXX` 与
   `Get-CimInstance Win32_Process -Filter "Name='bash.exe'"` 的 `ProcessId` 对得上才算对。

## 在这个设施里查存活

```bash
python3 bin/liveness.py            # ok/FAIL 两行，退出码 0/1
python3 bin/liveness.py --ensure   # 没跑就按人手方式启动，再复查
python3 bin/liveness.py --json     # 给脚本用
```

**不要**把 `launchctl print gui/$(id -u)/<label>` 当作判据：在总管自己的上下文里它只回
`Could not print domain`（见 [operations.md](operations.md) 与 `bin/selftest_liveness.py`
的开头）。一个跑不出结果的检查不算检查。
