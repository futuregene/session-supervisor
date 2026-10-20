#!/usr/bin/env python3
"""Self-test for the wake gate's "only new news wakes us" rule (bin/decide.py).

Run: python3 bin/selftest_decide.py
Wired into bin/selfcheck.py so CI runs it with the rest.

Why this exists: the heartbeat was clock-driven, so any low-priority event that
stayed pending (the normal case: a session waiting for the *user*) woke a turn
every five minutes for as long as it sat there — 115 of 128 wakes in the
measured 24 h, ≈¥0.24 each. `has_new_pending` is what stops that; regressing it
silently restores a ~¥28/day bill, so pin the boundary cases here.

Also pins `name_heal_needed`: the desktop app's first-turn auto-title overwrote the
newly rolled supervisor session's name, and the user could not find a new `会话总管`
in the sidebar (2026-10-16 21:32) — so the wake path renames it back, once, inside
that first-run window only.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("decide", os.path.join(ROOT, "bin", "decide.py"))
decide = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decide)

FAILED = []


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


# The desktop app's first-turn auto-title renamed the freshly rolled supervisor
# session away from `会话总管` (measured 2026-10-16 21:32), so no `会话总管` appeared in
# the sidebar. The heal must be bounded to that first run: later on, a different name
# is the user's own choice and must be left alone.
NAME_CASES = [
    ("a generated title after the first run",
     {"sessionName": "磁盘清理与事件处置", "queryCount": 1}, True),
    ("the name we set", {"sessionName": "会话总管", "queryCount": 1}, False),
    ("a rename the user made later", {"sessionName": "用户改的名", "queryCount": 5}, False),
    ("no runs yet (the roll just created it)",
     {"sessionName": "别的", "queryCount": 0}, False),
    ("unreadable status", {}, False),
]

for label, status, want in NAME_CASES:
    check(f"name heal: {label}", decide.name_heal_needed(status, "会话总管"), want)


# ── has_new_pending ─────────────────────────────────────────────────────────
print("has_new_pending")
check("first ever wake (no state file)", decide.has_new_pending(["a"], None), True)
check("first ever wake (empty state)", decide.has_new_pending(["a"], []), True)
check("identical set → no wake", decide.has_new_pending(["a", "b"], ["a", "b"]), False)
check("identical set, different order", decide.has_new_pending(["b", "a"], ["a", "b"]), False)
check("one new id → wake", decide.has_new_pending(["a", "b", "c"], ["a", "b"]), True)
call = "an event we acked disappears → not news"
check(call, decide.has_new_pending(["b"], ["a", "b"]), False)
call = "everything acked → no wake (the idle path handles it)"
check(call, decide.has_new_pending([], ["a", "b"]), False)
check("both empty", decide.has_new_pending([], []), False)
check("nothing pending but stale seen ids", decide.has_new_pending(None, ["a"]), False)
check("None ids dropped", decide.has_new_pending([None, "a"], ["a"]), False)
# The seen file is JSON, so ids come back as str; a numeric-looking id must not
# count as new just because of its Python type.
check("str coercion", decide.has_new_pending(["7"], ["7"]), False)
check("str coercion, new", decide.has_new_pending(["7", "8"], ["7"]), True)
# A wake that is deferred (monitor busy) must not consume the news — decide.py
# only writes the file once a wake really happens; this is the function's side
# of that contract: the same set is still "fresh" until it is recorded.
check("news survives a deferred wake", decide.has_new_pending(["a"], None), True)

# A wake that is deferred (monitor busy) must not consume the news — decide.py
# only writes the file once a wake really happens; this is the function's side
# of that contract: the same set is still "fresh" until it is recorded.

# ── the wake path itself ────────────────────────────────────────────────────
# Regression (2026-10-20): `main()` reached its spawn as
# `subprocess.Popen([BASH, ...wake.sh])` and there is no `BASH` in this module, so
# every automatic wake died with `NameError: name 'BASH' is not defined` — after
# `state/pending_seen.json` had already been written. The supervisor was therefore
# never woken between 19:17 and 21:11 while the ledger claimed two wakes.
# A pure unit test of the gate cannot see that; this one runs `main()` to the spawn.
print("wake path (spawn is real, everything around it is stubbed)")
import json
import subprocess as _subprocess
import tempfile
import types


def run_main(tmp, spawn, pending_rows=()):
    """Drive decide.main() in `tmp`, with `spawn` standing in for subprocess.Popen."""
    calls = []

    def fake_popen(argv, **kw):
        calls.append((argv, kw))
        return spawn(argv, **kw)

    fake_sub = types.SimpleNamespace(
        Popen=fake_popen,
        DEVNULL=_subprocess.DEVNULL,
        CREATE_NEW_PROCESS_GROUP=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        DETACHED_PROCESS=getattr(_subprocess, "DETACHED_PROCESS", 0),
    )
    saved = {}
    overrides = {
        "PENDING": os.path.join(tmp, "inbox", "pending.jsonl"),
        "WAKES": os.path.join(tmp, "state", "wakes.jsonl"),
        "PENDING_SEEN": os.path.join(tmp, "state", "pending_seen.json"),
        "PAYLOAD": os.path.join(tmp, "state", "turn_payload.json"),
        "MONITOR_FILE": os.path.join(tmp, "state", "monitor_session.txt"),
        "LOG": os.path.join(tmp, "logs", "decide.log"),
        "CFG_PATH": os.path.join(tmp, "config.json"),
        "subprocess": fake_sub,
    }
    for k, v in overrides.items():
        saved[k] = getattr(decide, k)
        setattr(decide, k, v)
    # `future()` asks the CLI whether the monitor is streaming; answer "idle".
    saved_future = decide.future
    decide.future = lambda cfg, args, timeout=30: (
        0, json.dumps({"isStreaming": False, "sessionName": "会话总管", "queryCount": 9}), "")
    saved_bash = decide.bash_exe
    decide.bash_exe = lambda: "bash-that-exists"
    try:
        for sub in ("inbox", "state", "logs"):
            os.makedirs(os.path.join(tmp, sub), exist_ok=True)
        open(os.path.join(tmp, "config.json"), "w").write(
            json.dumps({"heartbeat_minutes": 0, "min_wake_gap_seconds": 0}))
        open(os.path.join(tmp, "state", "monitor_session.txt"), "w").write("sess-monitor")
        with open(os.path.join(tmp, "inbox", "pending.jsonl"), "w", encoding="utf-8") as fh:
            for row in pending_rows:
                fh.write(json.dumps(row) + "\n")
        rc = decide.main()
    finally:
        for k, v in saved.items():
            setattr(decide, k, v)
        decide.future = saved_future
        decide.bash_exe = saved_bash
    return rc, calls


with tempfile.TemporaryDirectory() as tmp:
    rc, calls = run_main(tmp, lambda argv, **kw: None)
    check("a wake reaches the spawn", len(calls), 1)
    if calls:
        check("it spawns an existing executable", calls[0][0][0], "bash-that-exists")
        check("with wake.sh and the reason after it",
              (os.path.basename(calls[0][0][1]), calls[0][0][2]), ("wake.sh", "heartbeat"))
    check("the wake is recorded", os.path.exists(os.path.join(tmp, "state", "wakes.jsonl")), True)
    check("and the events are marked weighed",
          os.path.exists(os.path.join(tmp, "state", "pending_seen.json")), True)

# The other half of the regression: a spawn that fails must NOT consume the news.
with tempfile.TemporaryDirectory() as tmp:
    def boom(argv, **kw):
        raise OSError("no such file")
    rc, calls = run_main(tmp, boom)
    check("a failed spawn reports failure", rc, 1)
    check("and records nothing (the news survives)",
          os.path.exists(os.path.join(tmp, "state", "pending_seen.json")), False)
    check("no wake is claimed in the ledger",
          os.path.exists(os.path.join(tmp, "state", "wakes.jsonl")), False)

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}):")
    for f in FAILED:
        print("  " + f)
    sys.exit(1)
print("selftest_decide: all checks passed")
