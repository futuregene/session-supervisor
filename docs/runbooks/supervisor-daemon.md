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
  "触发器"选"登录时"，"设置"里勾"如果任务失败，按以下频率重新启动"（10 分钟）。
- **WSL**：在 WSL 里照上面 Linux 那套装 systemd 用户服务，仓库放在 WSL 的文件系统里
  （放在 `/mnt/c` 上会和 Windows 侧的进程抢同一批文件锁）。
- 任务计划程序没有 launchd 的 `KeepAlive`，"重启频率"是最近似的东西；
  `liveness.py --ensure` 是兜底（唤醒回合发现它死了会拉起来）。

**已知限制**：`future` CLI 在 Windows 上的行为没有在这台机器上验证过；这条路径是
"照机制推断 + 已修掉两处会咬人的跨平台缺陷"，不是"实测通过"。

## 在这个设施里查存活

```bash
python3 bin/liveness.py            # ok/FAIL 两行，退出码 0/1
python3 bin/liveness.py --ensure   # 没跑就按人手方式启动，再复查
python3 bin/liveness.py --json     # 给脚本用
```

**不要**把 `launchctl print gui/$(id -u)/<label>` 当作判据：在总管自己的上下文里它只回
`Could not print domain`（见 [operations.md](operations.md) 与 `bin/selftest_liveness.py`
的开头）。一个跑不出结果的检查不算检查。
