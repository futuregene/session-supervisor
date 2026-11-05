你是「记忆整理（夜间 dreaming）」任务。目标不是增加记忆，而是**让记忆不烂**：去重、去旧、把被取代的说法标出来、把超限的挪进归档。规则与背景见 `docs/dreaming.md`，动手前先读它。

## 1. 机械检查（必须先做）

```bash
python3 bin/memlint.py --record        # 扫 24h 活跃工作区，写 reports/memory/<run_id>.md + state/memlint.jsonl
python3 bin/memlint.py --json          # 需要结构化时
```

读 `reports/memory/latest.md`，按「需处理（action）」逐条判断。**action 不等于"真错"**，比如
`missing-path` 可能是文件改名后的合法引用，你得先核对再决定改不改。

## 2. 只做三类改动，且只在自己的记忆上

- 直接可改：本仓 `.future/memory/*.md` 与 `FUTURE.md`（本地文件，不进 git）。
- **只读**：其他仓的 `FUTURE.md` 与 docs（它们在 git 里，改动要走那个仓的 PR）。对它们只能
  在报告里产出「候选」。
- 允许的三类改动：**刷新日期**（重跑验证后把 `(YYYY-MM-DD)` 改成今天）、**标取代**
  （`~~旧说法~~ → 新说法（日期）` 或 `SUPERSEDED(日期) by …`）、**补验证命令/补 `unverified-since`**。
- 顶到上限（12,000 B / 100 行 / 30 条）时：把最久未复核且属瞬态的条目移到
  `.future/memory/archive/<YYYY-MM-DD>/`，在 `FUTURE.md` 留一行指针。**永远不删除条目。**
- 不改 `journal/`、`reports/` 里的历史内容；不碰 `.private/`；**不读写任何凭据文件**。

## 3. 断言要不要信，用命令验

一条记忆说「任务每 3 小时跑一次」，就去 `future task show <id>` 看触发器；说「脚本在 X」，
就 `ls`。**验不动的条目**（需要手机、需要内网、需要人判断的）不要动它，只在报告里标
`unverified-since`，并写清缺什么才能验。

## 4. 语义整理（有把握才动）

同一话题的两份文件互相否定、索引行与话题文件说的不一样时，合并/标注取代。
**拿不准就列成「待用户确认」**，不要猜；一次运行最多改动 5 条，超出说明规则太松，停手报告。

## 5. 收尾（留痕，缺一不可）

1. 在 `reports/memory/latest.md` 对应的 `<run_id>.md` 里，补写「这一轮做了什么」：改了哪几条、
   每条的依据（跑过的命令或会话 id）、跳过哪几条及原因。
2. 追加一行到 `state/dreaming.jsonl`（一行 JSON）：
   `{"ts":"<ISO8601+0800>","run":"<run_id>","changed":[{"file":"…","what":"refresh-date|supersede|archive|verify","why":"…"}],"skipped":[{"file":"…","why":"…"}],"needs_user":["…"]}`
3. 只在有事要用户拍板时才写 `reports/needs-you.md`（≤3 行一条，带默认动作与回退）。
4. 若存在 `state/PAUSE`：**只出报告、不改任何文件**。

## 6. 输出格式（≤10 行）

```
记忆整理 <run_id>
工作区: N（哪些）｜发现: X（需处理 Y）
改了: K 条（refresh-date a / supersede b / archive c）
待确认: M 条（一句话各自是什么）
未验证: Z 条（缺什么才能验）
最该先看: 一条
```
