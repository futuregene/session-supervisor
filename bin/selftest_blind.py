#!/usr/bin/env python3
"""Selftest for the third state `bin/blind.py` now has to see: running, not advancing.

`health.json` says `ok: true` for ever — `have_health()` never looks at its timestamp —
and `state/watchd.pid` says the loop is up. Neither can tell a scanner that finishes a
round from one that dies inside every round, so the only evidence is
`state/state.json: lastScanTs`. This pins both directions of the new criterion:

  * a frozen `lastScanTs` with a live watcher is reported (and reported **once**, with a
    stable signature — the message names the minute, which changes every cycle);
  * a fresh `lastScanTs`, or a dead watcher, is not this criterion's news;
  * the failing-scan path still behaves exactly as before (two misses before it speaks).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load():
    spec = importlib.util.spec_from_file_location("blind", ROOT / "bin" / "blind.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def live_pid() -> str:
    """A pid that is certainly alive: this process."""
    return str(__import__("os").getpid())


def dead_pid() -> str:
    """A pid that is certainly gone: a child that has already exited and been reaped."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    return str(p.pid)


def fixture(mod, tmp: pathlib.Path, *, last_scan_ts, pid, interval=30,
            health=None) -> None:
    state = tmp / "state"
    (state).mkdir(parents=True, exist_ok=True)
    (tmp / "reports").mkdir(parents=True, exist_ok=True)
    mod.HEALTH = str(state / "health.json")
    mod.BLIND_STATE = str(state / "blind.json")
    mod.SCAN_STATE = str(state / "state.json")
    mod.PIDFILE = str(state / "watchd.pid")
    mod.REPORT = str(tmp / "reports" / "blind.md")
    mod.CONFIG = str(tmp / "config.json")
    (tmp / "config.json").write_text(json.dumps({"scan_interval_seconds": interval}),
                                     encoding="utf-8")
    (state / "health.json").write_text(
        json.dumps(health if health is not None else
                   {"ts": time.time(), "ok": True, "sessionsTotal": 12}), encoding="utf-8")
    (state / "state.json").write_text(json.dumps({"lastScanTs": last_scan_ts}),
                                      encoding="utf-8")
    (state / "watchd.pid").write_text(pid, encoding="utf-8")


def run_main(mod):
    """`main()` with notices captured instead of written to the deployment's state."""
    said: list[str] = []
    original_notice = mod.notice
    mod.notice = said.append
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = mod.main()
    finally:
        mod.notice = original_notice
    return rc, out.getvalue(), said


def main() -> int:
    mod = load()
    now = time.time()

    # 1. the real incident, compressed: 400 s of no state write at a 30 s interval
    with tempfile.TemporaryDirectory(prefix="blind-stall-") as d:
        tmp = pathlib.Path(d)
        fixture(mod, tmp, last_scan_ts=now - 400, pid=live_pid())
        msg, sig = mod.progress_stall(now)
        check("a frozen lastScanTs is a stall", bool(msg) and "没有推进" in msg, f"{msg!r}")
        check("the stall says where it stopped and for how long",
              "lastScanTs 停在" in msg and "已 6 分钟" in msg, f"{msg!r}")
        check("the stall signature is stable, the message is not",
              sig == mod.progress_stall(now + 90)[1] and msg != mod.progress_stall(now + 90)[0],
              f"{sig!r}")
        check("the stall names its own cause for the briefing", "main()" in mod._diagnose(msg),
              mod._diagnose(msg))

        rc, printed, said = run_main(mod)
        check("a stall is reported on the first cycle (no blip allowance)",
              len(said) == 1 and "没有推进" in said[0], f"{said}")
        check("the stall is printed to the log once", "[blind] WARN scan-not-advancing" in printed,
              printed)
        report = (tmp / "reports" / "blind.md").read_text(encoding="utf-8")
        check("reports/blind.md says it is not advancing", "扫描在跑但没有推进" in report, report)

        rc, printed, said = run_main(mod)
        check("the same stall is not announced twice", said == [] and
              printed.strip() == "", f"{said} {printed!r}")

        # ... and the recovery is announced, exactly like a recovered failure
        fixture(mod, tmp, last_scan_ts=time.time(), pid=live_pid())
        rc, printed, said = run_main(mod)
        check("a scan that advances again clears the stall",
              "recovered" in printed and said == [], f"{printed!r}")

    # 2. the other direction: fresh progress is quiet
    with tempfile.TemporaryDirectory(prefix="blind-ok-") as d:
        tmp = pathlib.Path(d)
        fixture(mod, tmp, last_scan_ts=now - 20, pid=live_pid())
        check("a recent lastScanTs is not a stall", mod.progress_stall(now) == ("", ""),
              f"{mod.progress_stall(now)}")
        rc, printed, said = run_main(mod)
        check("a healthy round says nothing", printed.strip() == "" and said == [],
              f"{printed!r} {said}")

    # 3. no watcher = the other check's business, not this one
    with tempfile.TemporaryDirectory(prefix="blind-dead-") as d:
        tmp = pathlib.Path(d)
        fixture(mod, tmp, last_scan_ts=now - 400, pid=dead_pid())
        check("a dead watcher is not reported as a stall", mod.progress_stall(now) == ("", ""),
              f"{mod.progress_stall(now)}")

    # 4. a missing pid file / no baseline yet must not invent a stall
    with tempfile.TemporaryDirectory(prefix="blind-new-") as d:
        tmp = pathlib.Path(d)
        fixture(mod, tmp, last_scan_ts=0, pid=live_pid())
        check("a scanner that has not written a baseline yet is not a stall",
              mod.progress_stall(now) == ("", ""), f"{mod.progress_stall(now)}")

    # 5. the failing-scan path is untouched: one blip is quiet, two speak
    with tempfile.TemporaryDirectory(prefix="blind-fail-") as d:
        tmp = pathlib.Path(d)
        bad = {"ts": now, "ok": False, "error": "session list rc=1",
               "error_line": "unable to connect to Future Agent"}
        fixture(mod, tmp, last_scan_ts=time.time(), pid=live_pid(), health=bad)
        rc, printed, said = run_main(mod)
        check("one failing scan is still just a blip", said == [] and printed.strip() == "",
              f"{said} {printed!r}")
        rc, printed, said = run_main(mod)
        check("two failing scans still speak", len(said) == 1 and "连续失败" in said[0],
              f"{said}")

    print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
