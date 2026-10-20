#!/usr/bin/env python3
"""Self-test for the inbox fold rule (one live event per session + kind).

Run: python3 bin/selftest_inbox.py   (wired into bin/selfcheck.py / CI)

Why this is a safety rule and not cosmetics: the event ids on the card are the
**ack handles** (`bin/inbox.py ack <id>`), so an event that stops appearing on the
card but stays in `inbox/pending.jsonl` is never retired — it keeps counting in
`pendingCount`, keeps riding the heartbeat, and comes back on every later card as
if it were news. Folding is therefore only allowed together with *recording*: the
folded event must land in `inbox/handled.jsonl` with `foldedInto` naming its
survivor.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "bin", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inbox = load("inbox")


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


def ev(eid, kind, sid, ms, prio="low"):
    return {"id": eid, "kind": kind, "sessionId": sid, "detectedAtMs": ms, "priority": prio}


print("one survivor per (session, kind)")
rows = [ev("e1", "awaiting_user", "s1", 1000),
        ev("e2", "awaiting_user", "s1", 2000),
        ev("e3", "awaiting_user", "s1", 3000)]
keep, folded = inbox.fold_superseded(rows)
check("the newest of three survives", [e["id"] for e in keep], ["e3"])
check("the other two are folded", [e["id"] for e in folded], ["e1", "e2"])
check("folded events point at their survivor", {e["foldedInto"] for e in folded}, {"e3"})
check("folding is recorded, not dropped",
      all("foldedReason" in e and "handledAtMs" in e for e in folded), True)

print("sessions and kinds stay apart")
rows = [ev("a1", "awaiting_user", "s1", 1000),
        ev("a2", "awaiting_user", "s2", 2000),
        ev("a3", "new_session", "s1", 3000),
        ev("a4", "approval_pending", "s1", 4000, "high")]
keep, folded = inbox.fold_superseded(rows)
check("nothing folds when every (session, kind) is unique",
      [e["id"] for e in keep], ["a1", "a2", "a3", "a4"])
check("no folds recorded", folded, [])

print("run-level facts are never folded")
rows = [ev("r1", "run_error", "s1", 1000, "high"),
        ev("r2", "run_error", "s1", 2000, "high"),
        ev("f1", "failed_run", "s1", 3000, "high"),
        ev("f2", "failed_run", "s1", 4000, "high")]
keep, _ = inbox.fold_superseded(rows)
check("two failures stay two facts", [e["id"] for e in keep], ["r1", "r2", "f1", "f2"])

print("the survivor is chosen by time, not by position")
rows = [ev("o1", "awaiting_user", "s1", 9000), ev("o2", "awaiting_user", "s1", 100)]
keep, folded = inbox.fold_superseded(rows)
check("the later event survives wherever it sits", [e["id"] for e in keep], ["o1"])
check("the earlier one is folded", [e["id"] for e in folded], ["o2"])

print("no kinds = no folding (the config off-switch)")
rows = [ev("k1", "awaiting_user", "s1", 1000), ev("k2", "awaiting_user", "s1", 2000)]
keep, folded = inbox.fold_superseded(rows, kinds=())
check("everything is kept", [e["id"] for e in keep], ["k1", "k2"])
check("nothing folded", folded, [])

print("apply_fold writes both sides, and is idempotent")
d = tempfile.mkdtemp(prefix="selftest-inbox-")
p = os.path.join(d, "pending.jsonl")
h = os.path.join(d, "handled.jsonl")
with open(p, "w", encoding="utf-8") as fh:
    for e in [ev("p1", "awaiting_user", "s1", 1000), ev("p2", "awaiting_user", "s1", 2000),
              ev("p3", "awaiting_user", "s2", 3000)]:
        fh.write(json.dumps(e) + "\n")
keep, folded = inbox.apply_fold(pending_path=p, handled_path=h, now_ms=42)
check("one live event per session", [e["id"] for e in keep], ["p2", "p3"])
check("the folded event left pending.jsonl",
      [json.loads(ln)["id"] for ln in open(p, encoding="utf-8") if ln.strip()], ["p2", "p3"])
check("the folded event landed in handled.jsonl",
      [json.loads(ln)["id"] for ln in open(h, encoding="utf-8") if ln.strip()], ["p1"])
check("handledAtMs is the caller's clock",
      json.loads(open(h, encoding="utf-8").readline())["handledAtMs"], 42)
keep2, folded2 = inbox.apply_fold(pending_path=p, handled_path=h)
check("a second fold has nothing to do", folded2, [])
check("pending is unchanged by it", [e["id"] for e in keep2], ["p2", "p3"])
check("handled.jsonl was not appended again",
      len(open(h, encoding="utf-8").read().strip().splitlines()), 1)
shutil.rmtree(d, ignore_errors=True)

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}):")
    for f in FAILED:
        print("  " + f)
    sys.exit(1)
print("selftest_inbox: all checks passed")
