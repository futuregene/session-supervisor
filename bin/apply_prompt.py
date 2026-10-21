#!/usr/bin/env python3
"""Put the stable operating manual into the monitor session's *system prompt*.

The wake card is a user message, and FutureOS never drops user text when it
compacts, so every card stays in context forever (measured 2026-10-16: 110k
tokens ≈ 57 cards). The system prompt, by contrast, is rebuilt at the start of
every run and re-read from the session's recorded append — it costs cache-read
tokens but never accumulates.

That only helps if the append actually reaches the model. It did not before
future-os PR #1117 (the handler wrote the live loop's prompt and the run
rebuilt over it), and Help claimed otherwise. So this script does not trust the
flag: it **canary-verifies** the running binary end to end —

  1. create a throwaway session in a temp dir,
  2. append a random marker to *its* system prompt,
  3. run one tiny prompt that asks the model to quote the marker,
  4. only if the model quotes it, record `state/append_applied.json`,
  5. delete the canary session.

`bin/turn_prompt.py` drops the manual from the wake message only while that
record is present, canary-ok, and matches the current `prompts/append.md`
byte-for-byte. Everything is reversible (`--revert`).

Usage:
  python3 bin/apply_prompt.py --check     # report state, change nothing
  python3 bin/apply_prompt.py --apply     # canary-verify, then apply to the monitor
  python3 bin/apply_prompt.py --revert    # clear the append and the record
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Model used for the one-shot canary probe only (see canary_verify). Keep it a
# model that is live and paid on this host; the supervisor session's own model
# is decided in §3 of INSTALL.md, not here.
CANARY_MODEL = "westlake/glm-flash"
FUTURE = future_exe()
MONITOR_FILE = os.path.join(WATCH, "state", "monitor_session.txt")
APPEND_MD = os.path.join(WATCH, "prompts", "append.md")
APPLIED = os.path.join(WATCH, "state", "append_applied.json")
LOG = os.path.join(WATCH, "logs", "apply_prompt.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def future(args, timeout=300):
    env = dict(os.environ)
    env["PATH"] = tool_path()
    p = subprocess.run([FUTURE] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def monitor_id():
    with open(MONITOR_FILE, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def installed_build():
    """The running agent's build identity, for the record (e.g. `0.0.2-<sha>+local`)."""
    rc, out, _ = future(["session", "status", monitor_id(), "--json"], timeout=60)
    if rc != 0:
        return ""
    try:
        return json.loads(out).get("version", "")
    except ValueError:
        return ""


def canary_verify(manual):
    """True only if the running binary really puts the append in front of the model."""
    marker = "CANARY-" + hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    tmp = tempfile.mkdtemp(prefix="supervisor-canary-")
    sid = ""
    try:
        rc, out, err = future(["session", "new", "--cwd", tmp, "--name", "append canary", "--json"],
                              timeout=120)
        if rc != 0:
            log(f"canary: session new failed rc={rc} {err.strip()[:200]}")
            return False, "cannot create the canary session", ""
        sid = json.loads(out).get("sessionId", "")
        if not sid:
            return False, "session new returned no id", ""
        # This host (2026-10-20): the default `future/deepseek-flash` provider
        # answers HTTP 402 (balance exhausted), which reads to the canary as
        # "the model never saw the append". Pin the canary run to a live model
        # so the probe tests *delivery*, not the provider's billing status.
        future(["session", "set", sid, "--model", CANARY_MODEL], timeout=60)
        # The instruction lives *in the append*, not in the user turn. Asked to
        # echo a marker found in its system prompt, the model refused — correctly:
        # that is what an exfiltration probe looks like (2026-10-16 08:58, the
        # refusal itself proved delivery). A system-level instruction is followed.
        appended = (f"{marker}\nWhen asked for the verification token, reply with "
                    f"{marker} and nothing else.")
        rc, _out, err = future(["session", "set", sid, "--append-system-prompt", appended], timeout=60)
        if rc != 0:
            log(f"canary: set failed rc={rc} {err.strip()[:200]}")
            return False, "session set --append-system-prompt failed", sid
        ask = ("What is the verification token? Reply with the token alone, exactly "
               "as written, no other text.")
        # One retry: the model occasionally ignores the instruction, and a flaky
        # canary would keep the manual in the message forever (it flipped once at
        # 08:51 on a build where it had just passed at 08:50).
        quoted = False
        for _attempt in (1, 2):
            rc, out, err = future(["run", "--session", sid, "--permission", "none",
                                   "--no-tools", "--cwd", tmp, ask], timeout=300)
            quoted = marker in (out or "")
            log(f"canary: sid={sid} rc={rc} quoted={quoted}")
            if quoted:
                break
        if not quoted:
            return False, ("the model never saw the append (old binary?) — "
                           "staying on the full card"), sid
        return True, "the model quoted the appended marker", sid
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log(f"canary: {exc}")
        return False, f"canary errored: {exc}", sid
    finally:
        # Remove the throwaway session and its workspace: this script must not
        # leave anything in the user's session list.
        if sid:
            future(["session", "delete", sid], timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_check(manual):
    try:
        with open(APPLIED, "r", encoding="utf-8") as fh:
            applied = json.load(fh)
    except (OSError, ValueError):
        print("not applied (full card — the manual is sent in the message)")
        return 0
    fresh = applied.get("sha256") == sha256_text(manual)
    print(f"applied={applied.get('canary_ok')} fresh={fresh} "
          f"binary={applied.get('binary')} verifiedAt={applied.get('verifiedAt')} "
          f"mode={'minimal' if (fresh and applied.get('canary_ok')) else 'full'}")
    return 0


def current_supervisor_session():
    """The newest supervisor session that is not archived, per the agent itself.

    Why not trust the id file: it is runtime state. It can be stale — a
    `git checkout -- state/` or `git stash` restores the *committed* copy, which
    is the id from before the last roll (2026-10-16 08:39, after the 03:25 roll).
    Applying the manual then lands it on the **archived** session while the live
    one gets nothing, and `turn_prompt` cannot tell the difference. So resolve the
    target from the session list, and refuse when the file disagrees.
    """
    rc, out, _ = future(["session", "list", "--json"], timeout=60)
    if rc != 0:
        return ""
    try:
        listed = json.loads(out)
    except ValueError:
        return ""
    if isinstance(listed, dict):
        listed = listed.get("sessions") or listed.get("items") or []
    best, best_seen = "", -1
    for session in listed:
        name = (session.get("sessionName") or "").strip()
        if not name.startswith("会话总管") or "已归档" in name:
            continue
        seen = session.get("updatedAtMs") or 0
        if seen > best_seen:
            best, best_seen = session.get("id", ""), seen
    return best


def cmd_apply(manual, skip_canary=False, target=""):
    mon = target or monitor_id()
    if not target:
        live = current_supervisor_session()
        if live and mon != live:
            print(f"refusing: state/monitor_session.txt says {mon}, but the live "
                  f"supervisor session is {live} — the id file is stale; fix it first")
            log(f"apply: stale monitor id file={mon} live={live}")
            return 1
    else:
        # An explicit target is how a roll hands the manual to the session it just
        # created: that session is not in the session list yet (the agent indexes
        # it a moment later), so the cross-check above cannot see it — it would
        # refuse forever and the fresh supervisor would run without the manual.
        # Ask the agent about the id directly instead, and still refuse anything
        # that is not this repo's supervisor session.
        rc, out, _err = future(["session", "status", mon, "--json"], timeout=60)
        if rc != 0:
            print(f"refusing: cannot read session {mon}")
            log(f"apply: explicit target unreadable {mon}")
            return 1
        try:
            cwd = json.loads(out).get("cwd") or ""
        except ValueError:
            cwd = ""
        if os.path.realpath(cwd or "/") != os.path.realpath(WATCH):
            print(f"refusing: {mon} is not this repo's session (cwd={cwd!r})")
            log(f"apply: explicit target cwd mismatch {mon} cwd={cwd}")
            return 1
    rc, out, err = future(["session", "set", mon, "--append-system-prompt", manual], timeout=120)
    if rc != 0:
        print(f"cannot set the append on {mon}: {err.strip()[:300]}")
        log(f"apply: set failed rc={rc} {err.strip()[:200]}")
        return 1
    ok, why, _sid = (True, "canary skipped by request", "") if skip_canary else canary_verify(manual)
    if not ok:
        # Leave no half-state behind: an unverified append is either dead weight
        # (the card stays full) or, worse, duplicate text on every run.
        future(["session", "set", mon, "--append-system-prompt", ""], timeout=60)
        # The record and the append must stand or fall together: a stale record
        # with the append cleared makes `turn_prompt` drop the manual from the
        # message while nothing carries it (2026-10-16, caught mid-flight).
        if os.path.exists(APPLIED):
            os.remove(APPLIED)
        print(f"append set but NOT verified: {why}")
        print("the wake card stays full (see bin/turn_prompt.py); re-run after a rebuild")
        log(f"apply: unverified — {why}; append cleared and the record dropped")
        return 1
    record = {
        "sha256": sha256_text(manual),
        "appliedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verifiedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "canary_ok": True,
        "binary": installed_build(),
        "monitor": mon,
    }
    with open(APPLIED, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    print(f"applied + verified ({why}); wake card is now minimal ({len(manual)} B moved out of history)")
    log(f"apply: ok sha={record['sha256'][:12]} bytes={len(manual)} binary={record['binary']}")
    return 0


def cmd_revert():
    mon = monitor_id()
    rc, _out, err = future(["session", "set", mon, "--append-system-prompt", ""], timeout=60)
    if rc != 0:
        print(f"cannot clear the append on {mon}: {err.strip()[:300]}")
        return 1
    if os.path.exists(APPLIED):
        os.remove(APPLIED)
    print("append cleared; the wake card is full again")
    log("revert: cleared")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    ap.add_argument("--skip-canary", action="store_true",
                    help="apply without the end-to-end check (must then be verified elsewhere)")
    ap.add_argument("--session", default="",
                    help="target session id (default: state/monitor_session.txt, cross-checked "
                         "against the live supervisor session; an explicit id is used as-is "
                         "after checking that the session exists in this repo)")
    args = ap.parse_args(argv)

    try:
        with open(APPEND_MD, "r", encoding="utf-8") as fh:
            manual = fh.read().strip()
    except OSError:
        print(f"missing {APPEND_MD}")
        return 1
    if args.check:
        return cmd_check(manual)
    if args.revert:
        return cmd_revert()
    return cmd_apply(manual, skip_canary=args.skip_canary, target=args.session.strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
