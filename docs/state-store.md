# state-store.md — 热状态与台账的 SQLite 存储

> **何时读**：怀疑 `state/` 的文件与数据库不一致、要调 `state/store-mode.json` 的开关、
> 要在本仓改 `bin/scan.py` 等写状态的路径、或要决定"能不能停掉旧文件写入"时。

## 为什么有它

`state/state.json` 之前是**整份读、整份写**：扫描每 30 秒跑一轮，就把全部会话的状态重写一遍
（本部署实测 640 KB / 2738 个会话，约 1.8 GB/天），而每轮真正变化的会话只有个位数。
`state/approval-cache.json` 同一个形状。台账（`inbox/handled.jsonl`、`state/wakes.jsonl`、
`state/sent.jsonl`、`state/dispatch.jsonl`）是只追加的，但处理时仍然整份重写或整份扫描。

数据库把这三类东西收进来，让**写入量正比于"变化的那几行"**，而不是"整个状态"。
判断标准只有一条，别扩张：**机器记账 → 数据库；人读的记录 → 纯文本**。
所以 `journal/` `reports/` `.future/memory/` `logs/` **永远不进库**：它们要能 `grep`、
能被引用为证据、能被后来的人打脸。

## 进库与不进库

| 进库（`state/supervisor.db`） | 表 |
|---|---|
| 每会话热状态（`sig`/`emitted`/`lastOrdinal`/…，原样 JSON） | `sessions`、`event_keys` |
| `lastScanTs` / `deferred` / `peakSessions` 等小键 | `meta` |
| 事件台账：pending + handled **合成一张**（`state` 区分） | `events` |
| 事件去重（每会话一个窗口） | `event_keys` |
| 权限申请缓存 + 自动批准台账 | `approvals`、`approval_ledger` |
| 派工、已发消息、唤醒记录 | `dispatch`、`sent`、`wakes` |
| 定时任务复盘的 `last_reviewed_ms` 与计数 | `task_review` |
| 最近若干轮扫描健康（不再每轮覆盖） | `health` |

不进库：`journal/`、`reports/`、`.future/memory/`、`logs/`、`state/SNAPSHOT.md`。

## 三步，各自可回退

| 步 | 内容 | "算完成"的判据 | 回退 |
|---|---|---|---|
| 1 | 只新增 `bin/store.py`：建库、`--from-json` 导入、`--export-json` 导出 | 导入→导出与原文件**逐语义相等**（`store.py --selftest`） | 没人调用它，什么都不用做 |
| 2 | 逐模块 dual-write：**先写库、再写旧文件** | 库与旧文件一致，且"文件有、库没有"必须为 0 | `store.py --disable <模块>` |
| 3 | 停旧写入（读也改走库，旧文件只留导出快照） | 连续若干轮（≥ 一个扫描周期数倍）一致 | `--enable <模块>` 重新打开，或 `--export-json` 回到文件 |

**步 1、2 不改变读取路径**：所有读者照旧读旧文件，所以任何一步出问题只需关开关，
不需要回滚代码。这是"线上不允许停摆"的具体做法。

## 写路径的不变式（读代码时先看这一条）

1. **先写库、再写旧文件。** 所以"文件有、库没有"只可能是镜写失败 → 缺陷；
   "库有、文件没有"只是两次写之间的一瞬间 → 正常。一致性检查按这个不对称判据判定，
   否则检查会在一秒钟的竞态上闪红，然后被人改到什么都不查。
2. **镜写失败只记日志，不改调用方结果。** `store.mirror()` 吞掉异常并写
   `logs/store.log`；旧文件仍是权威，扫描不会因为数据库问题停下来
   （这与"扫描在跑但一轮也没跑完"是同一类事故，见 `docs/gotcha-scan.md`）。
3. **临时路径不写库。** 每个镜写都拿目标路径与部署自己的路径比一次
   （`store.is_live`），所以 selftest 用临时目录时天然不会碰线上库，也不需要知道这个模块存在。
4. **首次运行自动播种。** 第一次镜写时在一个事务里把旧文件导入并写 `seeded_at`，
   两个进程同时启动也只有一个播种成功。部署不需要停机步骤。

## 开关与观测

```bash
python3 bin/store.py modules                 # 每个模块现在是否在镜写
python3 bin/store.py --disable inbox         # 只把这一个模块退回旧实现
python3 bin/store.py --enable inbox
python3 bin/store.py stat                    # 行数、库/WAL 体积、旧文件体积
python3 bin/store.py --check-consistency --no-lost      # 线上：库 vs 旧文件
python3 bin/store.py --export-json /tmp/out  # 回退：导回今天的文件格式
python3 bin/store.py --selftest              # 夹具 round-trip（CI 可跑）
python3 bin/selftest_store.py                # 去重等价 / 崩溃 / 并发 / 写放大
```

`state/store-mode.json` 就是开关本体（缺省=全开）。它是运行态，不进 git。

## 断言在哪

| 断言 | 位置 |
|---|---|
| 导入→导出逐语义相等 | `store.py --selftest`（夹具）、`--check-consistency`（线上） |
| 每个模块的镜写动作导出回自己的文件 | `selftest_store.py` 第 5 节 |
| 去重与 `emitted` 窗口等价 | `selftest_store.py` 第 1 节 |
| 崩溃安全（`kill -9` 中途） | `selftest_store.py` 第 2 节 |
| 并发无 `database is locked`、`busy_timeout` 真等待 | `selftest_store.py` 第 3 节 |
| 写放大（O(变化行) vs O(整状态)） | `selftest_store.py` 第 4 节（有线上文件时量线上） |
| selfcheck 里挂哪两条 | `bin/selfcheck.py` 的 `store selftest` / `store consistency` |

## 步 3 之前必须想清楚的事

步 3 不是"再改一行"：它把库变成**权威**，读路径也跟着换（`scan.py` 自己、
`bin/blind.py`、`bin/state_snapshot.py`、`bin/inbox.py` 都要改）。
它要求**连续若干轮扫描一致**作为前提，因为这类错误（写了一半、看着在写其实没落盘）
只在多次运行后才显形，一次性验证看不见。没有那个观察窗口，就不要动读路径。
