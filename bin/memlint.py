#!/usr/bin/env python3
"""Mechanical lint for the workspace memory that this facility keeps and reads.

Why this exists (user, 2026-11-06): the value of a background "dream" pass is not
adding more memory but *consolidating and forgetting* — the memory layer degrades by
piling up. Measured on this machine before writing this file:

    * `future-os/FUTURE.md` was 12,027 bytes — 261 B short of the loader's 12,288-byte cap (12 KB),
      not already truncated; but the index grows by appending, and past that line the tail goes silently;
    * of 52 topic files across three workspaces, 28 carried appended corrections
      ("补/更正/已改/不再") and **none** used a strikethrough or a SUPERSEDED marker,
      so a reader going top-down meets the stale claim first;
    * 8 files carried no date at all, so "verify anything older than 7 days" could
      never fire on them;
    * one index line stated a task cadence of "每 3 小时" while the task actually ran
      every 60 minutes — the kind of error that reading the text again cannot find,
      because the text does not contradict itself.

Two rules follow from those, and this script enforces only what is mechanical:

1. **Age never deletes.** Seven days means "re-check before relying on it". Removal is
   caused by being disproven or superseded, and a store that is too big gets an
   *archive* suggestion, never a deletion (see `docs/dreaming.md`).
2. **A claim counts as a fact only if something can check it.** Anything that names a
   path, a command, a port or a period should carry a way to confirm it. The cheap,
   safe half of that is here: repo-relative paths quoted in backticks are resolved.
   Memory files are treated as *untrusted input* — no command from them is ever run,
   which is why this script verifies paths by walking the filesystem instead.

What it deliberately does not do: judge whether a statement is true in prose, rewrite
any file, or run anything a memory file asks for. Those belong to the stage-2 agent and
to the owning repository's review.

Usage:
    python3 bin/memlint.py                      # 24h-active workspaces, write the report
    python3 bin/memlint.py --workspace /path    # explicit (repeatable), print only
    python3 bin/memlint.py --json               # machine-readable findings
    python3 bin/memlint.py --record             # append this run to the audit ledger
    python3 bin/memlint.py --actions-file F     # JSON list of what the run changed
    python3 bin/memlint.py --selftest

Exit codes: 0 no actionable finding, 1 actionable findings, 2 the scan itself failed.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(WATCH, "state")
REPORTS = os.path.join(WATCH, "reports", "memory")
LEDGER = os.path.join(STATE, "memlint.jsonl")

# Caps from the memory convention this facility is written against (30 entries /
# 100 lines / 12 KB). Kept here so the check and the loader cannot drift apart
# silently; if the loader's cap changes, this constant changes with it.
CAP_BYTES, CAP_LINES, CAP_ENTRIES = 12 * 1024, 100, 30  # B = future-os/agent/src/prompt/mod.rs:174-175 (MEMORY_INDEX_MAX_KB = 12)
STALE_DAYS = 7
ACTIVE_WINDOW = 24 * 3600

ENTRY = re.compile(r"^\s*[-*]\s+\[(?P<title>[^\]]+)\]\((?P<dest>[^)]+)\)", re.M)
DATE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
ANY_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A supersession is an act, not a word: the line must both say that something was
# replaced *and* name what it replaced. Measured 2026-11-06, before narrowing — the
# vocabulary alone (补/更正/已改/不再/后来/…) fired on 9 of 34 topic files in one
# workspace and none of the nine voided a claim: `不再新建 socket` describes the
# world, `发现时应顺手修正` is an instruction, `后来起的 worktree` is a sequence. What
# they share is the word, not the act. So the test is conjoint, and confined to one
# line (`此前写的是 A，已改为 B`); the strikethrough form it recommends stays exempt.
SUPERSEDE_VERB = re.compile(
    r"(不再|已不成立|已作废|作废|取代|废弃|改为|改成|更正|修正|已改|superseded|deprecated)", re.I)
PRIOR_CLAIM = re.compile(
    r"(此前|原先|旧说法|旧记录|旧结论|旧值|本文早先|之前写的是|之前写的|之前写|以前写的|以前写|曾写|已不成立)",
    re.I)
SUPERSEDED_MARK = re.compile(r"(~~|SUPERSEDED\()", re.I)
# A repo-relative path quoted in backticks: has a slash, no spaces, not a URL, not a
# home-absolute path (those belong to another machine and cannot be checked here).
# The first segment must look like a directory that exists in this kind of repo: a
# bare `future/deepseek-flash` is a model id, `origin/main` a git ref and
# `owner/repo` a GitHub slug — none of them are paths, and treating them as paths
# produced 25 false "missing path" findings on the first run (2026-11-06).
QUOTED_PATH = re.compile(r"`([A-Za-z0-9._/@+-]*/[A-Za-z0-9._/@+-]+)`")
PATH_ROOTS = {".future", ".worktrees", "bin", "docs", "prompts", "scripts", "src",
              "tests", "test", "mobile", "desktop", "harness", "tui", "crates",
              "packages", "skills", "apps", "lib", "internal", "cmd", "assets"}
# Build output and placeholders: a memory file may legitimately name a path that only
# exists after a build, or write `a/.../b` as shorthand. Reporting those as broken
# references buried the real ones under noise on the first run (2026-11-06).
BUILD_DIRS = {"coverage", "build", "target", "outputs", "intermediates",
              "node_modules", "dist", ".next"}
# A path that a memory file has already *told the reader is gone* is not a broken
# reference — reporting it as actionable every night buries the real ones. Measured
# 2026-11-07: 6 of 6 actionable `missing-path` findings in this repo were historical
# references to deleted files (`prompts/dev-pr.md` removed in `5c7746f`, two removed
# worktrees), all re-reported since 2026-11-06 because the text was semantically right
# and the detector could not see the "已撤掉" next to it. Two forms are recognised,
# both of which a reader can already follow: a struck-through span (`~~…~~`, the same
# convention `unmarked-supersession` accepts) and an explicit removal verb on the same
# line. Severity drops to `info` rather than disappearing, so the audit trail stays.
# 2026-11-08: `已删` alone was missing, so the natural phrasing `已删的 X` (and `已删掉`)
# re-created two actionable findings from the very paragraph that recorded them as
# historical — twice in one night, while editing that paragraph. The bare stem covers
# `已删的` / `已删掉` / `已删除`; no line uses it to mean anything else.
STRUCK = re.compile(r"~~[^~]{1,2000}?~~", re.S)
REMOVED = re.compile(r"已删|已移除|已被删除|已不存在|现已不存在|从仓库删掉|删除了|撤掉")


def superseded_line(line: str) -> bool:
    """Does this one line declare that an earlier statement no longer holds?

    Both halves are required: the replacing verb alone is ordinary prose, and the
    reference to the earlier statement alone says nothing was replaced. A line that
    already strikes the old text through (or carries `SUPERSEDED(…)`) is what the
    convention asks for, so it is not reported.
    """
    if SUPERSEDED_MARK.search(line):
        return False
    return bool(SUPERSEDE_VERB.search(line) and PRIOR_CLAIM.search(line))


def looks_like_repo_path(p: str) -> bool:
    if p.startswith(("/", "~", "./")) or "://" in p or "@" in p:
        return p.startswith("./")
    if "..." in p:
        return False
    if any(seg in BUILD_DIRS for seg in p.split("/")):
        return False
    first = p.split("/", 1)[0]
    return first in PATH_ROOTS


def load_state_sessions() -> dict:
    try:
        with open(os.path.join(STATE, "state.json"), encoding="utf-8") as fh:
            return json.load(fh).get("sessions", {}) or {}
    except Exception:
        return {}


def active_session_ids(now=None) -> list:
    now_ms = (now or time.time()) * 1000
    return [sid for sid, v in load_state_sessions().items()
            if now_ms - (v.get("lastUpdMs") or 0) < ACTIVE_WINDOW * 1000]


def cwds_of(session_ids: list) -> list:
    """Workspaces of those sessions, via the session registry.

    `state.json` records activity per session but not where it ran, so the mapping
    comes from `future session list`. A CLI that is down is not an error for a lint
    run: the caller then passes `--workspace` explicitly.
    """
    if not session_ids:
        return []
    try:
        out = subprocess.run(["future", "session", "list", "--json"],
                             capture_output=True, text=True, timeout=180,
                             env={**os.environ, "PATH": os.environ.get("PATH", "")})
        sessions = json.loads(out.stdout).get("sessions", [])
    except Exception:
        return []
    by_id = {s.get("id"): s.get("cwd") for s in sessions}
    seen, out_cwds = set(), []
    for sid in session_ids:
        cwd = by_id.get(sid)
        if cwd and cwd not in seen and os.path.isdir(cwd):
            seen.add(cwd)
            out_cwds.append(cwd)
    return sorted(out_cwds)


def has_memory(ws: str) -> bool:
    return (os.path.isfile(os.path.join(ws, "FUTURE.md"))
            or os.path.isdir(os.path.join(ws, ".future", "memory")))


def finding(ws, kind, path, severity, detail, line=None):
    return {"workspace": ws, "kind": kind, "path": path, "severity": severity,
            "detail": detail, "line": line}


def check_index(ws: str, now_date: datetime.date) -> list:
    """Lint `FUTURE.md`: capacity, dating, links, duplicate titles."""
    out = []
    fut = os.path.join(ws, "FUTURE.md")
    if not os.path.isfile(fut):
        return out
    text = open(fut, encoding="utf-8", errors="replace").read()
    entries = list(ENTRY.finditer(text))
    size, lines = len(text.encode()), text.count("\n") + 1

    over = []
    if size > CAP_BYTES:
        over.append(f"{size} B > {CAP_BYTES} B")
    if lines > CAP_LINES:
        over.append(f"{lines} lines > {CAP_LINES}")
    if len(entries) > CAP_ENTRIES:
        over.append(f"{len(entries)} entries > {CAP_ENTRIES}")
    if over:
        # Archive, never delete: the loader would silently drop the tail otherwise.
        out.append(finding(ws, "capacity", "FUTURE.md", "action",
                           "超过上限（" + "; ".join(over) + "）→ 移动到 .future/memory/archive/ 并在索引留一行指针，不要删除"))

    titles = {}
    dests = {}
    for m in entries:
        line_no = text[:m.start()].count("\n") + 1
        title, dest = m.group("title").strip(), m.group("dest").strip()
        tail = text[m.end():text.find("\n", m.end())]
        if not DATE.search(tail):
            out.append(finding(ws, "undated-entry", "FUTURE.md", "info",
                               f"条目「{title[:40]}」没有 (YYYY-MM-DD) —— 无法被「7 天复核」规则命中", line_no))
        else:
            d = DATE.search(tail).group(1)
            try:
                age = (now_date - datetime.date.fromisoformat(d)).days
            except ValueError:
                age = 0
            if age > STALE_DAYS:
                out.append(finding(ws, "stale-entry", "FUTURE.md", "info",
                                   f"「{title[:40]}」已 {age} 天未复核（{d}）—— 依赖前要重核，而不是删除", line_no))
        if dest and not dest.startswith(("http://", "https://", "#")):
            target = os.path.normpath(os.path.join(ws, dest))
            if not os.path.exists(target):
                out.append(finding(ws, "dead-link", "FUTURE.md", "action",
                                   f"「{title[:40]}」指向不存在的 {dest}", line_no))
        titles.setdefault(title, []).append(line_no)
        if dest:
            dests.setdefault(dest, []).append(line_no)
    for title, at in titles.items():
        if len(at) > 1:
            out.append(finding(ws, "duplicate-title", "FUTURE.md", "info",
                               f"同一个标题出现 {len(at)} 次（行 {at}）", at[0]))
    for dest, at in dests.items():
        if len(at) > 1:
            out.append(finding(ws, "duplicate-link", "FUTURE.md", "info",
                               f"同一个目标被索引 {len(at)} 次：{dest}", at[0]))
    return out


def check_topic_files(ws: str) -> list:
    """Lint every topic file: dating, unmarked corrections, unresolvable paths."""
    out = []
    mem = os.path.join(ws, ".future", "memory")
    if not os.path.isdir(mem):
        return out
    for name in sorted(os.listdir(mem)):
        if not name.endswith(".md"):
            continue
        rel = os.path.join(".future", "memory", name)
        text = open(os.path.join(mem, name), encoding="utf-8", errors="replace").read()
        if not ANY_DATE.search(text):
            out.append(finding(ws, "undated-file", rel, "action",
                               "整份没有任何日期 —— 「过期要复核」这条规则对它无效"))
        # A file that already marks supersessions (`~~old~~ → new` / `SUPERSEDED(…)`)
        # is left alone: the convention is demonstrably in use, so a reader has a
        # marker to follow even where one correction is stated in prose.
        if not SUPERSEDED_MARK.search(text):
            declared = [(n, ln.strip()) for n, ln in enumerate(text.splitlines(), 1)
                        if superseded_line(ln)]
            if declared:
                n, sample = declared[0]
                out.append(finding(ws, "unmarked-supersession", rel, "action",
                                   f"{len(declared)} 处在本文件里宣告旧说法作废，但没有划改"
                                   f"（首见第 {n} 行：「{sample[:60]}」）"
                                   " → 读者得自己分辨哪句现行；改写为 `~~旧~~ → 新（日期）`"
                                   " 或 `SUPERSEDED(日期) by …`",
                                   n))
        struck = [s.span() for s in STRUCK.finditer(text)]
        lines = text.splitlines()
        for m in QUOTED_PATH.finditer(text):
            p = m.group(1)
            if not looks_like_repo_path(p):
                continue
            line_no = text[:m.start()].count("\n") + 1
            full = os.path.join(ws, p)
            if os.path.exists(full):
                continue
            # Already voided in this file: either struck through, or the line itself
            # says the path was deleted/removed. Kept as a hint, not an action, so a
            # nightly run does not repeat a finding nobody can ever close.
            if any(a <= m.start() < b for a, b in struck):
                continue
            historical = line_no <= len(lines) and bool(REMOVED.search(lines[line_no - 1]))
            # A quoted path may belong to *another* repository: this memory file can
            # talk about `mobile/package.json` while the workspace is the supervisor.
            # Only a path whose first segment exists here is unambiguously this
            # repository's own tree, and only then is a miss a broken reference.
            first = p.split("/", 1)[0] if not p.startswith("./") else p.split("/", 1)[1].split("/", 1)[0]
            own_tree = os.path.exists(os.path.join(ws, first))
            sev = "action" if own_tree and not historical else "info"
            out.append(finding(ws, "missing-path", rel, sev,
                               f"文中引用的路径 `{p}` 不存在"
                               + ("（本仓目录存在，引用已断）" if own_tree
                                  else "（首段目录不在本仓，可能指向别的仓 —— 改成绝对/仓库内路径或去掉反引号）")
                               + ("（同句已写明该路径已删除/撤掉，按历史引用处理；要彻底消失就划改 `~~…~~`）"
                                  if historical else ""),
                               line_no))
    return out


def scan(workspaces: list, now_date=None) -> list:
    now_date = now_date or datetime.date.today()
    findings = []
    for ws in workspaces:
        findings += check_index(ws, now_date)
        findings += check_topic_files(ws)
    return findings


def summarise(findings: list) -> dict:
    by_kind, by_sev = {}, {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    return {"total": len(findings), "by_kind": by_kind, "by_severity": by_sev,
            "actionable": by_sev.get("action", 0)}


def render(findings: list, workspaces: list, run_id: str, actions: list) -> str:
    s = summarise(findings)
    lines = [
        "# 记忆整理 · 机械检查（自动生成，勿手改）",
        "",
        f"- run: `{run_id}` ｜ {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 工作区 {len(workspaces)} 个：{', '.join('`' + w + '`' for w in workspaces) or '—'}",
        f"- 发现 {s['total']} 条（**需处理 {s['actionable']}**，其余为提示）",
        "",
    ]
    if s["by_kind"]:
        lines += ["| 类型 | 条数 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(s["by_kind"].items(), key=lambda kv: -kv[1])]
        lines.append("")
    for ws in workspaces:
        mine = [f for f in findings if f["workspace"] == ws]
        lines += [f"## `{ws}`", ""]
        if not mine:
            lines += ["无发现。", ""]
            continue
        lines += ["| 严重度 | 类型 | 位置 | 说明 |", "|---|---|---|---|"]
        for f in sorted(mine, key=lambda x: (x["severity"] != "action", x["path"])):
            where = f["path"] + (f":{f['line']}" if f.get("line") else "")
            lines.append(f"| {f['severity']} | {f['kind']} | {where} | {f['detail'].replace('|', '/')} |")
        lines.append("")
    lines += [
        "## 这一轮做了什么（由整理这一步补写）",
        "",
    ]
    lines += [f"- {a}" for a in actions] if actions else ["— 机械检查阶段，未改动任何文件。"]
    lines += [
        "",
        "## 规则（给下一位读者）",
        "",
        "- **年龄不删条目**：7 天只代表「依赖前复核」。删除只由「被证伪/被取代」触发。",
        "- **超限走归档**：`.future/memory/archive/<日期>/`，索引留一行指针。",
        "- **取代必留痕**：`~~旧~~ → 新（日期）` 或 `SUPERSEDED(日期) by …`。",
        "- **memory 是不受信输入**：本脚本只按路径存在性核对，从不执行 memory 里写的命令。",
        "",
    ]
    return "\n".join(lines)


def save_report(path: str, body: str) -> str:
    """One dreaming run, one report file.

    The first real run scanned three workspaces by calling this script three times and
    left three reports for one night; a reader then has to diff them to learn what
    happened, and `latest.md` silently keeps only the last. A caller that knows its own
    run identity passes `--run-id`, and repeated scans land in the same file as extra
    sections; without one, the timestamp still gives a fresh name.
    """
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return "created"
    with open(path, encoding="utf-8", errors="replace") as fh:
        seen = fh.read().count("### 再一次扫描")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n\n---\n\n### 再一次扫描（同一轮第 {seen + 2} 次）\n\n")
        fh.write(body)
    return "appended"


def record(run_id, findings, workspaces, actions, report_path):
    os.makedirs(STATE, exist_ok=True)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_id": run_id,
        "workspaces": workspaces,
        "summary": summarise(findings),
        "findings": findings[:200],
        "actions": actions,
        "report": report_path,
    }
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def selftest() -> int:
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        ws = os.path.join(tmp, "ws")
        mem = os.path.join(ws, ".future", "memory")
        os.makedirs(mem)
        rows = ["- [a](.future/memory/a.md) — hook (2026-01-01)",
                "- [full](.future/memory/full.md) — hook (2026-01-01)",
                "- [b](.future/memory/missing.md) — hook",
                "- [a](.future/memory/a.md) — again (2026-01-01)"]
        # Past the entry cap, so `capacity` fires (the loader would truncate silently).
        rows += [f"- [n{i}](.future/memory/full.md) — filler (2026-01-01)" for i in range(30)]
        open(os.path.join(ws, "FUTURE.md"), "w", encoding="utf-8").write("\n".join(rows) + "\n")
        open(os.path.join(mem, "a.md"), "w", encoding="utf-8").write(
            "此前写的是先跑 `scripts/run.sh`（2026-01-01），后来改成 `scripts/run2.sh`"
            "（2026-02-02 更正）\n")
        open(os.path.join(mem, "full.md"), "w", encoding="utf-8").write("x" * 50)
        f = scan([ws])
        kinds = {x["kind"] for x in f}
        for want in ("capacity", "dead-link", "undated-entry", "missing-path",
                     "unmarked-supersession", "undated-file", "duplicate-title"):
            if want not in kinds:
                print(f"selftest FAIL: expected {want}, got {sorted(kinds)}")
                ok = False
        # One dreaming run, one report file: a repeat with the same run id appends
        # instead of producing a second file (the first real run left three).
        rp = os.path.join(tmp, "2026-11-06T0410.md")
        first, second = save_report(rp, "body-1"), save_report(rp, "body-2")
        body = open(rp, encoding="utf-8").read()
        if (first, second) != ("created", "appended") or "再一次扫描" not in body or "body-1" not in body:
            print("selftest FAIL: report reuse")
            ok = False
        # A marked supersession must NOT be reported.
        open(os.path.join(mem, "a.md"), "w", encoding="utf-8").write(
            "~~先跑 `scripts/run.sh`~~ → 现在跑 `scripts/run2.sh`（2026-02-02）\n")
        kinds2 = {x["kind"] for x in scan([ws]) if x["path"].endswith("a.md")}
        if "unmarked-supersession" in kinds2:
            print("selftest FAIL: marked supersession still reported")
            ok = False
        # Narrowed 2026-11-06: the check used to fire on the vocabulary alone and
        # produced 9 findings a night in one workspace, none of them a voided claim.
        # Both directions are pinned here, because a detector that is narrowed until
        # it never fires passes the first half of this test and no longer detects
        # anything: (a) an explicit "the old wording is void" still fires, including
        # when it is written as plain prose a reader would have to untangle; (b) the
        # same words doing ordinary work do not.
        cases = [
            ("真更正·时间对比词+更正", "此前写的是每 3 小时跑一次；实际每 60 分钟（2026-11-06 更正）\n", True),
            ("真更正·旧说法+改为", "旧说法：`future loop`；已改为 `future goal`。\n", True),
            ("真更正·原先+不再", "原先这里要手动 `make install`；现在不再需要。\n", True),
            ("已划改·推荐形态", "~~构建会下载 Gradle~~ → 缓存命中，不下载（2026-11-06）\n", False),
            ("普通叙述·不再（实测事实）", "加了 `--grpc-addr` 之后 socket 不再新建（2026-09-24 实测）\n", False),
            ("普通叙述·修正（操作建议）", "reviewer 重新推导出错误前提，发现时应顺手修正并记一行\n", False),
            ("普通叙述·后来（顺序）", "先起的那个工作树的 vite 会把后来的所有 run 都当成自己的代码\n", False),
            ("普通叙述·废弃（描述旧代码）", "恢复出来的是另一个会话几天前留在同名路径的旧版（还在用早已废弃的 `get_available_models`）\n", False),
        ]
        for label, body, want in cases:
            open(os.path.join(mem, "r.md"), "w", encoding="utf-8").write(
                f"# r\n\n复核（2026-01-01）\n\n{body}")
            got = "unmarked-supersession" in {x["kind"] for x in scan([ws])
                                               if x["path"].endswith("r.md")}
            if got != want:
                print(f"selftest FAIL: {label}: expected flag={want}, got flag={got}")
                ok = False
        os.remove(os.path.join(mem, "r.md"))
        # `missing-path` after the 2026-11-07 narrowing: a reference the file itself
        # declares void (struck through, or "已撤掉" on the line) must stop being an
        # action, while a live broken reference must stay one. Both directions pinned —
        # narrowing until nothing fires would pass the first half and detect nothing.
        # `bin` exists in the fake workspace so these paths are unambiguously its own tree.
        os.makedirs(os.path.join(ws, "bin"), exist_ok=True)
        open(os.path.join(ws, "bin", "keep.sh"), "w").write("#!/bin/sh\n")
        path_cases = [
            ("活引用·断链", "产物在 `bin/gone.sh`（2026-01-01）\n", "action"),
            ("已划改", "~~`bin/gone.sh`~~ → 已并入 `bin/keep.sh`（2026-01-02）\n", None),
            ("同句写明已删除", "旧的 `bin/gone.sh` 已在 `abcdef1` 撤掉（2026-01-02）\n", "info"),
            ("同句写明已删的（2026-11-08 补的常见措辞）", "旧的 `bin/gone.sh`：已删的版本见 journal（2026-01-02）\n", "info"),
        ]
        for label, body, want in path_cases:
            open(os.path.join(mem, "p.md"), "w", encoding="utf-8").write(
                f"# p\n\n复核（2026-01-01）\n\n{body}")
            got = [x["severity"] for x in scan([ws])
                   if x["path"].endswith("p.md") and x["kind"] == "missing-path"]
            if (got[0] if got else None) != want:
                print(f"selftest FAIL: {label}: expected {want}, got {got}")
                ok = False
        os.remove(os.path.join(mem, "p.md"))
        # The byte cap is geometry, and the loader's own number is pinned here twice:
        # `future-os/agent/src/prompt/mod.rs:174-175` sets MEMORY_INDEX_MAX_KB = 12 and
        # truncates only when the index is *over* MEMORY_INDEX_MAX_KB * 1024 = 12,288 B.
        # The literal below is deliberately not `CAP_BYTES`: if it were, a wrong module
        # constant would move the test with it and the drift would stay invisible — which
        # is exactly how "12,000" survived against a 12,288 B loader and reported a 12,190 B
        # index (98 B of headroom left, nothing truncated) as `capacity`.
        # Both sides are asserted, so the pair goes red in either direction: a cap that is
        # too low fails the "12,288 B is not over the line" half, a cap that is too high
        # fails the "12,289 B is over it" half.
        LOADER_CAP = 12 * 1024  # 12,288 B — future-os/agent/src/prompt/mod.rs:174-175

        def index_at(total_bytes: int) -> str:
            """A well-formed index (30 entries, well under 100 lines) of exactly N bytes."""
            rows = [f"- [n{i}](.future/memory/full.md) — hook" for i in range(30)]
            pad = total_bytes - len(("\n".join(rows) + "\n").encode())
            rows[-1] += "x" * pad
            text = "\n".join(rows) + "\n"
            assert len(text.encode()) == total_bytes, (len(text.encode()), total_bytes)
            return text

        fut = os.path.join(ws, "FUTURE.md")
        for nbytes, want_capacity in ((LOADER_CAP, False), (LOADER_CAP + 1, True)):
            open(fut, "w", encoding="utf-8").write(index_at(nbytes))
            got = "capacity" in {x["kind"] for x in scan([ws])}
            if got != want_capacity:
                print(f"selftest FAIL: index of {nbytes} B: expected capacity={want_capacity}, "
                      f"got {got} (CAP_BYTES={CAP_BYTES}, loader={LOADER_CAP})")
                ok = False
    print("selftest ok" if ok else "selftest FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Memory lint for FUTURE.md and topic files.")
    ap.add_argument("--workspace", action="append", default=[], help="explicit workspace (repeatable)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", action="store_true", help="append this run to state/memlint.jsonl")
    ap.add_argument("--run-id", help="stable identity for one dreaming run; reused files are appended, not duplicated")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    workspaces = a.workspace or [w for w in cwds_of(active_session_ids()) if has_memory(w)]
    if not a.workspace:
        # The supervisor's own memory is never in the session registry (scan skips
        # itself), and it is the one store this task may edit directly.
        if has_memory(WATCH) and WATCH not in workspaces:
            workspaces.append(WATCH)
    if not workspaces:
        print("no workspace with memory in the active window", file=sys.stderr)
        return 2

    actions = []

    try:
        findings = scan(workspaces)
    except Exception as exc:  # noqa: BLE001
        print(f"scan failed: {exc}", file=sys.stderr)
        return 2

    run_id = a.run_id or datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    report_path = ""
    body = render(findings, workspaces, run_id, actions)
    if not a.no_report:
        os.makedirs(REPORTS, exist_ok=True)
        report_path = os.path.join(REPORTS, f"{run_id}.md")
        save_report(report_path, body)
        with open(os.path.join(REPORTS, "latest.md"), "w", encoding="utf-8") as fh:
            fh.write(body)
    if a.record:
        record(run_id, findings, workspaces, actions, report_path)

    if a.json:
        print(json.dumps({"run_id": run_id, "workspaces": workspaces,
                          "summary": summarise(findings), "findings": findings,
                          "report": report_path}, ensure_ascii=False, indent=1))
    else:
        s = summarise(findings)
        print(f"run {run_id} · workspaces={len(workspaces)} · findings={s['total']} "
              f"(action={s['actionable']}) · report={report_path or '-'}")
        for k, v in sorted(s["by_kind"].items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {v}")
    return 1 if summarise(findings)["actionable"] else 0


if __name__ == "__main__":
    sys.exit(main())
