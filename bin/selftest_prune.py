#!/usr/bin/env python3
"""Behavioural checks for bin/prune.py — run by bin/selfcheck.py.

The two halves fail in opposite ways, so both are asserted: a per-run log must
disappear once it is old, and an append-only log must be *truncated* (never
unlinked — a live writer appends to the path, and a deleted file would keep
receiving writes invisibly while the diagnosis disappears).

Each case is falsifiable: flipping `is_per_run()` or the rotate branch turns the
corresponding assertion red.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prune  # noqa: E402


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def run() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as d:
        prune.LOGDIR = d
        _write(os.path.join(d, "send-20260101-010101-abc-020304.log"), "old send")
        _age(os.path.join(d, "send-20260101-010101-abc-020304.log"), 30)
        _write(os.path.join(d, "wake-20260101-010101-events.log"), "old wake")
        _age(os.path.join(d, "wake-20260101-010101-events.log"), 30)
        _write(os.path.join(d, "wake-20991231-010101-events.log"), "fresh wake")
        _write(os.path.join(d, "scan.log"), "".join(f"line {i}\n" for i in range(400)))
        _age(os.path.join(d, "scan.log"), 30)  # old but append-only: must survive
        _write(os.path.join(d, "tiny.log"), "new writer activity\n")

        # Dry run changes nothing.
        prune.prune(7, 200, 10, dry=True)
        for name in ("send-20260101-010101-abc-020304.log", "scan.log"):
            if not os.path.exists(os.path.join(d, name)):
                failures.append(f"dry run removed {name}")

        prune.prune(7, 200, 10, dry=False)

        if os.path.exists(os.path.join(d, "send-20260101-010101-abc-020304.log")):
            failures.append("old per-run send log was not deleted")
        if os.path.exists(os.path.join(d, "wake-20260101-010101-events.log")):
            failures.append("old per-run wake log was not deleted")
        if not os.path.exists(os.path.join(d, "wake-20991231-010101-events.log")):
            failures.append("fresh per-run wake log was deleted")
        if not os.path.exists(os.path.join(d, "scan.log")):
            failures.append("append-only log was unlinked instead of rotated")
        else:
            body = open(os.path.join(d, "scan.log"), encoding="utf-8").read()
            lines = body.splitlines()
            if len(lines) != 10:
                failures.append(f"rotate kept {len(lines)} lines, expected 10")
            if lines and lines[-1] != "line 399":
                failures.append(f"rotate dropped the newest line (last is {lines[-1]!r})")
        if not os.path.exists(os.path.join(d, "tiny.log")):
            failures.append("small append-only log was removed")

    if failures:
        print("prune selftest FAILED: " + "; ".join(failures))
        return 1
    print("prune selftest ok: per-run logs expire by age, append-only logs rotate in place")
    return 0


if __name__ == "__main__":
    sys.exit(run())
