#!/usr/bin/env python3
"""Single-flight lock so two supervisor turns never act at the same time.

  lock.py acquire   -> exit 0 if acquired, exit 1 if another turn holds it
  lock.py release   -> release if held
  lock.py status

**Held by freshness, not by a live pid (measured 2026-10-16 22:3x).** Until this
fix `acquire` refused only when the recorded pid was *alive* — but the pid it
recorded is the `python3 bin/lock.py acquire` process itself, which exits as soon
as it prints. So every later `acquire` saw a dead pid and stole the lock: the
file claimed exclusivity (`docs/artifact`-level promise in `prompts/append.md`)
and provided none. Recording something longer-lived is not possible either — the
whole process tree above a tool call is `zsh -> future agent`,
**one agent process shared by every session** (measured: ancestor pid 16714 was
up 5 h serving all sessions), so no per-turn process exists to point at.

A turn therefore holds the lock from `acquire` until `release`, and a crashed
turn is escaped by *time*: after `lock_ttl_seconds` (config, default 900 s) the
lock may be taken again. The trade is deliberate: blocking wakes forever is worse
than a rare overlap, and observed supervisor turns run 4–7 min.

**Why this was worth fixing at all (2026-10-16 21:49 incident).** After the
21:27 rotation the *archived* session `20261016-153105-…` was still reachable —
the user kept talking to it (its 21:50 thread is in `journal/`), so it ran turns
of its own and, with the lock a no-op, it and the new monitor both wrote the
records and opened PRs (#190/#191 interleaved, one journal entry landed
mid-file). Neither turn was doing anything wrong; nothing excluded them.
"""
import json
import os
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(WATCH, "state", "agent.lock")
# Kept for callers that still pass a pid-based notion of staleness; the lock
# itself no longer consults `pid_alive` (see the module docstring).
DEFAULT_TTL_SECONDS = 900
CFG_PATH = None  # resolved on first use; tests set it to a fixture


def _config_path() -> str:
    """Where the run configuration lives — `toolpath.config_path()` unless overridden.

    `CFG_PATH` stays a module attribute because `bin/selftest_lock.py` points it at
    a temporary fixture; the default is resolved lazily so importing this file does
    not depend on `toolpath` being importable yet.
    """
    global CFG_PATH
    if CFG_PATH:
        return CFG_PATH
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from toolpath import config_path
    CFG_PATH = config_path()
    return CFG_PATH


def read_lock():
    try:
        with open(LOCK, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def pid_alive(pid):
    """Delegates to `liveness.pid_alive` — one implementation, one behaviour.

    It used to be the POSIX idiom `os.kill(pid, 0)` here as well, which on Windows
    is `TerminateProcess(handle, 0)`: a *liveness* probe that ends the process it
    was asked about. Only `bin/selftest_lock.py` calls this now (the lock itself
    is held by TTL, see above), but a second copy of that idiom is a second place
    to forget it when the facility moves off macOS.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import liveness  # local import: keeps `lock.py` usable without it at import time
    return liveness.pid_alive(pid)


def ttl_seconds():
    """How long a turn may hold the lock before a later turn may take it over."""
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        val = int(cfg.get("lock_ttl_seconds") or 0)
        return val if val > 0 else DEFAULT_TTL_SECONDS
    except Exception:
        return DEFAULT_TTL_SECONDS


def holder_label(entry):
    if not entry:
        return "?"
    for key in ("note", "session", "pid"):
        if entry.get(key):
            return str(entry[key])
    return "?"


def main(argv):
    cmd = argv[0] if argv else "status"
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    cur = read_lock()
    me = os.getpid()
    if cmd == "acquire":
        if cur:
            ts = float(cur.get("ts") or 0)
            age = time.time() - ts
            ttl = ttl_seconds()
            if age < ttl:
                print(f"busy: held {int(age)}s ago by {holder_label(cur)} (ttl {ttl}s)")
                return 1
            print(f"taking over a lock older than {ttl}s ({int(age)}s, {holder_label(cur)})")
        note = ""
        for i, a in enumerate(argv[1:]):
            if a == "--note" and i + 2 <= len(argv[1:]):
                note = argv[i + 2]
        with open(LOCK, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "pid": me,
                    "ts": time.time(),
                    "note": note or (argv[1] if len(argv) > 1 and not argv[1].startswith("--") else ""),
                    "session": os.environ.get("FUTURE_SESSION_ID", ""),
                },
                fh,
            )
        print(f"acquired by pid {me}")
        return 0
    if cmd == "release":
        # A supervisor turn is several shell processes, so release must not be
        # pid-bound: the owner of the lock is the *turn*, not the process.
        if cur:
            try:
                os.remove(LOCK)
            except FileNotFoundError:
                pass
            print(f"released (was pid {cur.get('pid')})")
        else:
            print("already free")
        return 0
    print(json.dumps(cur) if cur else "free")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
