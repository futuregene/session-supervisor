#!/usr/bin/env python3
"""Is the watcher alive, and is it actually scanning? — answerable from any context.

Why this exists (2026-10-17). The 08:30 briefing is told to prove liveness with

    launchctl print gui/$(id -u)/<label> | grep 'state ='

and that command **cannot run from the supervisor's own context**: it answers
`Could not print domain: 125: Domain does not support specified action`. So the
one check that was supposed to notice a dead watcher reports nothing either way,
and the briefing can only ever read it as unknown. Measured the same morning,
after the supervisor had been silent for 5 h 20 m (03:11 → 08:32, not one
`scan.log` line): `watchd` was running again, and the two checks that DO work
from here were both green. A check that cannot fail is not a check.

So liveness is defined by evidence the facility produces itself, and nothing else:

  1. `state/health.json` is fresher than `--max-age` (default 180 s, six scan
     cycles) and `ok` is true — the scanner is running and its last
     `future session list` succeeded;
  2. the pid in `state/watchd.pid` is alive — the loop itself is up.

`--ensure` additionally *starts* `bin/watchd.sh` when it is not running, which is
what the briefing used to only describe in prose. It uses the same entry point a
human would (`bash bin/watchd.sh`, detached), so there is no second way for the
watcher to come up.

Exit codes: 0 healthy (or healed), 1 still unhealthy, so callers can branch.
`--root` exists so the selftest can point at a fixture instead of the live state.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

DEFAULT_MAX_AGE = 180
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import bash_exe  # noqa: E402


def paths(root):
    return (
        os.path.join(root, "state", "health.json"),
        os.path.join(root, "state", "watchd.pid"),
        os.path.join(root, "logs"),
    )


def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 - missing reads as unknown
        return ""


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def pid_alive(pid):
    """True when that pid exists. **Must not be written as `os.kill(pid, 0)` alone.**

    On Windows `os.kill()` has no signal half: any `sig` other than the two
    console events is passed to `TerminateProcess(handle, sig)`. So the usual
    POSIX idiom `os.kill(pid, 0)` does not probe the watcher — it *kills* it with
    exit code 0, and reports "dead" on the way out. A liveness check that stops
    the thing it is checking is worse than no check.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:  # noqa: BLE001
            return False
    return _pid_alive_windows(pid)


def _pid_alive_windows(pid):
    """Ask the OS for the exit code instead of poking it with a signal."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def watcher(root, _max_age):
    _, pidfile, _ = paths(root)
    pid = (read_text(pidfile) or "").strip()
    if not pid:
        return False, "state/watchd.pid is missing — no watcher recorded"
    if not pid_alive(pid):
        return False, f"watchd pid {pid} is not running"
    return True, f"watchd pid {pid} is running"


def scanner(root, max_age):
    healthfile, _, _ = paths(root)
    health = load(healthfile)
    if not health:
        return False, "state/health.json is missing or unreadable — the scanner has never run"
    age = time.time() - float(health.get("ts") or 0)
    if age > max_age:
        return False, f"state/health.json is {int(age)}s old (> {max_age}s) — the scanner stopped"
    if not health.get("ok"):
        return False, f"the scanner is running but failing: {health.get('error') or 'ok=false'}"
    return True, f"scanner ok, last scan {int(age)}s ago"


def start_watchd(root):
    _, _, logdir = paths(root)
    os.makedirs(logdir, exist_ok=True)
    bash = bash_exe()
    if not bash:
        raise RuntimeError(
            "no bash found (PATH, $BASH, or the Git Bash prefixes): bin/watchd.sh needs one "
            "(on Windows install Git Bash, on WSL use the Linux side)")
    out = open(os.path.join(logdir, "watchd-ensure.out"), "a", encoding="utf-8")
    # Detach from the caller, for real.
    #
    # `start_new_session=True` is POSIX-only and is *silently ignored* on Windows, so a
    # daemon started this way inherited the caller's console. When that console went
    # away, the whole daemon was killed with CTRL_CLOSE_EVENT: measured 2026-10-20, the
    # scheduled task ended with `LastTaskResult=3221225786` (0xC000013A,
    # STATUS_CONTROL_C_EXIT) and the supervisor was simply dead for 18 minutes — no wake
    # ever came, and nothing reported it, because the thing that would have reported it
    # is the thing that died. DETACHED_PROCESS gives the child no console to lose.
    kwargs = {"start_new_session": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        kwargs["start_new_session"] = False  # POSIX flag; keep it honest on Windows
    subprocess.Popen(
        [bash, os.path.join(root, "bin", "watchd.sh")],
        cwd=root,
        stdout=out,
        stderr=subprocess.STDOUT,
        **kwargs,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="watcher/scanner liveness, from any context")
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                    help=f"how stale state/health.json may be (default {DEFAULT_MAX_AGE}s)")
    ap.add_argument("--ensure", action="store_true",
                    help="start bin/watchd.sh when it is not running, then re-check")
    ap.add_argument("--root", default=REPO, help="repo root to inspect (for the selftest)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    w_ok, w_msg = watcher(args.root, args.max_age)
    healed = False
    if not w_ok and args.ensure:
        try:
            start_watchd(args.root)
            healed = True
        except Exception as exc:  # noqa: BLE001
            w_msg = f"{w_msg}; could not start it: {exc}"
        deadline = time.time() + min(args.max_age, 45)
        while time.time() < deadline:
            if watcher(args.root, args.max_age)[0]:
                break
            time.sleep(2)
        w_ok, w_msg = watcher(args.root, args.max_age)

    s_ok, s_msg = scanner(args.root, args.max_age)
    ok = w_ok and s_ok
    if args.json:
        print(json.dumps({"ok": ok, "watcher": w_ok, "scanner": s_ok, "healed": healed,
                          "watcherDetail": w_msg, "scannerDetail": s_msg},
                         ensure_ascii=False))
    else:
        print(f"{'ok  ' if w_ok else 'FAIL'} watcher: {w_msg}")
        print(f"{'ok  ' if s_ok else 'FAIL'} scanner: {s_msg}")
        if healed:
            print("started bin/watchd.sh (it was not running)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
