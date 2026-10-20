# docs/ — 按话题分的机制文档（给机器读）

> **何时读**：想知道"某个文档该不该读"时；或**新写文档之前**（先找已有话题，别新建）。

## 约定

1. **这里只放框架本身**：别人 clone 下来照此也能用的机制、流程、规则、坑。
   凡涉及本机路径、账号、具体会话/任务 id、用户原话、某日实测数字、某次事故的经过——
   一律写进 `.future/memory/`，由根目录 `FUTURE.md` 索引。**`docs/` 里出现实例内容就是错位。**
2. **一个话题一个文件**，目标 ≤6 KB。大了就再拆——颗粒度过粗会让 agent 费上下文、也不知道何时读。
3. **每个文件的第 3 行必须是 `> **何时读**：…`**，用"触发场景"写，不要写文件简介。
   这是索引里最有用的一列：agent 靠它决定**要不要打开**，而不是靠文件名猜。
4. 文件名说清**话题**（`gotcha-send.md`、`throttle-and-budget.md`），不要用序号当唯一信息。
5. 规则过期就地更正，不要靠追加"某日补充"来打补丁；来由写在 `.future/memory/incidents/`。
6. **能被工具拒绝的坑，不写进文档**。一条坑若已经有代码拦下（工具直接拒绝/纠正）或探针钉住
   （`bin/selfcheck.py` / `bin/selftest_*.py`），文档里只留**一行**说它存在与钉在哪，
   不要展开成因与对策——展开留着的是**代码管不了的部分**：外部系统的行为（GitHub、钥匙串、git 语义、
   已安装二进制的实现）、以及"看到这个症状时该怎么判断"。写完自问一句：
   **这条能不能变成一个会失败的检查？能，就去做检查，然后删掉这段。**

## 索引（按"什么时候需要它"排）

| 文件 | 何时读 |
|---|---|
| [architecture.md](architecture.md) | 想知道"为什么被唤醒""某脚本干什么" |
| [task-review.md](task-review.md) | 收到 `task_review` / `task_run_failed` / `task_run_empty`，或要改某个定时任务的提示词 |
| [throttle-and-budget.md](throttle-and-budget.md) | 调唤醒频率/预算，或怀疑被限流、被误报刷屏 |
| [runbooks/operations.md](runbooks/operations.md) | 设施疑似不工作、要核查状态、或做只读巡检 |
| [runbooks/limits.md](runbooks/limits.md) | 判断某结论可不可信、或想知道什么还没验证 |
| [runbooks/supervisor-daemon.md](runbooks/supervisor-daemon.md) | 在新机器上部署、或把设施迁到 Linux / Windows |
| [../INSTALL.md](../INSTALL.md) | **第一次安装**：给 agent 照做的十步（前置、配置、总管会话、守护、验证） |
| [gotcha-send.md](gotcha-send.md) | **往别的会话发消息之前** |
| [gotcha-scan.md](gotcha-scan.md) | 怀疑扫描器误报/漏报，或事件看着不对 |
| [gotcha-goal.md](gotcha-goal.md) | 处理 `future goal` / worker / 票与租约之前 |
| [gotcha-authority.md](gotcha-authority.md) | 拿不准某动作是否越界、或授权口径打架 |
| [gotcha-tools.md](gotcha-tools.md) | 用 `future` CLI / shell / git 之前 |
| [gotcha-records.md](gotcha-records.md) | 提交与记录之前 |
| [git-flow.md](git-flow.md) | **要在本仓改文件/开 PR/收尾同步**（必读） |
| [privacy-and-state.md](privacy-and-state.md) | 决定某文件该不该提交、或处理隐私/运行态 |

每份的第 3 行是「**何时读**」，用触发场景写——那是决定"要不要打开"的唯一依据。
`AGENTS.md` §8 不再复述这份表（复述会漂移），只指向这里。

## 与其他位置的分工

- `AGENTS.md` —— **每轮注入**，只放"每轮都必须晓得"的。
- `POLICY.md` —— 授权口径，**权威**；与本目录冲突时以它为准。
- `FUTURE.md` + `.future/memory/` —— **本部署**的事实、用户口径、事故史、花费实测（不进 git）。
- `state/SNAPSHOT.md` —— 运行态快照（我是谁 / 待处理 / 最近动作），回合第一读。
- `journal/` `reports/` —— 发生了什么的记录，不是手册。
- `.private/profile/` —— **关于用户**（不进 git）；细节见该目录 README。
