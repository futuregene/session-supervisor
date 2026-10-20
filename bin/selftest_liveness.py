#!/usr/bin/env python3
"""Pin `bin/liveness.py` — the check that has to work from the supervisor's context.

Context: on 2026-10-17 the briefing's liveness probe (`launchctl print gui/…`)
turned out to be unrunnable here (125: Domain does not support specified action),
so a 5 h 20 m silence (03:11 → 08:32, zero `scan.log` lines) passed unnoticed by
the one check meant to catch it. These assertions fix the replacement's contract:
each half must fail on its own evidence, and both together must be required.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import liveness  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")
    if not ok:
        fails.append(name)


def fixture(root, pid=None, health=None):
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    if pid is not None:
        with open(os.path.join(root, "state", "watchd.pid"), "w") as fh:
            fh.write(str(pid))
    if health is not None:
        with open(os.path.join(root, "state", "health.json"), "w") as fh:
            json.dump(health, fh)


def run(root, *extra):
    return subprocess.run([sys.executable, os.path.join(HERE, "liveness.py"),
                           "--root", root, *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").returncode


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # No state at all: the scanner has never run, nothing is recorded.
        check("empty root is unhealthy", run(tmp), 1)

        # Healthy: our own pid (alive) and a fresh ok scan.
        fixture(tmp, pid=os.getpid(), health={"ts": time.time(), "ok": True})
        check("fresh state, live pid", run(tmp), 0)

        # The watcher half must fail on its own: a pid that is not running.
        fixture(tmp, pid=99999999, health={"ts": time.time(), "ok": True})
        check("dead watcher pid fails", run(tmp), 1)

        # The scanner half must fail on its own, twice over: staleness and ok=false.
        fixture(tmp, pid=os.getpid(), health={"ts": time.time() - 9999, "ok": True})
        check("stale health fails", run(tmp), 1)
        fixture(tmp, pid=os.getpid(), health={"ts": time.time(), "ok": False, "error": "rc=1"})
        check("ok=false fails", run(tmp), 1)

        # A missing health file is 'never ran', not 'healthy'.
        os.remove(os.path.join(tmp, "state", "health.json"))
        check("missing health fails", run(tmp), 1)

        # --max-age is respected: 100 s old is fine under 180 s, not under 60 s.
        fixture(tmp, pid=os.getpid(), health={"ts": time.time() - 100, "ok": True})
        check("100s old, default max-age", run(tmp), 0)
        check("100s old, --max-age 60", run(tmp, "--max-age", "60"), 1)

        # --json is machine-readable and carries both halves.
        p = subprocess.run([sys.executable, os.path.join(HERE, "liveness.py"),
                            "--root", tmp, "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        payload = json.loads(p.stdout)
        check("json has both halves", sorted(payload)[:3],
              ["healed", "ok", "scanner"])

        # `pid_alive` must *read* the pid, never signal it. On Windows the POSIX
        # idiom `os.kill(pid, 0)` is `TerminateProcess(handle, 0)` — it would kill
        # the watcher it was asked to check. Assert the three outcomes instead:
        # our own pid is alive, a wild pid is not, junk is not.
        sys.path.insert(0, HERE)
        import liveness  # noqa: E402
        check("pid_alive(self)", liveness.pid_alive(os.getpid()), True)
        check("pid_alive(wild)", liveness.pid_alive(999999), False)
        check("pid_alive(junk)", liveness.pid_alive("not-a-pid"), False)

    print("selftest_liveness: ALL PASS" if not fails else f"selftest_liveness: FAILED {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
