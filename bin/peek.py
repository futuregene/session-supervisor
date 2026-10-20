#!/usr/bin/env python3
"""Peek at another session cheaply, then escalate depth only if needed.

Why this exists: a bare `future session transcript --session <id>` is *not*
bounded. Measured 2026-10-15 on a 600-entry session:

    bare                         250,224 B   (2,499 lines)
    --limit 3 --truncate 300         921 B
    --limit 1 --truncate 400         236 B
    --runs                         1,126 B

So the default read was ~270x more than the turn needed. Every byte here is
replayed into the supervisor's context, so this script makes the *cheap* read
the default and the expensive one an explicit, deliberate step:

  peek.py <session-id>              # metadata (--runs) + last 3 records, ~1 KB
  peek.py <session-id> --more       # last 10 records, wider truncation
  peek.py <session-id> --grep "PR #"  # only blocks matching a pattern
  peek.py <session-id> --full       # escape hatch: whole transcript (prints a size warning)

Progressive path: peek -> --more -> --grep <specific thing> -> --full (rare).
Never start with --full.
"""
import os
import re
import subprocess
import sys

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path  # noqa: E402

FUTURE = future_exe()


def run(args, timeout=120):
    env = dict(os.environ)
    env["PATH"] = tool_path()
    p = subprocess.run([FUTURE] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return p.stdout


def total_entries(sid):
    """Cheap: `--counts` scans the whole session but prints only a distribution."""
    out = run(["session", "transcript", "--session", sid, "--select", "all", "--counts"])
    m = re.search(r"nextCursor=(\d+)", out)
    return int(m.group(1)) if m else 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    sid = argv[0]
    rest = argv[1:]
    g = None
    if "--grep" in rest:
        g = rest[rest.index("--grep") + 1]

    def t(extra):
        return run(["session", "transcript", "--session", sid] + extra)

    def tail(n, trunc):
        """The LAST n records. `--cursor`/`--limit` walk *forward* from the start,
        so `--limit n` alone returns the OLDEST records — you must start at
        total-n. (scan.py does the same for last_assistant_text.)"""
        total = total_entries(sid)
        start = max(0, total - n)
        return t(["--cursor", str(start), "--limit", str(n), "--truncate", str(trunc)]), total

    # 1. Always cheap: run ledger (status / duration / error per run).
    meta = t(["--runs"])
    print("=== run ledger (cheap) ===")
    print(meta.rstrip())
    print()

    # 2. Then one shallow page, or the targeted read the caller actually asked for.
    if "--full" in rest:
        out = t([])
        print(f"=== FULL transcript ({len(out.encode())} B — you asked for it) ===")
        print(out.rstrip())
        return 0
    if g:
        out = t(["--grep", g, "--limit", "5", "--truncate", "400"])
        print(f"=== records matching {g!r} ===")
        print(out.rstrip())
        return 0
    if "--more" in rest:
        out, total = tail(10, 600)
        label = "last 10 records"
    else:
        out, total = tail(3, 300)
        label = "last 3 records (use --more / --grep <what> / --full to escalate)"
    print(f"=== {label} — of {total} total ===")
    print(out.rstrip())
    print(f"\n[peek: {len(meta.encode()) + len(out.encode())} B read]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))