# 外部事件进来：`bin/trigger.py`

> **何时读**：要让外部应用（GitHub / CI / IM / 邮件 / 自建脚本）往收件箱放事件，或要改 `trigger.py` 的字段之前

（来由与真实经过见 `.future/memory/`，本文件只留机制）

## 一条路

外部应用**只有一条路**：`bin/trigger.py`。它不自己写 `inbox/pending.jsonl`，不调 `future run`，也不碰 `decide.py`。放进来的事件与 `scan.py` 产出的事件**同形状**，所以后面那一整条链（折叠 → 优先级 → 节流 → 唤醒卡）不需要为它开分支。

| 场景 | 命令 |
|---|---|
| 正文是一整条评论/一封信，已经落成文件 | `python3 bin/trigger.py ingest --from event.json` |
| 适配器能管道 | `cat event.json \| python3 bin/trigger.py ingest` |
| 适配器只会往目录丢文件 | `python3 bin/trigger.py drain <dir>`：成功的进 `ingested/`，坏的进 `rejected/` 并附 `.reason` |
| 查已接收过哪些 | `python3 bin/trigger.py seen [--json]` |

**HTTP 不在这份脚本里**：要网络入口就在适配器里写几行 webhook 处理器，验签（HMAC/白名单）之后再调 `ingest`。`ingest` 本身没有鉴权，**不要把它直接暴露到公网**。

## 字段

一行一个 JSON 对象（stdin 或 `--from` 的文件）。

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `source` | 是 | — | `github` / `ci` / `im:<provider>` / `mail` / `file` / 自定的 `<token>` |
| `source_ref` | 是 | — | 人能读的坐标，如 `owner/repo#12`、`chat_id`、`run_id` |
| `thread_key` | 是 | — | 这条事件属于哪条对话，如 `gh:owner/repo#12`；**折叠按它** |
| `dedupe_key` | 是 | — | 一次事实一个键，如 `github:o/r#12:c:12345`；重投＝同一个 id |
| `body` 或 `body_file` | 是 | — | 全文；`body_file` 必须是绝对路径 |
| `summary` | 是 | — | 卡上唯一允许出现的正文摘录，超 200 字截断并记 `summary_chars` |
| `author` | 否 | — | `{login, id, role}`，`role ∈ collaborator/member/stranger/unknown` |
| `url` | 否 | — | 必须是 `http(s)://` |
| `trust` | 否 | `external-unknown` | `system` / `self` / `external-known` / `external-unknown` |
| `action_class` | 否 | `observe` | `observe` / `triage` / `act`（**目前只记录，见文末**） |
| `priority` | 否 | `low` | `high` / `medium` / `low`，被 `trust` 封顶（见下） |
| `at` | 否 | — | 外部世界的发生时间（ISO-8601），只进载荷 |
| `event` | 否 | — | 适配器自己的事件名，如 `comment_created`，只进 `detail` |
| `session_id` / `session_name` | 否 | 空 | 明确要挂到我们某个会话上时才填 |

## 三条不变量

1. **一次事实一个 `dedupe_key`，重投是空操作。** 事件 id 由这个键派生（`ev_ext_<sha12>`），所以重投拿到的是同一个 id、不追加行。记录在 `state/external-seen.jsonl`：**落盘，跨进程**，不是 `scan.py` 那种进程内环。
2. **正文不进卡。** 全文写 `state/external/<sha16>.json`，事件里只有 `summary`（≤200）+ `detail.payload_ref`。陌生人写的字于是变成"这一轮得去读的素材"，而不是一条 user 消息里的指令。
3. **`trust` 封住唤醒预算。** `external-unknown` 一律存成 `priority: low`（原本的要求记在 `priority_requested`），因为它们只能随心跳出现，不能单独唤醒抢走一次 run；`system` / `self` / `external-known` 按投递方说的算。

输入不合法就**整条拒绝**（退出码 2，一个字节都不写，只说哪个字段不对）。`dedupe_key` 写错会静默合并或重复一条事实，所以它必须由适配器自己提供，脚本不替你编。

## 落盘

| 路径 | 内容 | 回收 |
|---|---|---|
| `inbox/pending.jsonl` | 事件本体（与扫描器同一形状，`kind: external`） | ack / 折叠时移走 |
| `state/external-seen.jsonl` | 已接收的 `dedupe_key → id`，只追加 | **无**，只增 |
| `state/external/<sha16>.json` | 全文载荷 | **无**，只增 |

## 折叠与唤醒

- `inbox.fold_key`：有 `thread_key` 的行按 thread 折叠，其余仍按 `(sessionId, kind)`。所以同一个 PR 的一串评论在卡上只留最新一条，其余进 `handled.jsonl` 带 `foldedInto`。
- `bin/config.json` 的 `fold_superseded_kinds` 必须包含 `external`，否则 `decide.py` 不会折叠它（缺省值取自 `inbox.FOLD_KINDS`，那里已经包含）。
- 唤醒仍由 `decide.py` 判：只有 `high`/`medium` 单独唤醒；`external-unknown` 恒为 `low`，只搭心跳。
- 卡上每个 `(会话, kind)` 一行；外部事件没有 `sessionId`，所以各自一行，`sessionName` 默认是 `<source>:<source_ref>`。

## `action_class` 是闸，不是标签

`observe` / `triage` / 空值 / 任何不认识的值，在 `bin/send.py` 与 `bin/dispatch.py` 里都是**拒绝**：

| 调用 | `act` | 其它任何值（含空、未知） |
|---|---|---|
| `send.py <会话> <正文>` | 发送 | 退出码 **6**，一个字节都不写 |
| `send.py --new …` | 建会话并发送 | 退出码 **6**，**不建会话** |
| `dispatch.py add …` | 写一行派工 | 退出码 **6**，**不写台账** |

- **fail-closed**：只有精确的 `act` 放行。新类名不会自动成为新权限——适配器发明一个 `auto`，它只会被拒绝。
- **拒绝时零副作用**：不做会话查询、不进 `state/deferred-sends.jsonl`（那是"暂时发不出去"的队列，不是绕过闸的后门）、不写 guard 台账、不建会话。
- **记录优先于声明**：`--event <id>` 让收件箱里的记录说话——事件写的是 `observe`，调用方嘴里的 `--action-class act` 会被当成矛盾直接拒绝；收件箱里查不到的 id 也拒绝（无法核对）。
- **不带这两个参数 = 今天的行为**：人工派单（没有事件来源）与既有拒绝语义（会话忙 3、goal worker 4、只知悉 5）一个字都没变；闸只加在它们**前面**。
- `close` / ack **不受闸限制**：记录发生过什么，正是只读事件允许产生的结果。

## 还没做（别当成已经有了）
- 没有把 `thread_key` 与 `dispatch.jsonl` 的会话对上做"直投 worker"。
- `state/external-seen.jsonl` 与 `state/external/` 没有回收，长期只会变大。
- 并发：同一个 `dedupe_key` 被两个适配器同时投递时，可能各追加一行（**id 相同**，折叠会并成一条）。这是刻意不引入锁的取舍——加锁会让一个卡住的适配器拖住所有投递。
