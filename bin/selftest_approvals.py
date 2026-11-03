#!/usr/bin/env python3
"""Self-test for `approvals.py auto`: what it asks, what it decides, how much it spawns.

Why this test exists
--------------------
The cycle used to run `future session approvals <id>` once per session, on a
machine with a few hundred sessions and a 30 s cadence. Almost every session had
nothing pending, so the cycle spawned a few hundred CLI processes — each leaving
its own `~/.future/cli/logs/cli.<pid>.log` pair that is never cleaned up — to
learn nothing. The fix narrows *which* sessions are asked about; it must not
change a single decision rule, and it must not lose a request.

Two things are pinned here:

  * **fan-out** — with an unchanged session list, a cycle spawns one CLI process
    (the `session list`), not one per session; and
  * **nothing is lost** — a real pending payload is found, the deny list still
    escalates instead of approving, the per-cycle budget still stops the loop, and
    the approve argv carries the request id the payload actually spells.

The `future` here is a fake executable on disk (not a patched function), so the
count is of real process spawns. `bin/selftest_send.py` set that precedent.

Usage: python3 bin/selftest_approvals.py   (exit 0 = all checks passed)
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "bin", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ap = load("approvals")


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {name}")


class FakeFuture:
    """A real executable on disk that answers the three call shapes and logs argv.

    `%*` in the Windows launcher does not re-quote the `--note` text, so the note
    may arrive split across arguments; every assertion below reads only the first
    four tokens plus the presence of `--note`. That is deliberate — the test is
    about which sessions we ask and what we decide, not about shell quoting.
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="selftest-approvals-fake-")
        with open(os.path.join(self.dir, "_fake.py"), "w", encoding="utf-8") as fh:
            fh.write(FAKE_BODY)
        py = sys.executable
        if os.name == "nt":
            self.exe = os.path.join(self.dir, "future.bat")
            with open(self.exe, "w", encoding="ascii", newline="\r\n") as fh:
                fh.write("@echo off\r\n"
                         f'"{py}" "%~dp0_fake.py" %*\r\n')
        else:
            self.exe = os.path.join(self.dir, "future")
            with open(self.exe, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f'#!/bin/sh\nexec "{py}" "$(dirname "$0")/_fake.py" "$@"\n')
            os.chmod(self.exe, 0o755)
        self.world = os.path.join(self.dir, "world.json")
        self.calls_path = os.path.join(self.dir, "calls.jsonl")
        self.set_world({})

    def set_world(self, world):
        with open(self.world, "w", encoding="utf-8") as fh:
            json.dump(world, fh, ensure_ascii=False)

    def reset(self, world):
        self.set_world(world)
        if os.path.exists(self.calls_path):
            os.unlink(self.calls_path)

    def calls(self):
        if not os.path.exists(self.calls_path):
            return []
        with open(self.calls_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def asks(self):
        """Only the per-session calls — what the cap and the cache govern."""
        return [c for c in self.calls() if c[:2] == ["session", "approvals"]]

    def approves(self):
        return [c for c in self.calls() if c[:2] == ["session", "approve"]]


FAKE_BODY = '''
import json, os, sys
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args, ensure_ascii=False) + "\\n")
world = json.load(open(os.path.join(here, "world.json"), encoding="utf-8"))
if args[:2] == ["session", "list"]:
    print(json.dumps({"sessions": world.get("sessions", [])}, ensure_ascii=False))
    sys.exit(0)
if args[:2] == ["session", "approvals"] and len(args) >= 3:
    if args[2] in (world.get("fail_reads") or []):
        sys.stderr.write("agent hiccup\\n")
        sys.exit(1)
    print(json.dumps({"pendingApprovals": (world.get("pending") or {}).get(args[2], [])},
                     ensure_ascii=False))
    sys.exit(0)
if args[:2] == ["session", "approve"] and len(args) >= 4:
    sys.exit(int(world.get("approve_rc", 0)))
sys.exit(2)
'''


class Workspace:
    """A throwaway watch root, so no real ledger/report/needs-you is written.

    The suite must never append to `state/approvals.jsonl` or
    `reports/needs-you.md`: those are the user's audit trail and inbox.
    """

    def __init__(self, fake, **cfg):
        self.root = tempfile.mkdtemp(prefix="selftest-approvals-root-")
        for sub in ("state", "reports"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        self.cfg = {"auto_approve": True, "auto_approve_per_cycle": 5,
                    "auto_approve_per_hour": 20, "future_bin": fake.exe}
        self.cfg.update(cfg)
        ap.WATCH = self.root
        ap.LEDGER = os.path.join(self.root, "state", "approvals.jsonl")
        ap.REPORT = os.path.join(self.root, "reports", "approvals.md")
        ap.CACHE = os.path.join(self.root, "state", "approval-cache.json")
        ap.EXCLUDE = os.path.join(self.root, "state", "approval-exclude.jsonl")
        ap.cfg = lambda: self.cfg
        self.fake = fake

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def ledger(self):
        p = self.path("state", "approvals.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def needs_you(self):
        p = self.path("reports", "needs-you.md")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def run(self, argv=None):
        saved = sys.stdout
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        try:
            return ap.main(argv or ["auto"])
        finally:
            sys.stdout.close()
            sys.stdout = saved


def session(sid, updated=1000, streaming=False, name="s"):
    return {"id": sid, "sessionName": name, "updatedAtMs": updated,
            "isStreaming": streaming, "queryCount": 1}


def pending(rid, command="python3 bin/selfcheck.py", tool="shell"):
    """A real payload: the agent spells the id `approval_request_id` (snake_case)."""
    return {"approval_request_id": rid, "session_id": "s",
            "tool_id": "call_1", "tool_name": tool, "kind": "shell_command",
            "risk_level": "medium", "title": "Approve shell command",
            "requested_action": {"command": command},
            "action": {"tool": tool, "category": "shell_command",
                       "summary": command, "command": command}}


fake = FakeFuture()

print("candidates(): a parked (streaming) run outranks everything, and it is never dropped")
sessions = [session("idle-1", 1000), session("idle-2", 2000),
            session("parked", 500, streaming=True)]
picked = ap.candidates(sessions, {}, {"max_approval_checks_per_cycle": 1,
                                      "approval_rescan_hours": 2})
check("with a cap of one, the parked run is the one asked about",
      [(r, s["id"]) for r, s in picked], [("streaming", "parked")])
check("and the answer keeps the session list's own order", [s["id"] for _, s in
      ap.candidates(sessions, {}, {"max_approval_checks_per_cycle": 9})],
      ["idle-1", "idle-2", "parked"])
check("...even when that list is handed over reversed", [s["id"] for _, s in
      ap.candidates(list(reversed(sessions)), {}, {"max_approval_checks_per_cycle": 9})],
      ["parked", "idle-2", "idle-1"])
check("a missing cache makes every session a candidate",
      len(ap.candidates(sessions, {}, {"max_approval_checks_per_cycle": 9})), 3)
check("...but never unbounded", len(ap.candidates(
    [session(f"x{i}", i) for i in range(120)], {}, {"max_approval_checks_per_cycle": 20})), 20)
cache = {"idle-1": {"updatedAtMs": 1000, "checkedAt": time.time() - 30},
         "idle-2": {"updatedAtMs": 2000, "checkedAt": time.time() - 30}}
check("an unchanged, idle session is not asked about again",
      [s["id"] for _, s in ap.candidates([session("idle-1", 1000)], cache,
                                         {"max_approval_checks_per_cycle": 20})], [])
check("a moved updatedAtMs is",
      [s["id"] for _, s in ap.candidates([session("idle-1", 1001)], cache,
                                         {"max_approval_checks_per_cycle": 20})], ["idle-1"])
check("the staleness backstop still re-checks an unmoved session",
      [s["id"] for _, s in ap.candidates([session("idle-1", 1000)], cache,
                                         {"max_approval_checks_per_cycle": 20,
                                          "approval_rescan_hours": 0})], ["idle-1"])

print("request_id()/tool_of(): the payload's own spelling, not the one we guessed")
check("approval_request_id is read", ap.request_id(pending("rid-1")), "rid-1")
check("a camelCase payload still works", ap.request_id({"requestId": "rid-2"}), "rid-2")
check("and the old id key too", ap.request_id({"id": "rid-3"}), "rid-3")
check("the tool name comes from tool_name", ap.tool_of(pending("r")), "shell")
check("...or from the nested action", ap.tool_of({"action": {"tool": "write"}}), "write")
check("a nameless request degrades to '?'", ap.tool_of({}), "?")

print("auto: no pending request anywhere — one process per cycle, not one per session")
many = [session(f"s{i:03d}", 1000 + i) for i in range(60)]
ws = Workspace(fake, max_approval_checks_per_cycle=20)
fake.reset({"sessions": many})
rc = ws.run()
check("the cycle succeeds", rc, 0)
check("the first (cold) cycle is capped, not one call per session", len(fake.asks()), 20)
check("and it is the only kind of call besides the list", len(fake.calls()), 21)
ws.run()
ws.run()                               # cycles two and three cover the rest
fake.reset({"sessions": many})          # same sessions, cache warm, count afresh
ws.run()
check("with a warm cache and no change, the cycle spawns exactly one process",
      len(fake.calls()), 1)
check("...and it has nothing to approve", ws.ledger(), [])

print("auto: a session whose approval list cannot be read is retried, not remembered")
ws2 = Workspace(fake, max_approval_checks_per_cycle=5)
fake.reset({"sessions": [session("sick", 10)], "fail_reads": ["sick"]})
ws2.run()
check("the failed read is not cached as 'nothing pending'",
      "sick" in json.load(open(ws2.path("state", "approval-cache.json"), encoding="utf-8")), False)
fake.reset({"sessions": [session("sick", 10)]})
ws2.run()
check("so the next cycle asks it again", len(fake.asks()), 1)

print("auto: a pending request outside the deny list is approved with the payload's id")
ws3 = Workspace(fake)
fake.reset({"sessions": [session("busy", 10, streaming=True)],
            "pending": {"busy": [pending("approval-abc")]}})
ws3.run()
approves = fake.approves()
check("the approve call happened", len(approves), 1)
check("carrying the id the payload spelled", approves[0][2:4], ["busy", "approval-abc"])
check("with the documented note flag", "--note" in approves[0], True)
ledger = ws3.ledger()
check("the ledger records one approval", [r.get("action") for r in ledger], ["approve"])
check("the ledger carries the real request id", ledger[0].get("requestId"), "approval-abc")
check("and the tool name", ledger[0].get("tool"), "shell")
check("the report mentions it", "→ approve" in open(ws3.path("reports", "approvals.md"),
                                                  encoding="utf-8").read(), True)

print("auto: a deny-listed request is escalated — never approved, never silent")
ws4 = Workspace(fake)
danger = pending("approval-danger", command="git push --force origin main")
fake.reset({"sessions": [session("risky", 10, streaming=True)],
            "pending": {"risky": [danger]}})
ws4.run()
check("nothing was approved", fake.approves(), [])
ledger = ws4.ledger()
check("the ledger marks it escalated", [r.get("action") for r in ledger], ["escalated"])
check("with the matched pattern as the reason",
      "deny-pattern" in (ledger[0].get("reason") or ""), True)
check("the user's inbox got a line", "approval-danger" in ws4.needs_you(), True)
check("and the report says it was not approved",
      "未自动批准" in open(ws4.path("reports", "approvals.md"), encoding="utf-8").read(), True)

print("auto: the per-cycle budget still stops the loop")
ws5 = Workspace(fake, auto_approve_per_cycle=2)
fake.reset({"sessions": [session("multi", 10, streaming=True)],
            "pending": {"multi": [pending(f"approval-{i}") for i in range(5)]}})
ws5.run()
check("only the budget's worth is approved", [c[3] for c in fake.approves()],
      ["approval-0", "approval-1"])
check("the ledger agrees", [r.get("action") for r in ws5.ledger()], ["approve", "approve"])

print("auto: the hourly ceiling and PAUSE still come first")
ws6 = Workspace(fake, auto_approve_per_hour=0)
fake.reset({"sessions": [session("busy", 10, streaming=True)],
            "pending": {"busy": [pending("approval-x")]}})
ws6.run()
check("an exhausted hourly budget spawns nothing at all", fake.calls(), [])
ws7 = Workspace(fake)
open(ws7.path("state", "PAUSE"), "w").close()
fake.reset({"sessions": [session("busy", 10, streaming=True)],
            "pending": {"busy": [pending("approval-x")]}})
ws7.run()
check("PAUSE short-circuits before any call", fake.calls(), [])

print("auto: --dry-run decides, spawns no approve, and records what it would do")
ws8 = Workspace(fake)
fake.reset({"sessions": [session("busy", 10, streaming=True)],
            "pending": {"busy": [pending("approval-dry")]}})
ws8.run(["auto", "--dry-run"])
check("no approve call", fake.approves(), [])
check("the ledger says dry-run", [r.get("action") for r in ws8.ledger()], ["dry-run"])

print("auto: an excluded session is still skipped")
ws9 = Workspace(fake, exclude_sessions=["hands-off"])
fake.reset({"sessions": [session("hands-off", 10, streaming=True)],
            "pending": {"hands-off": [pending("approval-no")]}})
ws9.run()
check("its request is neither approved nor escalated",
      (fake.approves(), ws9.ledger()), ([], []))
check("and it is not even asked about", fake.asks(), [])

if FAILED:
    print("selftest_approvals: FAILED")
    for line in FAILED:
        print("  " + line)
    sys.exit(1)
print("selftest_approvals: all checks passed")
