# 提案：外部事件源触发器（trigger.py）

状态：**部分落地**——字段、去重、正文与卡分离、按 thread 折叠已实现（`bin/trigger.py`，
机制见 [../external-events.md](../external-events.md)）；`action_class` 的引脚闸门、直投 worker、回收仍未做。
本页保留为设计依据（威胁模型、信任分级、限速与验收指标）。

> **何时读**：要决定接不接外部事件源、或实现 `trigger.py` 之前

现在这套设施的九种事件全部来自**会话自己的状态机**（`docs/architecture.md`「事件种类」），
外部世界发生的事——CI 红了、PR 有人提意见、群里有人报错——一条都进不来。要接，就得先决定
两件比"写个消费者"更贵的事：**别人写的正文凭什么能叫醒我**，以及**叫醒之后这一轮准它做什么**。
本页只定义接口、字段与闸门，不实现。

## 1. 威胁模型

现有事件与外部事件有一个本质区别：前者的 `detail` 是我们的脚本从 session 状态里算出来的，结构固定；
后者的正文是别人写的。而唤醒卡本来就是一条 **user 消息**（`prompts/turn.md`），投递通道只给它加一行抬头
`会话总管转达 · <intent> · 非用户本人`（`bin/send.py:235`）。抬头说明的是"谁发的"，不是"内容可信不可信"。
把外部正文原样落进卡里，等于让一条 GitHub 评论以用户回合的身份，进入一个拥有 shell、能派工、
能自动批准权限的会话。

被诱导的是三步，每一步今天都通：

1. **准入**：正文里的指令被读成"要办的事"，而不是"某人说了一句话"；
2. **能力**：这一轮有 shell 与 `send.py --new`，注入的动作当场执行，或被派给一个全权限 worker；
3. **放大**：worker 的权限请求走 `approvals.py auto`，只有 `DENY_PATTERNS` 那一小张表会拦
   （`bin/approvals.py:106-115`：发版、部署、删库、强推，当前 21 条），其余自动批准。
   数的是条数不是分支数——表里有几条自带 `(a|b|c)` 选择，所以编译出的正则分支多于条数；
   这里的结论不依赖具体数字：**表之外的任何动作都会被自动批准**。

现有挡板各自挡住什么、挡不住什么：

| 位置 | 挡住 | 挡不住 |
|---|---|---|
| `bin/send.py:632-639` quiet gate | "投递一条不需要动作的消息"（省一次 run） | 判据只看抬头的 intent 词，不看正文可信度 |
| `bin/send.py:653-660` / `:661-670` | 给 goal worker 发消息；给 mid-run 会话发消息 | 路由与时机，不涉及内容 |
| `bin/guard.py check` | 同一会话的重复打扰（防打转） | 按会话计数，没有来源维度 |
| `bin/inbox.py:47-55` fold_superseded | 同一会话、同一 kind 的多条状态 | 键是 `(sessionId, kind)`，外部事件没有"会话状态"可依 |
| `bin/scan.py:100-112` event_key | 同一状态重复上报（`stamp` = 会话最后活动） | 该键的前提是"会话动过"，外部事件没有这个前提 |
| `bin/dispatch.py:17-22` | 派工必须带 `accept`（没人能判的行不接受） | 记录的是"我要求了什么"，不记录"谁要求我"——注入来的派工事后与用户授权的无法区分 |
| `bin/lock.py` | 两个回合并发 | 不涉及内容 |

结论：准入层缺**来源**这一维，能力层缺**"这一轮能做什么"**的约束，追溯层缺**"谁要求"**的记录。
三样都补上，外部事件才和现有事件同级；补不上，就不要接。

## 2. 事件 schema

一次 `trigger.py ingest` 落一行；字段在进 `inbox/pending.jsonl` 之前就被定死，正文之后不得再改写它们。

| 字段 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `source` | `github` / `ci` / `im:<provider>` / `mail` / `file` | 必填无默认 | 适配器标识；信任判定与限速的第一维 |
| `source_ref` | `owner/repo`、`chat_id`、`run_id` | 必填 | 人类可读坐标，进卡时原样显示 |
| `author` | `{login, id, role}`，`role ∈ collaborator/member/stranger/unknown` | `role=unknown` | 只认平台给的字段，不根据名字或内容猜 |
| `url` | 绝对 URL | 空 | 供人核对；渲染禁止 raw HTML 与自动加载图片 |
| `trust` | `system` / `self` / `external-known` / `external-unknown` | **`external-unknown`** | 由适配器 + `config.external_known[]` 决定；正文与 `author.role` 都不得提升它 |
| `thread_key` | `gh:<repo>#<n>` / `im:<chat>:<topic>` / `ci:<provider>:<run>` | 必填，无法归属时 `source:object` | 合并与直通的键；与 `state/dispatch.jsonl` 的 `session` 对上即可直投 worker |
| `dedupe_key` | `source:object_id:revision` | 必填 | 见 §4，`revision` 用平台自己的版本字段 |
| `action_class` | `observe` / `triage` / `act` | **`observe`** | 不是事件的性质，是这一轮**允许做什么**的上限 |
| `priority` | `high` / `medium` / `low` | `low` | 与 `bin/decide.py:29` 的 `NOTIFY_PRIORITY` 同一套词 |
| `payload_ref` | `state/external/<sha>.json` | 必填 | 正文落盘，卡上只放摘录（≤200 字）；正文永不整段进卡 |

`action_class` 怎么变成硬约束（三条都在脚本里，不在提示词里）：

- `observe`：卡上只有结构化字段 + 摘录；回合内 `send.py` 拒绝 `--new`、`dispatch.py add` 拒写、
  只能写 `inbox` ack 与 `journal`。
- `triage`：允许 `bin/needs-you` 升级、允许开**只读**调研会话，不允许派改代码的工单。
- `act`：完整工具面。
- 校验方式：三个脚本各收一个 `--action-class`，各有一条拒绝分支；`bin/selfcheck.py` 各加一条
  **负向**断言（必须拒绝成功），不是"应当拒绝"的注释。

## 3. 信任分级 → 能力

| trust | 谁产生 | 实时唤醒 | 允许 | 禁止（机械判据） |
|---|---|---|---|---|
| `system` | 自己的 CI/部署 webhook，HMAC 验签通过 | 是 | `act`，可直接派工 | 正文里的自由文本（日志、PR 标题）不得作为动作来源：卡上只渲染 run id、conclusion、失败 job 名；正文单独标记、单独摘录 |
| `self` | 自己派出的 worker / 定时任务回执 | 是 | `act` | worker 转述的第三方原文按 `external-*` 重新定级，不能继承 `self` |
| `external-known` | 用户写进 `config.external_known[]` 的仓库或群 | 是，受每源限额 | `triage`；命中已有 `thread_key` 的派工行时可 `act` | 无 `thread_key` 或查不到派工行时，`send.py --new` 必须非 0 退出（负向断言） |
| `external-unknown` | 其余一切 | **否** | `observe`，只进每日简报汇总 | 不进 `NOTIFY_PRIORITY` 候选，`decide.py` 不为它单独唤醒；`action_class` 恒为 `observe` |

`external-unknown` 不实时唤醒，不是谨慎，是结构解法：**陌生来源不参与争抢唤醒预算**，
一个群刷屏换不来一次唤醒（限额是第二道，见 §4）。

## 4. 去重与限速

- **键**：`dedupe_key = source:object_id:revision`，`revision` 取平台的版本字段（comment 的
  `updated_at`、run 的 `attempt`）。同键只更新摘录、不产事件。落 `inbox/external-seen.jsonl`：
  跨进程存活，不能靠 `scan.py` 那种进程内 `seen` 列表（`bin/scan.py:343-348`）。
- **折叠**：同一 `thread_key` 在 `thread_coalesce_seconds`（默认 120）内的事件并成一条，
  沿用 `fold_superseded` 的思路，键从 `(sessionId, kind)` 扩成 `(thread_key)`；保留 id 作 ack
  句柄这一点不变（`bin/inbox.py:55-60` 的注释解释了为什么不能只渲染最新一条）。
- **限额**：新增 `max_external_wakes_per_source_hour`（默认 4）与 `max_external_wakes_hour`
  （默认 8）。全局 `max_wakes_per_hour`（现 12，`bin/decide.py:491`）给外部划的是**上限不是配额**：
  外部最多 8，内部事件永远保底 4。这样"一个源刷屏"最多消耗自己那 4 次，挤不掉别的源，
  更挤不掉内部事件。
- **与现有节流的关系**：`min_wake_gap_seconds`（`bin/decide.py:478`）照常对合并后的一次唤醒生效；
  `low_wake_minutes`（`bin/decide.py:487`）只管 low：`external-unknown` 根本不实时唤醒，
  `external-known` 的 low 与现有 low 同待遇，不另开一套。

## 5. 验收与观察

| 指标 | 怎么取 | 达标 | 告警 |
|---|---|---|---|
| 唤醒次数/天，按 `source × trust` 分 | `logs/decide.log` 与 `inbox/handled.jsonl` 按 kind/source 聚合 | 外部 ≤ 8/天；单源 ≤ 4/h | 连续两天触顶 → 收窄该源，或提前做直通 |
| 每轮事件数（batch size） | 每次唤醒卡里 pending 的条数 | p50 ≥ 2 | p50 = 1 且唤醒数偏高 → 门禁太松，先收紧 |
| 以"无需行动"结束的比例 | 回合末 journal 行里写 `no-action` 的比例 | ≤ 30% | > 50% → fast path 判据不足 |

节奏：上线第一周每天看一次（并在 08:30 简报里出一行），之后每周一次。三个数连续两周达标，
才谈第二个源。这三个数对应文章里的两个数字（batch 让单次决定的分摊成本下降、直通省 37% 的
read-cache token）：前者验合并，后者验直通该不该做。

## 6. 不做什么

- 不实现 `trigger.py`，**不新增 kind**（`kind` 字面值属于实现阶段），不改 `decide.py`、`inbox.py`、
  `send.py`、`approvals.py`、`guard.py`。
- 不接任何真实事件源，不开监听的网络端口；IM 侧桥接（future-os 的 inbound webhook）不在本页范围。
- 不改 `POLICY.md`：信任分级若要进授权白名单，是立项后的单独一改。
- 不做直通（thread → worker）的实现，只在该字段里预留 `thread_key`。
