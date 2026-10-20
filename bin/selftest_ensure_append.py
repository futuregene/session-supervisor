#!/usr/bin/env python3
"""Selftest for bin/ensure_append.py's decision (self-healing the manual's home).

Context: editing `prompts/append.md` invalidates the canary record, so the composer
falls back to `full` and every wake re-sends the whole manual. The owner forgot once
and the user saw the duplicate (「怎么上条信息中把操作手册又重复了一遍」, 2026-10-16).
The decision below is what makes the wake path heal itself without inventing a
canary run on every wake when the installed binary simply cannot support the append.

`should_apply` is pure, so no agent/session/network is touched here.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import ensure_append as ea  # noqa: E402

FAILED = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILED.append(name)


STALE = ea.STALE_REASON

print("the recoverable case: the text moved on → re-apply")
ea.last_attempt = lambda: (0.0, False)
go, why = ea.should_apply("full", STALE)
check("apply", go, True)
check("and it says why", why, STALE)

print("already minimal: nothing to do (the common case, every wake)")
go, why = ea.should_apply("minimal", "verified 2026-10-16 14:04:12 on 0.0.2-9dce2ca9+local")
check("no apply", go, False)
check("and the reason is reported", "already minimal" in why, True)

print("other 'full' reasons are NOT auto-applied (retrying would not help)")
for reason in ("no state/append_applied.json",
               "append was never canary-verified",
               "prompts/append.md is missing"):
    go, why = ea.should_apply("full", reason)
    check(f"does not apply on {reason!r}", go, False)
    check(f"and reports it as not recoverable ({reason!r})", "not the recoverable case" in why, True)

print("cooldown: a failing apply must not be retried on every wake")
ea.last_attempt = lambda: (time.time() - 60, False)
go, why = ea.should_apply("full", STALE)
check("no apply inside the cooldown", go, False)
check("and it says how long it has been", "cooling down" in why, True)
ea.last_attempt = lambda: (time.time() - (ea.DEFAULT_COOLDOWN + 60), False)
go, _ = ea.should_apply("full", STALE)
check("apply again once the cooldown passed", go, True)

print("a *successful* apply must not block the next real re-apply (2026-10-16)")
# The success proves the apply path works; meeting the stale case again means the file
# was edited a second time. Cooling down here re-sent the manual for the next 30 min.
ea.last_attempt = lambda: (time.time() - 60, True)
go, why = ea.should_apply("full", STALE)
check("apply even 60s after a success", go, True)
check("and it is the stale reason, not a cooldown", why, STALE)

print("--force overrides the reason and the cooldown")
ea.last_attempt = lambda: (time.time() - 5, False)
go, why = ea.should_apply("full", "append was never canary-verified", force=True)
check("forced apply", go, True)
check("marked as forced", why, "forced")
go, _ = ea.should_apply("minimal", "x", force=True)
check("force also applies when already minimal", go, True)

if FAILED:
    print("selftest_ensure_append: FAILED")
    for line in FAILED:
        print("  " + line)
    sys.exit(1)
print("selftest_ensure_append: all checks passed")
