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

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}):")
    for f in FAILED:
        print("  " + f)
    sys.exit(1)
print("selftest_decide: all checks passed")
