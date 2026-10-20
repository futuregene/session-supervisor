#!/usr/bin/env python3
"""Keep `logs/` bounded: drop per-run logs by age, rotate append-only logs by size.

Why both halves: `logs/` holds two different kinds of file and they fail in
opposite ways.

* **Per-run logs** (`send-<id>-<hhmmss>.log`, `wake-<date>-<reason>.log`, …) are
  written once and never touched again. Their only failure mode is accumulation —
  hundreds of small files that nobody will ever read again. Age is the right key,
  so those are *deleted* once older than `--days`.
* **Append-only logs** (`scan.log`, `decide.log`, `wakes` …) are opened in append
  mode by long-lived loops and grow without bound. Deleting them is wrong (the
  running process keeps writing to the unlinked inode, and the recent history is
  exactly what a diagnosis needs), so those are *rotated in place*: when one
  exceeds `--max-bytes`, its last `--keep-lines` lines are kept.

The distinction is by name, because that is what the writers control:

    a file whose name carries a run date (`20261015-023022`) or matches
    `(send|wake|turn|update)-…` is per-run; everything else is append-only.

Usage:
    prune.py --logs [--days 7] [--max-bytes 1048576] [--keep-lines 5000] [--dry-run]

Called from `bin/wake.sh` (best effort, exit 0 always) so it never needs a cron.
"""
import argparse
import os
import re
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(WATCH, "logs")

# Names that carry a run identity, i.e. one file per wake / send. Anything else in
# logs/ is treated as append-only.
PER_RUN = re.compile(r"(?:^|[-_])(?:send|wake|turn|update|correction|scan)-\d{8}")
DATE_IN_NAME = re.compile(r"\d{8}")


def is_per_run(name: str) -> bool:
    return bool(PER_RUN.search(name)) or bool(DATE_IN_NAME.search(name))


def rotate(path: str, max_bytes: int, keep_lines: int) -> tuple[bool, str]:
    """Truncate an append-only log to its last `keep_lines` lines, in place."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"stat failed: {e}"
    if size <= max_bytes:
        return False, "under limit"
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes * 4))
            tail = f.read().splitlines(keepends=True)[-keep_lines:]
        # Write through the same path so the appending writer keeps its target.
        with open(path, "wb") as f:
            f.write(b"".join(tail))
    except OSError as e:
        return False, f"rotate failed: {e}"
    return True, f"{size} -> {os.path.getsize(path)} bytes"


def prune(days: int, max_bytes: int, keep_lines: int, dry: bool) -> list[str]:
    if not os.path.isdir(LOGDIR):
        return ["no logs/ directory"]
    cutoff = time.time() - days * 86400
    out = []
    deleted = rotated = kept_bytes = 0
    for name in sorted(os.listdir(LOGDIR)):
        path = os.path.join(LOGDIR, name)
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        kept_bytes += st.st_size
        if is_per_run(name):
            if st.st_mtime < cutoff:
                if not dry:
                    try:
                        os.remove(path)
                    except OSError as e:
                        out.append(f"could not remove {name}: {e}")
                        continue
                deleted += 1
        else:
            if st.st_size > max_bytes:
                if dry:
                    out.append(f"would rotate {name} ({st.st_size} bytes)")
                    rotated += 1
                    continue
                ok, detail = rotate(path, max_bytes, keep_lines)
                if ok:
                    rotated += 1
                    out.append(f"rotated {name}: {detail}")
                else:
                    out.append(f"kept {name}: {detail}")
    verb = "would delete" if dry else "deleted"
    out.insert(0, f"{verb} {deleted} per-run log(s) older than {days}d; "
                  f"rotated {rotated} append-only log(s); "
                  f"{len(os.listdir(LOGDIR))} files, {kept_bytes / 1024:.0f} KB before")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--logs", action="store_true", help="prune the logs/ directory")
    ap.add_argument("--days", type=int, default=7, help="keep per-run logs this many days (default 7)")
    ap.add_argument("--max-bytes", type=int, default=1024 * 1024,
                    help="rotate an append-only log above this size (default 1 MiB)")
    ap.add_argument("--keep-lines", type=int, default=5000,
                    help="lines kept when rotating (default 5000)")
    ap.add_argument("--dry-run", action="store_true", help="report only")
    args = ap.parse_args(argv)
    if not args.logs:
        ap.print_help()
        return 2
    for line in prune(args.days, args.max_bytes, args.keep_lines, args.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
