#!/usr/bin/env python3
"""Selftest for taskruns.py — the scheduled-task reflection trigger.

Runs on synthetic ledgers (no `future` calls, no repo state touched): the point is
that a run becomes an event exactly once, that a task's first sighting is a
baseline rather than a flood of history, and that a review is *asked for* after N
runs without anyone having to remember it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load():
    spec = importlib.util.spec_from_file_location("taskruns", ROOT / "bin" / "taskruns.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE_PATH = pathlib.Path(tempfile.mkdtemp(prefix="taskruns-")) / "state.json"
    mod.DIGEST_DIR = mod.STATE_PATH.parent / "digests"
    return mod


T = {"id": "tsk_demo", "name": "演示任务", "enabled": True,
     "trigger": "every 60m", "sessionRetention": "delete"}


def run(rid, status="completed", text="done", ver=1, err=None):
    return {"id": rid, "status": status, "resultSummary": text, "promptVersion": ver,
            "errorMessage": err, "startedAt": 1_800_000_000_000, "finishedAt": 1_800_000_060_000}


def feed(mod, runs, state, now_ms, cfg=None):
    """`collect` with the CLI replaced by a fixed ledger."""
    mod.fetch_tasks = lambda _cfg: ([T], "")
    mod.fetch_runs = lambda _cfg, _tid, limit=20: list(reversed(runs))  # newest first
    return mod.collect(cfg or {"task_review_every_runs": 3, "task_poll_seconds": 0},
                       state=state, now_ms=now_ms, force=True, fetch=True)


def main() -> int:
    mod = load()
    # 1. first sight is a baseline: history produces no events and no review debt
    st = {}
    ev, st = feed(mod, [run("r1"), run("r2", "failed", err="502")], st, 1_000)
    check("first sight: no events for history", ev == [], f"{ev}")
    check("first sight: review counter starts at zero",
          st["tasks"]["tsk_demo"]["runsSinceReview"] == 0)
    check("first sight: history marked seen",
          set(st["tasks"]["tsk_demo"]["seenRunIds"]) == {"r1", "r2"})

    # 2. a new failing run is one high event, keyed by run id
    ev, st = feed(mod, [run("r1"), run("r2", "failed", "502-upstream", err="502")], st, 2_000)
    ev, st = feed(mod, [run("r1"), run("r2", "failed", "502-upstream", err="502"), run("r3", "failed", "502-down", err="502")], st, 3_000)
    check("new failure -> one task_run_failed",
          [e["kind"] for e in ev] == ["task_run_failed"], f"{[e['kind'] for e in ev]}")
    check("failure carries the run id and error",
          ev and ev[0]["detail"].get("runId") == "r3" and "502" in ev[0]["detail"].get("error", ""))
    check("failure event is high priority", ev and ev[0]["priority"] == "high")

    # 3. the same run polled again is not a second event
    ev2, st = feed(mod, [run("r1"), run("r2", "failed"), run("r3", "failed")], st, 4_000)
    check("re-poll of seen runs emits nothing", ev2 == [], f"{[e['kind'] for e in ev2]}")

    # 4. completed-but-silent run is an event of its own
    ev3, st = feed(mod, [run("r1"), run("r2"), run("r3"), run("r4", "completed", "")], st, 5_000)
    check("empty result -> task_run_empty",
          any(e["kind"] == "task_run_empty" for e in ev3), f"{[e['kind'] for e in ev3]}")

    # 5. a cancellation is the user's doing, not news
    ev4, st = feed(mod, [run("r1"), run("r5", "cancelled")], st, 6_000)
    check("cancelled run is not an anomaly event",
          [e for e in ev4 if e["kind"].startswith("task_run")] == [],
          f"{[e['kind'] for e in ev4]}")

    # 6. the review is asked for by itself after `every` runs, exactly once
    st2 = {}
    mod.fetch_tasks = lambda _cfg: ([T], "")
    ledger = [run("a1")]
    mod.fetch_runs = lambda _cfg, _tid, limit=20: list(reversed(ledger))
    mod.collect({"task_review_every_runs": 2, "task_poll_seconds": 0}, state=st2,
                now_ms=10_000, force=True, fetch=True)  # baseline
    for i in range(2, 5):
        ledger.append(run(f"a{i}"))
        ev, st2 = mod.collect({"task_review_every_runs": 2, "task_poll_seconds": 0},
                              state=st2, now_ms=10_000 + i * 1000, force=True, fetch=True)
        ev = [e for e in ev if e["kind"] == "task_review"]
        if i == 3:
            check("review fires once the threshold is reached", len(ev) == 1, f"{ev}")
            check("review names the run count and the task",
                  ev and "2 次运行未复盘" in ev[0]["summary"], f"{ev and ev[0]['summary']}")
        if i == 4:
            check("review is not re-asked every poll", ev == [], f"{[e['summary'] for e in ev]}")
    check("reviewPending is set until reviewed", st2["tasks"]["tsk_demo"]["reviewPending"])

    # 7. reviewing closes the loop and lets a later batch ask again
    mod.mark_reviewed("tsk_demo", state=st2, now_ms=20_000)
    check("reviewed resets the counter", st2["tasks"]["tsk_demo"]["runsSinceReview"] == 0)
    check("reviewed clears pending", st2["tasks"]["tsk_demo"]["reviewPending"] is False)
    for i in range(5, 8):
        ledger.append(run(f"a{i}"))
        ev, st2 = mod.collect({"task_review_every_runs": 2, "task_poll_seconds": 0},
                              state=st2, now_ms=20_000 + i * 1000, force=True, fetch=True)
        ev = [e for e in ev if e["kind"] == "task_review"]
        if i == 6:
            check("next batch asks again", len(ev) == 1, f"{ev}")

    # 8. two consecutive failures ask for a review even below the run threshold
    st3 = {}
    led3 = [run("b1")]
    mod.fetch_runs = lambda _cfg, _tid, limit=20: list(reversed(led3))
    mod.collect({"task_review_every_runs": 6, "task_poll_seconds": 0}, state=st3,
                now_ms=30_000, force=True, fetch=True)
    got = []
    for i, status in ((2, "failed"), (3, "failed")):
        led3.append(run(f"b{i}", status, "", err="502"))
        ev, st3 = mod.collect({"task_review_every_runs": 6, "task_poll_seconds": 0},
                              state=st3, now_ms=30_000 + i * 1000, force=True, fetch=True)
        got += [e["kind"] for e in ev]
    check("two failures in a row -> review", "task_review" in got, f"{got}")

    # 9. digest: one file to read instead of N runs, with the repetition analysis
    import json as _json
    mod.future = lambda _cfg, _args, timeout=30: (
        0, _json.dumps({"task": dict(T, prompt="x" * 100, promptVersion=7)}), "")
    mod.fetch_runs = lambda _cfg, _tid, limit=20: [
        run("d1", text="本轮结论：A 已完成\n仍不确定：X 这条链路在真机上没有验证过\n跳过: fix/foo 分支有未落地内容"),
        run("d2", text="本轮结论：A 已完成\n仍不确定：X 这条链路在真机上没有验证过\n跳过: fix/foo 分支有未落地内容"),
        run("d3", text="本轮结论：A 已完成\n仍不确定：X 这条链路在真机上没有验证过\n跳过: fix/foo 分支有未落地内容")]
    path = mod.digest("tsk_demo", {"task_review_every_runs": 3}, runs_n=3)
    body = pathlib.Path(path).read_text(encoding="utf-8")
    check("digest is written to the state dir", pathlib.Path(path).exists() and "Digest" not in body)
    check("digest states the prompt version and size", "v7" in body and "100 字符" in body)
    check("digest counts the repeated uncertain line", "仍不确定" in body and "×3" in body)
    check("digest ends with the three review questions", "复盘要回答的三件事" in body)
    rep = mod.repeated_lines(["alpha beta gamma delta\none two three four five",
                              "alpha beta gamma delta\nsix seven eight nine ten",
                              "alpha beta gamma delta\n" + "x" * 30])
    check("repeated lines are counted across runs",
          any("alpha beta gamma delta" in ln for _, ln in rep), f"{rep}")

    # 10. ids are stable, so a lost state file cannot duplicate an event
    check("event ids are deterministic",
          mod.event_id("task_run_failed", "trn_abc-1") == mod.event_id("task_run_failed", "trn_abc-1"))
    check("event ids differ per run",
          mod.event_id("task_run_failed", "trn_a") != mod.event_id("task_run_failed", "trn_b"))

    # 11. the wiring: events only matter if the scanner actually asks for them and
    # puts them in the same list it writes to inbox/pending.jsonl, and if the wake
    # gate treats a medium event as notify-worthy (it does: NOTIFY_PRIORITY).
    scan_src = (ROOT / "bin" / "scan.py").read_text(encoding="utf-8")
    check("scan polls the ledger", "taskruns.collect(" in scan_src)
    check("scan appends those events", "for ev in task_events" in scan_src and "events.append(ev)" in scan_src)
    check("scan reports them in health", "\"taskEvents\"" in scan_src)
    decide_src = (ROOT / "bin" / "decide.py").read_text(encoding="utf-8")
    check("a medium task event can wake a turn", '"medium"' in decide_src.split("NOTIFY_PRIORITY", 1)[1][:80])

    print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
