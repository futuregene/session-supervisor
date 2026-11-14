# state-store-retention.md — 库会怎么长大：保留策略与维护

> **何时读**：要跑 `store.py --retain` / `--compact`、觉得数据库太大、或要改
> `SLIM_FIELDS` / `EVENT_PAYLOAD_BUDGET` 之前。存储的整体形状见 [state-store.md](state-store.md)。

## 只有 `events` 会无界增长

一条收件箱事件一行。它的 `detail` 里带着别的会话的原文（`awaiting_user` 最多 1500 字），
而且 `detail_json` 一度与 `row_json` **重复存了这份原文**——实测 handled 3393 行里
`row_json` 5.0 MB、`detail_json` 3.6 MB，`events` 表占了整库 17.8 MB 的 15.2 MB。

所以两件事，缺一不可：

- **`detail_json` 只存摘要**（`questionLike` / `task_id` / `runId` / `action_class` / …），
  列存在只为查询时不必解析整行；完整事件在 `row_json` 里。
- **保留策略 = 年龄 + 字节预算**（`store.py --retain`，默认 30 天 / 2 MB）：
  早于窗口的 handled 事件只留 `id/kind/priority/sessionName/summary/时间戳/折叠链`
  + `detail` 里的索引字段，行**不删**；若全量明细的存活部分仍超预算，再按最旧优先继续瘦身。
  年龄管不了体积（一轮扫描最多 25 条，洪水日会翻倍），字节预算能。

`sessionName` 与 `summary` 必须在保留字段里：唤醒卡的 `TRIAGE_FIELDS` 正好是
`id/kind/priority/sessionId/sessionName/summary`，少了它们卡片对每条瘦身事件都显示空。
第 9 节断言把这条钉住（逐字段相等，且**唯一**丢掉的键是 `lastAssistantText`）。
`bin/peek.py` 不读收件箱（它读会话 transcript），所以不受影响。

`inbox/handled.jsonl` **永不被改写**：完整原文在那里。`--export-json` 导出的是库当前的
样子（瘦身行带 `_slim: true`，能区分"策略删的"与"生产者没写"）；一致性检查按身份
（id/kind/handledAtMs）认瘦身行，整行删掉仍然判丢写。

## 量级

```
瘦身前：3393 行 handled，库 17.8 MB / 4336 页
瘦身+compact 后：1890 页 → 7.7 MB；detail_json 3.63 MB → 0.12 MB
```

按约 100 条/天估算：一年后约 3.6 万行，瘦身行约 250 B ⇒ **+9 MB/年**，
且全量明细被预算钉在 2 MB 以内，不随流量线性涨。

## 维护

- **`PRAGMA optimize`** 在进程退出时跑（`atexit`）：这些模块都是短进程（一轮扫描 / 一次审批 /
  一个唤醒回合各一进程），没有长连接可优化。
- **`--compact`** 是一次性 `VACUUM`，同时把已存在的库转成 `auto_vacuum=INCREMENTAL`
  （新建库直接就是这个设置）；之后的瘦身可用 `PRAGMA incremental_vacuum` 增量回收。
  它要求此刻没有别的写入，跑完顺手 `--check-consistency`。
- **WAL 与 checkpoint**：`journal_mode=WAL`、`busy_timeout=5000`、`synchronous=NORMAL`
  （`kill -9` 不丢已提交事务；与旧的整份 `os.replace` 同级，OS 崩溃可能丢最后几条）。
  每个进程退出即关闭连接，SQLite 在最后一个连接关闭时 checkpoint 并把 `-wal` 删掉——
  这就是"看不到 `-wal` 文件"的原因，**不是 checkpoint 被关掉了**；长寿连接仍有 1000 页
  autocheckpoint 兜底。

## 断言

| 断言 | 节 |
|---|---|
| 保留只瘦身年龄/预算命中的 handled，pending 一行不动 | 7 |
| 瘦身保留身份（id/kind/会话/时间戳/折叠链），删除的行仍判丢写 | 7 |
| 瘦身后卡片字段逐字段不变，只丢正文；瘦身行大小不再随正文增长 | 9 |
| 库领先文件=正常，文件领先库=丢写（保留不弄瞎该判据） | 6 |

```bash
python3 bin/store.py --retain --dry-run     # 先看要动多少行、能省多少字节
python3 bin/store.py --retain && python3 bin/store.py --compact
python3 bin/store.py --check-consistency
```
