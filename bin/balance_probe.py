#!/usr/bin/env python3
"""Sample the platform balance on a timer, so a spend spike has a shape instead of a total.

Why this exists (2026-10-26): the 08:30 briefing reports one number — yesterday's
balance difference. That is the right *total*, and it is useless the moment the
number is wrong, because a 24 h window cannot tell a slow drip from a burst. On
the morning of 2026-10-26 the account lost 346 credits in 97 minutes while this
machine's own runs accounted for 2.12 of them: no local table could say when the
money left, only that it had. Two points five minutes apart did (`-14.2` then
`-3.4`), which is the difference between "something is leaking continuously" and
"it happened once, here".

So this is a *sampler*, not an accountant: it appends a point only when the last
one is older than `--min-gap`, and never edits or deletes anything. `balance-log
report --days 1` already walks back to the last point at or before its cutoff, so
a denser series does not change the daily total it computes.

`future account balance` costs nothing to call; the only budget here is a file
append and one subprocess. It is safe to call from the 30 s scanner because of
the gap guard.
"""
import argparse
import json
import os
import subprocess
import sys

LOG = os.path.expanduser("~/.future/balance-log.jsonl")
RECORDER = os.path.expanduser("~/.future/bin/balance-log")


def last_point():
    """The most recent record, or None. A malformed tail line is ignored, not fatal."""
    try:
        with open(LOG) as fh:
            lines = [line for line in fh if line.strip()]
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def probe(min_gap_seconds=600):
    """Record one balance point unless the previous one is younger than the gap.

    Returns {"skipped": reason} or the recorder's own output, so a caller can log
    why nothing happened without treating that as a failure.
    """
    prev = last_point()
    import time
    if prev is not None:
        age = time.time() - float(prev.get("ts") or 0)
        if age < min_gap_seconds:
            return {"skipped": "fresh", "age_seconds": round(age)}
    if not os.path.exists(RECORDER):
        return {"skipped": "no-recorder", "path": RECORDER}
    p = subprocess.run([sys.executable, RECORDER, "record", "--json"],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return {"skipped": "recorder-failed", "stderr": (p.stderr or p.stdout).strip()[:300]}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"recorded": True, "raw": p.stdout.strip()[:300]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-gap", type=int, default=600,
                    help="seconds; skip when the last point is younger (default 600)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    out = probe(args.min_gap)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
