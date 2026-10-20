#!/usr/bin/env python3
"""Pin the supervisor's single-flight lock — it must exclude, and it must expire.

Written 2026-10-16 after measuring that the lock excluded nothing: `acquire`
recorded its own short-lived pid, so every later `acquire` saw a dead pid and
stole the lock. Two supervisor sessions then wrote the records and opened PRs in
the same minutes (#190/#191), one journal entry landing mid-file.

The assertions are the contract:
  1. a fresh hold blocks the next turn **even though the recorded pid is dead**
     (this is the regression: the old code returned 0 there);
  2. `release` frees it immediately;
  3. a hold older than the ttl may be taken over — a crashed turn must not wedge
     the supervisor forever;
  4. the ttl comes from config, and a missing or non-positive value falls back.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lock  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")
    if not ok:
        fails.append(name)


def acquire_in_its_own_process(tmp, note):
    """A real turn is a separate process whose pid is gone the moment it prints.

    Running the acquire in-process would record *this* test's pid, which stays
    alive — exactly the confusion that hid the bug.
    """
    code = (
        "import sys; sys.path.insert(0, {here!r}); import lock;"
        "lock.WATCH = {tmp!r}; lock.LOCK = {lk!r}; lock.CFG_PATH = {cfg!r};"
        "sys.exit(lock.main(['acquire', '--note', {note!r}]))"
    ).format(here=HERE, tmp=tmp, lk=os.path.join(tmp, "agent.lock"),
             cfg=os.path.join(tmp, "config.json"), note=note)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace").returncode


def main():
    with tempfile.TemporaryDirectory() as tmp:
        lock.WATCH = tmp
        lock.LOCK = os.path.join(tmp, "agent.lock")
        lock.CFG_PATH = os.path.join(tmp, "config.json")

        check("acquire in a now-dead process", acquire_in_its_own_process(tmp, "turn-A"), 0)
        entry = json.load(open(lock.LOCK))
        check("holder recorded", entry.get("note"), "turn-A")
        check("recorded pid is dead (that is the point)", lock.pid_alive(entry["pid"]), False)
        check("fresh hold blocks the next turn", lock.main(["acquire", "--note", "turn-B"]), 1)

        check("release", lock.main(["release"]), 0)
        check("acquire after release", lock.main(["acquire", "--note", "turn-B"]), 0)
        check("release again is not an error", lock.main(["release"]), 0)
        check("status reads free when there is no file", lock.read_lock(), None)

        with open(lock.LOCK, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "ts": time.time() - lock.ttl_seconds() - 5}, fh)
        check("a crashed turn's stale hold is taken over", lock.main(["acquire", "--note", "turn-C"]), 0)
        check("and the takeover is recorded", json.load(open(lock.LOCK)).get("note"), "turn-C")

        with open(lock.CFG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"lock_ttl_seconds": 60}, fh)
        check("configured ttl", lock.ttl_seconds(), 60)
        with open(lock.CFG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"lock_ttl_seconds": 0}, fh)
        check("non-positive ttl falls back", lock.ttl_seconds(), lock.DEFAULT_TTL_SECONDS)
        os.remove(lock.CFG_PATH)
        check("missing config falls back", lock.ttl_seconds(), lock.DEFAULT_TTL_SECONDS)

    print("selftest_lock: ALL PASS" if not fails else f"selftest_lock: FAILED {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
