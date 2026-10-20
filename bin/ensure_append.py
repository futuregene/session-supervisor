#!/usr/bin/env python3
"""Re-apply the operating manual when `prompts/append.md` was edited and not applied.

Why this exists (user, 2026-10-16: 「怎么上条信息中把操作手册又重复了一遍」): the manual
rides in the monitor session's *system prompt*, and `bin/turn_prompt.py` only drops it
from the wake message while a canary-verified record matches the file on disk. Editing
`prompts/append.md` — a normal self-iteration step — invalidates that record, so the
composer correctly falls back to `full` mode and every wake then carries the whole
manual *again* (~3.5 KB per wake) until someone remembers to run
`apply_prompt.py --apply`. The owner of the file forgot exactly once and the user saw
the duplicated manual; a step that depends on remembering is the bug, not the forgetting.

So the wake path heals itself: `wake.sh` calls this before composing. It only re-applies
in the one case where the mechanism is known to work and the text merely moved on
("changed since it was applied"). A record that is missing, or an append that was never
canary-verified, is *not* auto-applied: that shape means the installed binary may not
support the append at all (`future-os` before #1117), where retrying would burn a canary
run forever and still not help — those are logged instead, and `mode` stays `full`.

`--force` re-applies regardless of the reason. A cooldown stops a failing apply from
being retried on every wake — and only a *failing* one: a success proves the apply path
works, so meeting the stale case again means a human edited the file a second time,
which is exactly what this script exists to heal. Cooling down after a success instead
kept re-sending the manual (2026-10-16: applied 17:10:44, `prompts/append.md` edited by
PR #169, the 17:37 wake still carried 3.7 KB of manual because 1587s < 1800s).
Always exits 0: a wake must not fail because of this.

  ensure_append.py [--force] [--cooldown-seconds 1800]
"""
import argparse
import json
import os
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WATCH, "bin"))
import turn_prompt  # noqa: E402  (mode() is the single source of truth)

LOG = os.path.join(WATCH, "logs", "ensure_append.log")
ATTEMPT = os.path.join(WATCH, "state", "append_apply_attempt.json")
DEFAULT_COOLDOWN = 1800
# The one reason that means "this worked before and the file has moved on".
STALE_REASON = "prompts/append.md changed since it was applied"


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def last_attempt():
    try:
        with open(ATTEMPT, encoding="utf-8") as fh:
            data = json.load(fh)
        return float(data.get("at") or 0), bool(data.get("ok"))
    except (OSError, ValueError, AttributeError):
        return 0.0, False


def note_attempt(ok):
    try:
        os.makedirs(os.path.dirname(ATTEMPT), exist_ok=True)
        with open(ATTEMPT, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "ok": ok}, fh)
    except OSError:
        pass


def should_apply(current_mode, reason, force=False, cooldown=DEFAULT_COOLDOWN):
    """`(bool, why)` — decide *before* touching anything, so it can be tested."""
    if force:
        return True, "forced"
    if current_mode != "full":
        return False, f"already minimal ({reason})"
    if reason != STALE_REASON:
        return False, f"not the recoverable case ({reason})"
    at, ok = last_attempt()
    waited = time.time() - at
    # Only a *failed* attempt cools down (see the module docstring): after a success,
    # the stale case means the file was edited again, which must be healed now.
    if at and not ok and waited < cooldown:
        return False, f"retried {int(waited)}s ago; cooling down"
    return True, STALE_REASON


def run_apply(timeout=240):
    try:
        p = subprocess.run([sys.executable, os.path.join(WATCH, "bin", "apply_prompt.py"), "--apply"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()[-300:]
    except Exception as exc:  # noqa: BLE001
        return False, f"apply errored: {exc}"


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cooldown-seconds", type=int, default=DEFAULT_COOLDOWN)
    args = ap.parse_args(argv)

    current, reason = turn_prompt.mode()
    go, why = should_apply(current, reason, args.force, args.cooldown_seconds)
    if not go:
        if current == "full" and reason == STALE_REASON:
            log(f"skipped: {why}")
        else:
            log(f"nothing to do: {why}")
        print(f"ensure_append: {why}")
        return 0
    ok, detail = run_apply()
    note_attempt(ok)
    after, after_reason = turn_prompt.mode()
    log(f"applied={ok} ({why}); mode now {after} ({after_reason}); {detail}")
    print(f"ensure_append: applied={ok}; mode={after}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
