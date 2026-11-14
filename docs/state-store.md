# state-store.md — 热状态与台账的 SQLite 存储

> **何时读**：怀疑 `state/` 的文件与数据库不一致、要调 `state/store-mode.json` 的开关、
> 要改 `bin/scan.py` 等写状态的路径、要跑保留策略或 VACUUM、或要判断一张空表是否该有数据。

## 为什么有它

`state/state.json` 之前整份读、整份写（扫描每 30 秒一轮，实测 640 KB / 2738 会话，
约 1.8 GB/天），而每轮真正变化的会话只有个位数；`approval-cache.json` 同一形状。
数据库把热状态与台账收进来，让**写入量正比于"变化的那几行"**。

分界只有一条：**机器记账 → 数据库；人读的记录 → 纯文本**。
`journal/` `reports/` `.future/memory/` `logs/` 永远不进库——要能 `grep`、能当证据、能被推翻。

## 进库与不进库

进库（`state/supervisor.db`）：`sessions` + `event_keys`（每会话热状态与 25 键去重窗口）、
`meta`（`lastScanTs`/`deferred`/`peakSessions` 等小键）、`events`（pending 与 handled 合一张，
`state` 区分）、`approvals` + `approval_ledger`、`dispatch`、`sent`、`wakes`、
`task_review`（`last_reviewed_ms` 与计数）、`health`（最近 288 轮）。

## 三步，各自可回退

**步 1** 只新增 `bin/store.py`（建库、`--from-json`、`--export-json`），没人调用它；
**步 2** 逐模块 dual-write（先写库、再写旧文件），回退是 `--disable <模块>`；
**步 3** 停旧写入并把读路径改走库，回退是 `--enable` 或 `--export-json`。
步 1、2 **不改变读取路径**，所以出问题只需关开关，不必回滚代码。

## 写路径的不变式

1. **先写库、再写旧文件**：于是"文件有、库没有"只可能是镜写失败（缺陷），
   "库有、文件没有"只是两次写之间的一瞬（正常）。一致性检查按这个不对称判据判定。
2. **镜写失败只记 `logs/store.log`，不改调用方结果**：旧文件仍是权威，扫描不会因
   数据库问题停下（同"扫描在跑但一轮也没跑完"那一类事故）。
3. **临时路径不写库**（`store.is_live`）：selftest 用临时目录时天然不碰线上库。
4. **首次运行自动播种**：第一次镜写在一个事务里导入旧文件并写 `seeded_at`，
   两个进程同时启动也只有一个播种成功——部署不需要停机步骤。
5. **一致性检查先拍文件快照、再读库**，否则"刚写完库、还没写文件"会被误判成丢写。
6. **每个写文件的模块都必须调用对应的镜写**：`selftest_store.py` 第 8 节直接读源码
   核对清单（删掉一条镜写，那个检查就红）。运行时看不出来的断线只能这样钉。

## 库会怎么长大：保留策略与维护

只有 `events` 会无界增长（一条收件箱事件一行）。它的 `detail` 里带着别的会话的原文
（`awaiting_user` 最多 1500 字），`detail_json` 一度与 `row_json` **重复存了这份原文**
（实测 3.6 MB vs 5.0 MB）。所以：

- **`detail_json` 只存摘要**（`questionLike`/`task_id`/`runId`/`action_class`/…），
  列存在只为查询时不必解析整行；完整事件在 `row_json` 里。
- **保留策略 = 年龄 + 字节预算**（`store.py --retain`，默认 30 天 / 2 MB）：
  早于窗口的 handled 事件只留 id/kind/priority/会话/时间戳/折叠链 + `detail` 摘要，
  行**不删**；若全量明细的存活部分仍超预算，再按最旧优先继续瘦身。
  年龄管不了体积（扫描一轮最多 25 条，洪水日会翻倍），字节预算能。
- **`inbox/handled.jsonl` 永不被改写**：完整原文在那里。
  `--export-json` 导出的是库当前的样子（含瘦身后的行，带 `_slim: true`）。
- **量级**：3393 行 handled → 瘦身 17.8 MB → **7.7 MB**（`--compact` 后 1890 页）；
  按约 100 条/天估算，一年后约 3.6 万行、瘦身行约 250 B ≈ **+9 MB/年**。
- **维护**：`PRAGMA optimize` 在进程退出时跑（`atexit`，短进程的正确位置）；
  `--compact` 是**一次性** `VACUUM`，同时把已存在的库转成 `auto_vacuum=INCREMENTAL`
  （新建库直接就是这个设置），之后的瘦身可以用增量回收。
- **WAL 与 checkpoint**：`journal_mode=WAL`、`busy_timeout=5000`、`synchronous=NORMAL`
  （`kill -9` 不丢已提交事务；与旧的整份 `os.replace` 同级，OS 崩溃可能丢最后几条）。
  每个进程退出时关闭连接，SQLite 在最后一个连接关闭时 checkpoint 并把 `-wal` 删掉——
  这就是"看不到 `-wal` 文件"的原因，不是 checkpoint 被关掉了；长寿连接仍有 1000 页
  autocheckpoint 兜底。

## 空表怎么判：`--probe`

`approval_ledger` 一直是 0 行。"从没决策过"和"镜写断了"在表上长得一模一样，所以有探针：

```bash
python3 bin/store.py --probe approval_ledger
# rows_before / rows_seen / rows_after / cleaned
```

它走 `bin/approvals.py` 用的**同一处调用**写一行、读回、删掉，`rows_seen: 1` 才算接上。
零行是正确的条件：`state/approvals.jsonl` 与 `reports/approvals.md` 都不存在（两者都在
第一次决策时才创建）；否则就是丢写，`store consistency` 也会红。

## 合并之后立刻核两个输出

主检出的 `bin/` 就是线上代码——watchd 每 30 秒 exec 一次，没有暂存区，也没有"部署"这一步。
所以每次合入后**当场**贴这两样，别等下一次汇报：

```bash
tail -2 logs/scan.log                     # 要看到本轮的 `[scan] sessions=… events=…`
python3 bin/store.py --check-consistency   # 要看到 "agrees with … on all 10 legacy files"
```

代码合法、退出码 0、日志还在写，但什么都没干——这是已经发生过一次的事故形态
（`docs/gotcha-scan.md`）。

## 开关与观测

```bash
python3 bin/store.py modules                  # 每个模块现在是否在镜写
python3 bin/store.py --disable inbox | --enable inbox
python3 bin/store.py stat                     # 行数、逻辑字节、页数、freelist、auto_vacuum
python3 bin/store.py --retain [--days N] [--dry-run] | --compact
python3 bin/store.py --check-consistency [--no-lost] | --export-json <dir>
python3 bin/store.py --probe approval_ledger
python3 bin/selftest_store.py                 # 去重/崩溃/并发/写放大/保留/接线
```

`state/store-mode.json` 是开关本体（缺省=全开），运行态，不进 git。
selfcheck 挂两条：`store selftest`、`store consistency`（无线上库时说明后跳过）。

## 步 3 之前必须想清楚的事

步 3 把库变成**权威**，读路径也要换（`scan.py` 自己、`blind.py`、`state_snapshot.py`、
`inbox.py`）。它要求**连续若干轮一致**作为前提，因为"写了一半、看着在写其实没落盘"
只在多次运行后才显形，一次性验证看不见。没有那个观察窗口，就不要动读路径。
