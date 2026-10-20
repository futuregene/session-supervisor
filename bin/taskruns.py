#!/usr/bin/env python3
"""Watch scheduled-task runs, and make a review of them happen instead of a memory.

Why this exists (user, 2026-10-21: 「现在会话总管对那些已经执行完，并且执行完就
删除会话的定时任务，是不是没办法拿到结果进行反思」):

Most of this deployment's tasks run as `session retention = delete` — the run's
conversation is thrown away the moment it settles, so `bin/scan.py` (which reads
`future session list`) can never see them, and no event is ever produced for them.
Measured that day: 8 of 13 tasks delete their session, 9 of the 13 每小时 dev-PR
runs were already gone, and in 111 recorded runs `feedback` was **NULL in every
row** — the reflective hook existed and had never been used.

What survives is the task ledger (`task_runs`: status, error, prompt version, the
run's answer), so reflection is possible — it just has to be *triggered*. This
module turns that ledger into events, which the existing wake path already knows
how to act on:

  task_run_failed   high    a new run ended in a non-success state
  task_run_empty    medium  a run "completed" without producing any answer
                            (measured twice on the hourly task at prompt v12/v13:
                            a silent no-op nobody noticed)
  task_review       medium  enough new runs have piled up since the last review
                            (default 6, i.e. ~6 h for an hourly task), or two
                            consecutive failures

`task_review` is the actual answer to the user's question: it fires on its own
after every N runs, so optimisation does not depend on the supervisor remembering
to look. `digest` then writes one compact file to read instead of N runs.

No model calls here; everything is the ledger plus a few `future task` reads.

  taskruns.py collect [--json] [--force]      # scan entry point: new events + state
  taskruns.py list [--json]                   # tasks with their run counters
  taskruns.py digest <task> [--runs N] [--out F] [--print]
  taskruns.py reviewed <task>                 # close the loop: reset the counter
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import tool_path, config_path  # noqa: E402

CFG_PATH = config_path()
STATE_PATH = os.path.join(WATCH, "state", "taskruns.json")
DIGEST_DIR = os.path.join(WATCH, "state", "task-digests")

# `completed` is the only success spelling the ledger uses; everything else is worth
# an event except a user cancellation, which is the user's own doing (the scan does not
# report cancelled runs either, see `REAL_FAILURES` in bin/scan.py).
FAIL_STATUSES = ("failed", "error", "aborted", "timeout", "crashed")
QUIET_STATUSES = ("cancelled",)

# Lines a run writes about itself that are *not* a result: what it could not verify,
# what it skipped, what it wants from the user. They are the raw material of a prompt
# fix, so `digest` counts them across runs rather than printing them once.
UNCERTAIN = re.compile(r"不确定|未确认|未验证|没能在|没能验|仍不|尚未|待办|需要你|需你决定|留给下一轮")
RISK = re.compile(r"跳过|风险|未动|没动|未落地|残留|没有判断")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def future(cfg, args, timeout=30):
    exe = cfg.get("future_bin") or "future"
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run([exe] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def fetch_tasks(cfg):
    rc, out, err = future(cfg, ["task", "list", "--json"])
    if rc != 0:
        return [], f"task list rc={rc}: {err.strip()[:160]}"
    try:
        rows = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        return [], f"task list parse: {exc}"
    if isinstance(rows, dict):
        rows = rows.get("tasks") or []
    return rows, ""


def fetch_runs(cfg, task_id, limit=20):
    rc, out, _ = future(cfg, ["task", "runs", task_id, "--limit", str(limit), "--json"])
    if rc != 0:
        return []
    try:
        rows = json.loads(out)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(rows, dict):
        rows = rows.get("runs") or []
    return rows  # newest first


def watched(cfg, tasks):
    """Which tasks to watch: never the disabled ones, then the allow-list if any.

    A disabled task cannot produce runs, so polling it is pure cost. An empty
    `task_watch` means "all enabled tasks" — the user asked about the phenomenon, not
    about one task, and a task added tomorrow should not need a config edit.
    """
    allow = set(cfg.get("task_watch") or [])
    out = []
    for t in tasks:
        if not t.get("enabled"):
            continue
        if allow and t.get("id") not in allow:
            continue
        out.append(t)
    return out


def run_text(run):
    return (run.get("resultSummary") or run.get("resultText") or "").strip()


def anomalies(run):
    """Events one finished run is entitled to, independent of any threshold."""
    status = (run.get("status") or "").lower()
    out = []
    if status in FAIL_STATUSES:
        out.append(("task_run_failed", "high",
                    f"定时任务运行失败：{status}",
                    {"runId": run.get("id"), "status": status,
                     "error": (run.get("errorMessage") or "")[:400]}))
    elif status in QUIET_STATUSES:
        pass  # the user stopped it; not news
    elif status == "completed" and not run_text(run):
        out.append(("task_run_empty", "medium",
                    "定时任务“完成”了但没有任何输出",
                    {"runId": run.get("id"), "promptVersion": run.get("promptVersion")}))
    return out


def event_id(kind, key):
    # Deterministic, so a re-poll cannot produce a second event for the same fact
    # even if the state file were lost: the inbox dedups on id as well.
    return f"evt_{kind}_{re.sub(r'[^A-Za-z0-9]+', '', str(key))[:40]}"


def collect(cfg=None, state=None, now_ms=None, force=False, fetch=True):
    """Poll the watched tasks; return (events, state). Pure unless `fetch` is False.

    Idempotent by run id: a run already in `seenRunIds` never produces a second
    event, so the 30-second scan cadence and a manual `collect --force` agree.
    The **first** sight of a task is a baseline: its history is marked seen without
    producing events, or the first scan after this ships would fire one event per
    historical failure (two 502s and a zero-result run were sitting in the ledger).
    """
    cfg = cfg if cfg is not None else load_json(CFG_PATH, {})
    st = state if state is not None else load_json(STATE_PATH, {})
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    st.setdefault("tasks", {})
    poll_s = float(cfg.get("task_poll_seconds", 120))
    if (not force) and st.get("lastPollMs") and (now_ms - int(st["lastPollMs"])) < poll_s * 1000:
        return [], st
    every = int(cfg.get("task_review_every_runs", 6))
    max_hours = float(cfg.get("task_review_max_hours", 24))
    cap = int(cfg.get("task_run_seen_cap", 40))
    events = []
    if not fetch:
        return events, st
    tasks, why = fetch_tasks(cfg)
    if why:
        st["lastError"] = why
        st["lastPollMs"] = now_ms
        return events, st
    st.pop("lastError", None)
    for t in watched(cfg, tasks):
        tid = t.get("id")
        rec = st["tasks"].get(tid)
        fresh_task = rec is None
        if fresh_task:
            rec = st["tasks"][tid] = {
                "name": t.get("name"), "seenRunIds": [], "runsSinceReview": 0,
                "lastReviewMs": now_ms, "firstSeenMs": now_ms,
                "failureStreak": 0, "reviewPending": False, "reviewEmittedRuns": 0,
            }
        rec["name"] = t.get("name")
        rec["trigger"] = t.get("trigger")
        runs = fetch_runs(cfg, tid, limit=max(every * 3, 20))
        seen = set(rec.get("seenRunIds") or [])
        unseen = [r for r in runs if r.get("id") and r["id"] not in seen]
        for r in reversed(unseen):  # oldest first, so counters accumulate in order
            seen.add(r["id"])
            if fresh_task:
                # Baseline: the task's whole history is marked seen, and none of it
                # counts toward a review — otherwise the first scan after this ships
                # would ask to review 17 already-settled runs at once.
                continue
            rec["runsSinceReview"] = int(rec.get("runsSinceReview") or 0) + 1
            status = (r.get("status") or "").lower()
            if status in FAIL_STATUSES:
                rec["failureStreak"] = int(rec.get("failureStreak") or 0) + 1
            else:
                rec["failureStreak"] = 0
            ver = r.get("promptVersion")
            if ver and ver != rec.get("lastPromptVersion"):
                rec["promptVersionChangedAtMs"] = now_ms
            rec["lastPromptVersion"] = ver
            rec["lastRunId"] = r.get("id")
            rec["lastStartedAtMs"] = r.get("startedAt")
            for kind, prio, summary, detail in anomalies(r):
                events.append({
                    "id": event_id(kind, r.get("id")), "kind": kind, "priority": prio,
                    "sessionId": "", "sessionName": (t.get("name") or "")[:80],
                    "summary": summary, "detail": dict(detail, taskId=tid,
                                                       taskName=t.get("name")),
                    "detectedAtMs": now_ms,
                })
        rec["seenRunIds"] = (list(rec.get("seenRunIds") or []) + [r["id"] for r in unseen])[-cap:]
        if fresh_task:
            st["lastPollMs"] = now_ms
            continue
        runs_since = int(rec.get("runsSinceReview") or 0)
        stale_review = (max_hours > 0 and runs_since > 0
                        and (now_ms - int(rec.get("lastReviewMs") or now_ms)) >= max_hours * 3600_000)
        due = runs_since >= every or int(rec.get("failureStreak") or 0) >= 2 or stale_review
        # One pending review per task; a review that was emitted, acked and then
        # forgotten comes back once another `every` runs have piled up on top of it.
        if due and (not rec.get("reviewPending")
                    or runs_since - int(rec.get("reviewEmittedRuns") or 0) >= every):
            rec["reviewPending"] = True
            rec["reviewEmittedRuns"] = runs_since
            fails = sum(1 for r in runs[:runs_since] if (r.get("status") or "") in FAIL_STATUSES)
            events.append({
                "id": event_id("task_review", f"{tid}_{runs_since}_{int(now_ms // 3600_000)}"),
                "kind": "task_review", "priority": "medium",
                "sessionId": "", "sessionName": (t.get("name") or "")[:80],
                "summary": f"定时任务『{(t.get('name') or '')[:24]}』已有 {runs_since} 次运行未复盘"
                           + (f"（其中 {fails} 次失败）" if fails else ""),
                "detail": {"taskId": tid, "taskName": t.get("name"),
                           "runsSinceReview": runs_since, "failedRuns": fails,
                           "trigger": t.get("trigger"),
                           "digest": os.path.join(DIGEST_DIR, f"{tid}.md")},
                "detectedAtMs": now_ms,
            })
    st["lastPollMs"] = now_ms
    return events, st


def mark_reviewed(task_id, state=None, now_ms=None):
    st = state if state is not None else load_json(STATE_PATH, {})
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rec = (st.setdefault("tasks", {})).setdefault(task_id, {})
    rec.update({"runsSinceReview": 0, "lastReviewMs": now_ms, "reviewPending": False,
                "reviewEmittedRuns": 0, "failureStreak": 0})
    if state is None:
        save_json(STATE_PATH, st)
    return st


# ------------------------------------------------------------------ digest


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if len(ln.strip()) >= 14]


def repeated_lines(bodies, min_runs=3):
    """Lines that show up in most runs — boilerplate, or a standing open item.

    The hourly task writes the same "跳过或风险" paragraph every round; that is
    either something the prompt should carry (so the run stops re-deriving it) or
    something the prompt is making it re-derive forever. Counting them turns a
    review from "read 6 runs" into "look at 3 lines".
    """
    counts = {}
    for body in bodies:
        for ln in set(_lines(body)):
            counts[ln] = counts.get(ln, 0) + 1
    need = min(min_runs, max(2, len(bodies)))
    return sorted(((n, ln) for ln, n in counts.items() if n >= need), reverse=True)


def digest(task_ref, cfg=None, runs_n=None, out=None, do_print=False):
    cfg = cfg if cfg is not None else load_json(CFG_PATH, {})
    st = load_json(STATE_PATH, {})
    tasks, why = fetch_tasks(cfg)
    if why:
        raise SystemExit(why)
    task = next((t for t in tasks
                 if task_ref in (t.get("id"), t.get("name"))), None)
    if not task:
        raise SystemExit(f"no task matching {task_ref!r}")
    tid = task["id"]
    rec = (st.get("tasks") or {}).get(tid) or {}
    n = int(runs_n or max(int(rec.get("runsSinceReview") or 0), 6))
    runs = fetch_runs(cfg, tid, limit=min(max(n, 6), 30))
    rc, out_s, _ = future(cfg, ["task", "show", tid, "--json"])
    prompt, pver = "", None
    if rc == 0:
        try:
            t = json.loads(out_s)
            t = t.get("task", t)
            prompt, pver = t.get("prompt") or "", t.get("promptVersion")
        except Exception:  # noqa: BLE001
            pass
    bodies = [run_text(r) for r in runs]
    L = []
    L.append(f"# 定时任务复盘 · {task.get('name')} ({tid})")
    L.append("")
    L.append(f"- trigger `{task.get('trigger')}` · enabled {task.get('enabled')} · "
             f"session retention `{task.get('sessionRetention')}`")
    L.append(f"- prompt: v{pver} · {len(prompt)} 字符")
    L.append(f"- 台账：本文件覆盖最近 {len(runs)} 次运行；自上次复盘以来 {rec.get('runsSinceReview', '?')} 次"
             f"（上次复盘 {time.strftime('%F %H:%M', time.localtime(int(rec.get('lastReviewMs') or 0) / 1000)) if rec.get('lastReviewMs') else '从未'}）")
    L.append("")
    L.append("> 这是只读摘录。`feedback` 全为 NULL 是实测事实（111 行无一填写），"
             "所以下面的判断只能基于运行自述，不要假设有人验收过。")
    L.append("")
    L.append("## 运行（新→旧）")
    L.append("")
    L.append("| # | started | status | 版本 | 用时 | 输出字数 |")
    L.append("|---|---|---|---|---|---|")
    for i, r in enumerate(runs, 1):
        st_ms, fi_ms = r.get("startedAt") or 0, r.get("finishedAt") or 0
        dur = f"{(fi_ms - st_ms) / 1000:.0f}s" if st_ms and fi_ms else "-"
        L.append(f"| {i} | {time.strftime('%m-%d %H:%M', time.localtime(st_ms / 1000)) if st_ms else '-'} "
                 f"| {(r.get('status') or ''):} | v{r.get('promptVersion')} | {dur} | {len(run_text(r))} |")
    L.append("")
    fails = [r for r in runs if (r.get("status") or "") in FAIL_STATUSES]
    empties = [r for r in runs if (r.get("status") or "") == "completed" and not run_text(r)]
    if fails or empties:
        L.append("## 异常")
        L.append("")
        for r in fails:
            L.append(f"- **失败** {time.strftime('%m-%d %H:%M', time.localtime((r.get('startedAt') or 0) / 1000))}"
                     f" · `{(r.get('errorMessage') or '')[:200]}`")
        for r in empties:
            L.append(f"- **空输出** {time.strftime('%m-%d %H:%M', time.localtime((r.get('startedAt') or 0) / 1000))}"
                     f" · 状态 completed、结果为空（静默空跑）")
        L.append("")
    rep = repeated_lines(bodies)
    if rep:
        L.append("## 每轮都出现的行（→ 该进提示词，或该让它别再重复推导）")
        L.append("")
        for cnt, ln in rep[:12]:
            L.append(f"- ×{cnt} · {ln[:220]}")
        L.append("")
    unc, risk = {}, {}
    for body in bodies:
        for ln in _lines(body):
            if UNCERTAIN.search(ln):
                unc[ln] = unc.get(ln, 0) + 1
            if RISK.search(ln):
                risk[ln] = risk.get(ln, 0) + 1
    for title, bag in (("反复自报的不确定 / 待办（→ 交替失败，或提示词缺一步）", unc),
                       ("反复出现的跳过 / 风险（→ 规则要不要改，或该它在提示词里一次说清）", risk)):
        if not bag:
            continue
        L.append(f"## {title}")
        L.append("")
        for ln, cnt in sorted(bag.items(), key=lambda kv: -kv[1])[:10]:
            L.append(f"- ×{cnt} · {ln[:220]}")
        L.append("")
    L.append("## 各轮自述（截断）")
    L.append("")
    for i, (r, body) in enumerate(zip(runs, bodies), 1):
        L.append(f"### {i} · {(r.get('status') or '')} · v{r.get('promptVersion')}")
        L.append("")
        if not body:
            L.append("_（无输出）_")
        else:
            head, tail = body[:700], body[-500:]
            L.append(head + ("\n…\n" + tail if len(body) > 1200 else ""))
        L.append("")
    L.append("## 复盘要回答的三件事")
    L.append("")
    L.append("1. **这次运行有没有白跑**：空输出、失败、或结论与上一轮完全相同？")
    L.append("2. **重复出现的自述**：上面「每轮都出现的行」「反复不确定」里，哪些应当写进提示词、哪些应当删掉？")
    L.append("3. **要不要改提示词**：要 → `future task show <id> --json | jq -r .prompt > /tmp/t.md`（先取库里当前版本，"
             "绝不用本地旧副本）→ 改 → `future task edit <id> --prompt-file`；不要 → 也只记录结论。")
    L.append("")
    L.append(f"收尾：`python3 bin/taskruns.py reviewed {tid}`，并把结论写进 "
             f"`reports/decisions/$(date +%F).md`（做了什么 / 依据 / 回退 = `future task prompt revert {tid}`）。")
    text = "\n".join(L) + "\n"
    path = out or os.path.join(DIGEST_DIR, f"{tid}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    if do_print:
        print(text)
    else:
        print(path)
    return path


def main(argv):
    cfg = load_json(CFG_PATH, {})
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    as_json = "--json" in rest
    if cmd == "collect":
        events, st = collect(cfg, force="--force" in rest,
                             fetch="--no-fetch" not in rest)
        if "--no-write" not in rest:
            save_json(STATE_PATH, st)
        if as_json:
            print(json.dumps({"events": events, "watch": len(st.get("tasks") or {})},
                             ensure_ascii=False, indent=1))
        else:
            for e in events:
                print(f"{e['id']} [{e['priority']}] {e['kind']} :: {e['summary']}")
            print(f"-- {len(events)} event(s), {len(st.get('tasks') or {})} task(s) watched")
        return 0
    if cmd == "list":
        st = load_json(STATE_PATH, {})
        rows = []
        for tid, rec in (st.get("tasks") or {}).items():
            rows.append({"taskId": tid, "name": rec.get("name"), "trigger": rec.get("trigger"),
                         "runsSinceReview": rec.get("runsSinceReview"),
                         "failureStreak": rec.get("failureStreak"),
                         "reviewPending": bool(rec.get("reviewPending")),
                         "lastRunId": rec.get("lastRunId")})
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"{r['taskId'][:20]} {r['name']} · 未复盘 {r['runsSinceReview']} · "
                      f"连续失败 {r['failureStreak']} · {'待复盘' if r['reviewPending'] else ''}")
            print(f"-- {len(rows)} task(s)")
        return 0
    if cmd == "digest":
        if not rest or rest[0].startswith("-"):
            print(__doc__)
            return 2
        n = None
        out = None
        for i, a in enumerate(rest):
            if a == "--runs" and i + 1 < len(rest):
                n = int(rest[i + 1])
            if a == "--out" and i + 1 < len(rest):
                out = rest[i + 1]
        digest(rest[0], cfg, runs_n=n, out=out, do_print="--print" in rest)
        return 0
    if cmd == "reviewed":
        if not rest:
            print(__doc__)
            return 2
        tasks, why = fetch_tasks(cfg)
        if why:
            print(why)
            return 1
        target = rest[0]
        task = next((t for t in tasks if target in (t.get("id"), t.get("name"))), None)
        tid = task["id"] if task else target
        mark_reviewed(tid)
        print(f"reviewed: {tid} (counter reset)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
