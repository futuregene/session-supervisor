#!/usr/bin/env python3
"""Local checks gate for this repo. Run before every push / PR.

    python3 bin/selfcheck.py            # all checks
    python3 bin/selfcheck.py --quick    # skip the slow ones

Exit 0 = green (safe to push and let the PR auto-merge).
Exit 1 = at least one check failed; do NOT push / do NOT merge.

This is the "本地检查" AGENTS.md §4 requires to have actually run — it is what
the PR's CI workflow runs too, so a green run here and a green CI agree.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd: list[str], cwd: str = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def check(name: str, fn) -> bool:
    try:
        ok, detail = fn()
    except Exception as exc:  # a broken check is a red check, never a green one
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------- checks


def check_python_syntax() -> tuple[bool, str]:
    bad = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".worktrees", "node_modules"}]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            p = run([sys.executable, "-m", "py_compile", path])
            if p.returncode != 0:
                bad.append(os.path.relpath(path, ROOT))
    return (not bad), (", ".join(bad[:5]) if bad else "all .py compile")


def check_shell_syntax() -> tuple[bool, str]:
    bin_dir = os.path.join(ROOT, "bin")
    bad = []
    for fn in sorted(os.listdir(bin_dir)):
        if not fn.endswith(".sh"):
            continue
        p = run(["bash", "-n", os.path.join(bin_dir, fn)])
        if p.returncode != 0:
            bad.append(fn)
    return (not bad), (", ".join(bad) if bad else "all bin/*.sh parse")


def check_json() -> tuple[bool, str]:
    bad = []
    for fn in (os.path.join("bin", "config.json"),):
        try:
            json.load(open(os.path.join(ROOT, fn), encoding="utf-8"))
        except Exception as exc:
            bad.append(f"{fn}: {exc}")
    return (not bad), ("; ".join(bad) if bad else "bin/config.json parses")


def check_guard_selftest() -> tuple[bool, str]:
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_deny.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_scan_selftest() -> tuple[bool, str]:
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_scan.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_decide_selftest() -> tuple[bool, str]:
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_decide.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_inbox_selftest() -> tuple[bool, str]:
    """One live event per (session, kind) — and the folded ones are recorded.

    The card put one session on it three times (user, 2026-10-16: 「待处理事件一个
    会话出现了多条，为什么不能只使用最后一条呢」). The fold that fixes it must move the
    superseded event to `inbox/handled.jsonl`, because the ids on the card are the ack
    handles: an event that is hidden but never retired would keep counting in
    `pendingCount` and keep riding the heartbeat. The source pin below is the other
    half — the rule only matters if the wake gate applies it before it builds the card.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_inbox.py")])
    tail = (p.stdout or "").strip().splitlines()
    if p.returncode != 0:
        return False, (tail[-1] if tail else f"exit {p.returncode}")
    decide_src = open(os.path.join(ROOT, "bin", "decide.py"), encoding="utf-8").read()
    if "apply_fold(" not in decide_src or "fold_superseded_kinds" not in decide_src:
        return False, "bin/decide.py no longer folds superseded events before building the card"
    return True, (tail[-1] if tail else "ok")


def check_prompt_split() -> tuple[bool, str]:
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_prompt.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_notice_selftest() -> tuple[bool, str]:
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_notice.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_ensure_append_selftest() -> tuple[bool, str]:
    """Editing the manual must not make every wake carry it twice.

    The manual lives in the system prompt; editing `prompts/append.md` invalidates the
    canary record and the composer falls back to `full`, re-sending it in the message
    (the user noticed: 「怎么上条信息中把操作手册又重复了一遍」, 2026-10-16). The wake path
    now heals itself, and both directions of that decision are pinned in
    `selftest_ensure_append.py` — re-apply the recoverable case, and *not* retry a
    binary that cannot support the append.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_ensure_append.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_roll_selftest() -> tuple[bool, str]:
    """The supervisor must outlive its own context: compact before replacing.

    The user asked (2026-10-16) for a long conversation to be compacted rather
    than replaced — 「能不能不要开启新会话，而是删除总管会话的历史部分，然后进行压缩」.
    Rolling anyway silently ignores that; never falling back to rolling wedges the
    session (the 2026-10-15 incident). Both directions are pinned in
    `selftest_roll.py`.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_roll.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_liveness_selftest() -> tuple[bool, str]:
    """Liveness must be provable from the supervisor's own context.

    The probe the briefing used (`launchctl print gui/…`) cannot run here at all,
    so it could not report a dead watcher. `bin/liveness.py` derives liveness from
    the facility's own evidence (health freshness + watchd pid) and can heal;
    `selftest_liveness.py` pins that each half fails on its own evidence.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_liveness.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_prune_selftest() -> tuple[bool, str]:
    """`logs/` stays bounded without losing what a diagnosis needs.

    Two opposite failure modes: per-run logs pile up forever, and append-only
    logs (a live writer holds the path) grow without bound. `selftest_prune.py`
    pins that the first expire by age and the second are truncated in place —
    unlinking an append-only log would silently keep receiving writes.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_prune.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")

def check_dev_pr_selftest() -> tuple[bool, str]:
    """The dev-branch PR policy: two silent, expensive mistakes are pinned.

    Merging a red PR puts a broken `main` in front of every other session; and a
    *squash-merged* branch never has its own commits as ancestors of `main`, so
    "commits ahead" is not "unmerged" — treating it that way re-opens the same PR
    or re-points over work that has not landed. `selftest_dev_pr.py` pins both,
    plus the local-ahead case (never ours to push or reset).
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_dev_pr.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_lock_selftest() -> tuple[bool, str]:
    """The supervisor's single-flight lock has to actually exclude.

    It did not until 2026-10-16: `acquire` recorded its own short-lived pid, so
    the next turn saw a dead pid and stole the lock, and two supervisor sessions
    wrote the records and opened PRs at once. `selftest_lock.py` pins that a fresh
    hold blocks the next turn even though the holder's pid is gone.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_lock.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_send_selftest() -> tuple[bool, str]:
    """The timing rule for relays: never send into a session that is mid-run.

    A relay composed from a pre-run snapshot is read only *after* the target's
    run in flight, so it answers a superseded state (`docs/gotcha-send.md` A3
    measured 31 minutes late). The check also pins the absence of side effects
    on a refusal — recording one would burn the target's send quota for a message
    that was never delivered.
    """
    p = run([sys.executable, os.path.join(ROOT, "bin", "selftest_send.py")])
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else f"exit {p.returncode}")


def check_send_guard_invocation() -> tuple[bool, str]:
    """`bin/send.py` must run `bin/guard.py` with the interpreter, not via `future`.

    `send.run()` prepends the `future` binary to whatever it is given, so passing
    a python path to it executed `future python3 …/guard.py`. That stayed invisible
    while an unknown group printed the help and exited 0 (the guard silently passed
    every send); once PR #1107 made an unknown group an error, every send was
    refused with a blank `guard:` line. Pin the correct call shape here.
    """
    text = open(os.path.join(ROOT, "bin", "send.py"), encoding="utf-8").read()
    bad = 'run([sys.executable, os.path.join(WATCH, "bin", "guard.py")'
    if bad in text:
        return False, "bin/send.py passes guard.py to run() (which prefixes `future`)"
    if 'run_py([os.path.join(WATCH, "bin", "guard.py")' not in text:
        return False, "bin/send.py no longer calls guard.py through run_py()"
    return True, "guard.py is invoked with the interpreter"


def check_relay_format() -> tuple[bool, str]:
    """A relayed message must be framed by send.frame(), and counted by guard.

    The user asked (2026-10-16) for the supervisor's messages to render as proper
    Markdown: the old `[会话总管 …]` first line read as a stray bracketed token.
    He then asked for the *redundant fields* to go — the 56-character session id
    and the metadata blockquote that said `会话总管` a second time. Framing and
    *counting* are linked: if send stops emitting the heading, or the guard stops
    recognising it, the anti-loop limit silently loosens. `非用户本人` is pinned
    because it is the one field a target must not misread (its authority is this
    facility, not the user) and it was the easiest one to lose while trimming.
    """
    send_src = open(os.path.join(ROOT, "bin", "send.py"), encoding="utf-8").read()
    guard_src = open(os.path.join(ROOT, "bin", "guard.py"), encoding="utf-8").read()
    problems = []
    if "RELAY_HEADING = \"### 会话总管转达\"" not in send_src:
        problems.append("send.py lost the relay heading")
    if "text = frame(" not in send_src:
        problems.append("send.py no longer frames the body")
    if "非用户本人" not in send_src:
        problems.append("send.py dropped the not-the-user marker")
    if '"> **来自**' in send_src or "meta +=" in send_src:
        problems.append("send.py re-introduced the metadata blockquote")
    if "\"会话总管转达\"" not in guard_src:
        problems.append("guard.py does not count the new heading")
    if '"[会话总管"' not in guard_src:
        problems.append("guard.py dropped the legacy marker (old sends would uncount)")
    # Timing rule (user, 2026-10-16): a session that is still mid-run must not be
    # relayed into now — wait for the next wake. Losing this silently restores
    # the stale-snapshot sends that A3 documents.
    if "def busy(" not in send_src or "isStreaming" not in send_src:
        problems.append("send.py no longer checks whether the target is mid-run")
    if "return 3" not in send_src:
        problems.append("send.py no longer defers (distinct exit code) on a mid-run target")
    # One topic line only: the header carries it, so a body-level one must be
    # folded in rather than delivered twice (user, 2026-10-16).
    if "def take_body_topic(" not in send_src or "BODY_TOPIC" not in send_src:
        problems.append("send.py lost the body-topic fold (relays would show two 主题 lines)")
    # Never relay into a session the goal plane drives (user, 2026-10-16), and never
    # let that rule silence the supervisor's own channel.
    if "def goal_owned(" not in send_src or "GOAL_BOUNDARY_MARKER" not in send_src:
        problems.append("send.py no longer refuses goal-launched sessions")
    if "worker_sessions(" not in send_src:
        problems.append("send.py lost the exact goal-worker signal (the plane's own binding)")
    if "def worker_sessions(" not in open(os.path.join(ROOT, "bin", "goal_sessions.py"),
                                           encoding="utf-8").read():
        problems.append("bin/goal_sessions.py lost worker_sessions() — renamed workers would "
                        "wake the supervisor again (measured 2026-10-16)")
    if "supervisor_session()" not in send_src:
        problems.append("send.py lost the supervisor carve-out for the goal rule")
    # ... and the same rule must hold on the WAKE side (user, 2026-10-16: a goal
    # session must not wake the supervisor either). One definition, two gates:
    # `bin/goal_sessions.py` holds the detection, both scripts import it.
    if not os.path.exists(os.path.join(ROOT, "bin", "goal_sessions.py")):
        problems.append("bin/goal_sessions.py (the shared goal-session rule) is gone")
    scan_src = open(os.path.join(ROOT, "bin", "scan.py"), encoding="utf-8").read()
    if "skip_goal_session(" not in scan_src or "scan_skip_goal_sessions" not in scan_src:
        problems.append("bin/scan.py no longer skips goal sessions (workers would wake the supervisor)")
    if "supervisor_session()" not in scan_src:
        problems.append("bin/scan.py lost the supervisor carve-out (the supervisor must wake itself)")
    # The name signal alone misses a *renamed* worker, which is how two sessions of a
    # live goal kept waking the supervisor after the exemption shipped (2026-10-16).
    # The plane's own binding is the exact signal; scan.py must pass it through.
    if "worker_sessions(" not in scan_src or "goal_workers" not in scan_src:
        problems.append("bin/scan.py no longer uses the exact goal-worker binding")
    # Never *deliver* a message whose whole content is "take note" (user,
    # 2026-10-16: 「不需要会话再做任何事情的情况，那就不要发消息给会话了吧」). The
    # record belongs in journal/reports; a send would start a run in the target to
    # tell it to do nothing. Pinned here because a quiet gate that quietly stops
    # working is invisible: relays simply get delivered again.
    if "def quiet_intent(" not in send_src or 'QUIET_LABEL = "' not in send_src:
        problems.append("send.py lost the no-action awareness gate (quiet relays get delivered)")
    if "return 5" not in send_src:
        problems.append("send.py no longer refuses a no-action relay with its own exit code")
    # A new feature gets a new session (user, 2026-10-16): reusing one carries its
    # history into the work. `--new` is how the supervisor does that, and it needs
    # the `session status` fallback because a freshly created session is not in the
    # session list yet — losing either half silently returns to reusing sessions.
    if "def create_session(" not in send_src or "--new" not in send_src:
        problems.append("send.py lost `--new` (new features would reuse a session)")
    if "def resolve_cwd(" not in send_src or "read_state" not in send_src:
        problems.append("send.py lost the cwd fallback for a just-created session")
    return (not problems), ("; ".join(problems) if problems else
                            "relay framed + counted, no redundant fields, mid-run deferred, "
                            "no-action relays refused")


def check_no_secrets() -> tuple[bool, str]:
    """The repo is mirrored to GitHub: passwords/keys/tokens must never be committed."""
    tracks = run(["git", "ls-files"])
    files = [f for f in tracks.stdout.splitlines() if os.path.isfile(os.path.join(ROOT, f))]
    blanks = [f for f in files if "BLANK" in f or "占位" in f]
    patterns = [
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
        (r"\bgh[pousr]_[A-Za-z0-9]{16,}", "github token"),
        (r"\bsk-[A-Za-z0-9]{20,}", "api key"),
        (r"\bAKIA[0-9A-Z]{16}\b", "aws key"),
        (r"\b(?:password|passwd|secret|api_?key|token)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']", "inline credential"),
    ]
    hits = []
    for f in files + blanks:
        try:
            text = open(os.path.join(ROOT, f), encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for pat, label in patterns:
            if re.search(pat, text):
                hits.append(f"{f} ({label})")
    return (not hits), (", ".join(hits[:5]) if hits else f"{len(files)} tracked files clean")


def check_runtime_files_ignored() -> tuple[bool, str]:
    """Files that carry other sessions' text must stay out of git.

    On 2026-10-16 an edit to .gitignore dropped a newline, fusing
    `state/approvals.jsonl` and `state/msg-*.txt` into one rule. Path matching
    then stopped covering the message spool files, which hold other sessions'
    verbatim text — they showed up as untracked and a `git add -A` would have
    committed them. Probe the rules instead of trusting the file's shape.

    2026-10-16 (same class, found by use): `bin/notice.py` and `bin/directives.py`
    shipped tracked while their state files were never added to .gitignore, so the
    very first `notice.py add` left `state/notices.jsonl` untracked — one `git add -A`
    in a later round would have committed other sessions' verbatim text. Both paths
    are probed below.
    """
    probes = ["state/msg-anything.txt", "state/any-new-file.jsonl", "state/SNAPSHOT.md",
              "inbox/pending.jsonl", "inbox/archive/anything.jsonl",
              ".private/identity.md"]
    leaked = [p for p in probes if run(["git", "check-ignore", "-q", p]).returncode != 0]
    # Stronger than per-path rules: `state/` and `inbox/` are ignored wholesale
    # (2026-10-20), so the real assertion is that nothing under them is tracked.
    # Per-file rules had failed three separate times — each miss handed another
    # session's verbatim text to the next `git add -A`.
    tracked = run(["git", "ls-files", "state", "inbox"]).stdout.strip()
    leaked += [f"tracked: {line}" for line in tracked.splitlines() if line.strip()]
    return (not leaked), ("not ignored: " + ", ".join(leaked)) if leaked else "spool + state files ignored"


def check_private_not_tracked() -> tuple[bool, str]:
    """AGENTS.md §5: nothing under .private/ may ever be committed."""
    p = run(["git", "ls-files", ".private"])
    leaked = [f for f in p.stdout.splitlines() if os.path.basename(f) != "README.md"]
    ignored = run(["git", "check-ignore", "-q", ".private/probe"]).returncode == 0
    problems = []
    if leaked:
        problems.append("tracked: " + ", ".join(leaked[:5]))
    if not ignored:
        problems.append(".private is not gitignored")
    return (not problems), ("; ".join(problems) if problems else ".private ignored, nothing tracked")


def check_event_logs() -> tuple[bool, str]:
    """inbox/*.jsonl must stay parseable: scan.py and inbox.py append to them."""
    bad = []
    n = 0
    for dirpath, _, filenames in os.walk(os.path.join(ROOT, "inbox")):
        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    n += 1
                    try:
                        json.loads(line)
                    except Exception as exc:
                        bad.append(f"{os.path.relpath(path, ROOT)}:{i} {exc}")
    return (not bad), ("; ".join(bad[:3]) if bad else f"{n} events parse")


def check_owns_worktree() -> tuple[bool, str]:
    """Work must happen in .worktrees/<name>, never in the main checkout's tracked tree."""
    out = run(["git", "rev-parse", "--path-format=absolute", "--show-toplevel"]).stdout.strip()
    wt = "/.worktrees/" in out
    return True, f"{out} ({'worktree' if wt else 'main checkout — write only at turn end'})"


def check_no_local_identity() -> tuple[bool, str]:
    """No tracked file may name a home directory or a real launchd label.

    2026-10-18 (user: 「清理仓库中涉及本地信息或者不通用的部分」): the repo used to
    spell `/Users/<name>` in scripts, docs, prompts *and* records, and a launchd
    label that carried the account name. A clone then meant nothing anywhere else,
    and every commit carried one person's home directory.

    The rule that replaced it: **mechanism in git, instance facts in bin/config.json
    (`${WATCH}` for paths) or the environment.** Records keep the *shape* of what
    happened — `~/…`, `com.<user>.session-watch` — which is why placeholders are
    written rather than the lines being deleted: they are evidence, not config.

    **Deliberately host-independent.** The first version also failed on the
    current `$HOME` basename, which read as "the account name leaked" and was fine
    on this Mac — and then failed the PR on GitHub's runner, where the account is
    `runner` and two records legitimately say "shared CI runner". A check whose
    verdict depends on who runs it is not a check, so the rule is absolute paths
    plus a label that is not the placeholder: both mean the same thing on every
    host.
    """
    patterns = [(r"/Users/[A-Za-z0-9._-]+", "home path"),
                (r"/home/[A-Za-z0-9._-]+", "home path"),
                (r"com\.[A-Za-z0-9_-]+\.session-watch", "real launchd label")]
    files = run(["git", "ls-files"]).stdout.splitlines()
    hits = []
    for name in files:
        try:
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        for pat, what in patterns:
            m = re.search(pat, text)
            if m:
                hits.append(f"{name}:{text[:m.start()].count(chr(10)) + 1} {what} {m.group(0)}")
                break
    return (not hits), ("; ".join(hits[:5]) if hits
                        else f"{len(files)} tracked files carry no home path or real label")


def check_instance_state_untracked() -> tuple[bool, str]:
    """Workspace-local state stays out of git — the `FUTURE.md` rule.

    `FUTURE.md` / `.future/memory/` are this deployment's knowledge store (framework
    knowledge lives in AGENTS.md + docs/; everything machine-specific lives there).
    They stay on disk and out of git. The same class:
    the goal control plane's runtime (absolute cwd, per-turn events, 0-byte lock
    files), the snapshot `bin/state_snapshot.py` rewrites every turn, and the
    one-off local backups. **All of them stay on disk and keep working**; none of
    them belong in history, where they only add per-turn diff and machine facts.
    """
    probes = [".future/goal/registry.json", ".future/memory/anything.md", "FUTURE.md",
              "state/SNAPSHOT.md", "state/first_wake_verified",
              "state/task-daily-prompt.original.md",
              "state/tasks-zombie-runs-backup-2026.txt"]
    tracked = set(run(["git", "ls-files"]).stdout.splitlines())
    problems = [f"tracked: {p}" for p in probes if p in tracked]
    problems += [f"not ignored: {p}" for p in probes
                 if run(["git", "check-ignore", "-q", p]).returncode != 0]
    return (not problems), ("; ".join(problems) if problems
                            else "instance state (goal runtime, snapshot, local backups) untracked")


def check_toolchain_not_hardcoded() -> tuple[bool, str]:
    """`future` / `python3` are discovered, not spelled per machine.

    Twelve scripts used to open with the same Homebrew path. That ran on exactly
    one kind of Mac and put a machine fact in every file; discovery now lives in
    `bin/toolpath.py`, which is the only place allowed to name candidate prefixes.
    The shell entry points may list them only inside their `for d in …` fallback.
    """
    bad = []
    bin_dir = os.path.join(ROOT, "bin")
    # toolpath.py owns the candidate list; this check spells the pattern it looks for.
    exempt = {"toolpath.py", "selfcheck.py"}
    for name in sorted(os.listdir(bin_dir)):
        if name in exempt or not name.endswith((".py", ".sh")):
            continue
        try:
            with open(os.path.join(bin_dir, name), encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if "/opt/homebrew" not in line and "/usr/local/bin" not in line:
                continue
            if name.endswith(".sh") and "for d in" in line:
                continue
            if name.endswith(".py") and line.lstrip().startswith("#"):
                continue
            bad.append(f"bin/{name}:{i}")
    return (not bad), ("; ".join(bad[:5]) if bad
                       else "toolchain resolved in bin/toolpath.py only")


def check_imports_complete() -> tuple[bool, str]:
    """Every stdlib name a script *uses* is imported — a NameError here is silent.

    Found twice on 2026-10-18 within minutes: `bin/state_snapshot.py` used `sys`
    (added by a mechanical edit) and `bin/guard.py` had used `subprocess` for days
    without importing it. `py_compile` cannot see either, and guard's own
    `except Exception: return (0, 0)` turned its transcript cross-check into a
    permanent zero — a guard that always says "no earlier sends". A check that
    cannot fail again.

    Parsed with `ast`, not grepped: prose in docstrings says "second time." and
    would match `time.`, and code may import inside a function.
    """
    import ast

    mods = {"sys", "os", "json", "re", "time", "subprocess", "shutil", "pathlib",
            "argparse", "sqlite3", "tempfile", "glob", "shlex", "hashlib", "urllib",
            "dataclasses", "typing", "collections", "itertools", "datetime", "io",
            "uuid", "random", "traceback", "signal", "textwrap", "socket", "stat"}
    bad = []
    bin_dir = os.path.join(ROOT, "bin")
    for name in sorted(os.listdir(bin_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(bin_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=name)
        except SyntaxError:
            continue  # py_compile already reports it
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {a.asname or a.name for a in node.names}
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in mods}
        missing = used - imported
        if missing:
            bad.append(f"bin/{name}: {','.join(sorted(missing))}")
    return (not bad), ("; ".join(bad[:6]) if bad else "every used stdlib name is imported")


def upstream() -> str:
    """`<owner>/<repo>` of this checkout's origin, or a match-nothing string.

    It used to be written out literally, which made the probe *this machine's*
    rule rather than a rule: another deployment would inherit a check that can
    only ever pass here, and a renamed repo would silently stop being probed.
    Reading it from git keeps the assertion ("no tracked file names the upstream")
    while the name itself stays an instance fact.
    """
    out = run(["git", "config", "--get", "remote.origin.url"]).stdout.strip()
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", out)
    return m.group(1) if m else r"(?!)"


def check_docs_are_framework_only() -> tuple[bool, str]:
    """`AGENTS.md` and `docs/` describe the framework, not this deployment.

    The split (user request): anything that is only true of one machine, one user or
    one incident lives in `.future/memory/` behind the `FUTURE.md` index; the files
    a *different* deployment would also read must stay free of it. What that means in
    practice is mechanical: no dated provenance (`2026-10-18 …`), no upstream repo
    name, no absolute home path. Placeholders (`<名>`, `~/…`) are fine — the rule
    itself has to say `/Users/<名>` to be a rule.
    """
    names = ["AGENTS.md"] + sorted(
        os.path.join(rel, f) for rel, _, fs in os.walk(os.path.join(ROOT, "docs"))
        for f in fs if f.endswith(".md"))
    problems = []
    for rel in names:
        path = os.path.join(ROOT, rel)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for pattern, what in ((r"20\d\d-\d\d-\d\d", "dated provenance"),
                              (re.escape(upstream()), "upstream repo name")):
            if re.search(pattern, text):
                problems.append(f"{rel}: {what}")
    return (not problems), ("; ".join(problems[:6]) if problems
                            else "AGENTS.md + docs/ carry no dated provenance")


CHECKS = [
    ("python syntax", check_python_syntax),
    ("imports complete", check_imports_complete),
    ("shell syntax", check_shell_syntax),
    ("json", check_json),
    ("guard selftest", check_guard_selftest),
    ("send guard invocation", check_send_guard_invocation),
    ("relay format", check_relay_format),
    ("scan selftest", check_scan_selftest),
    ("decide selftest", check_decide_selftest),
    ("inbox fold selftest", check_inbox_selftest),
    ("prompt split", check_prompt_split),
    ("notice/directives selftest", check_notice_selftest),
    ("send selftest", check_send_selftest),
    ("roll selftest", check_roll_selftest),
    ("lock selftest", check_lock_selftest),
    ("prune selftest", check_prune_selftest),
    ("dev-pr selftest", check_dev_pr_selftest),
    ("liveness selftest", check_liveness_selftest),
    ("ensure-append selftest", check_ensure_append_selftest),
    ("no secrets in tracked files", check_no_secrets),
    ("private dir ignored", check_private_not_tracked),
    ("runtime files ignored", check_runtime_files_ignored),
    ("no local identity in tracked files", check_no_local_identity),
    ("instance state untracked", check_instance_state_untracked),
    ("toolchain not hardcoded", check_toolchain_not_hardcoded),
    ("docs are framework-only", check_docs_are_framework_only),
    ("event logs parse", check_event_logs),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the slow checks")
    args = ap.parse_args()
    print(f"selfcheck @ {ROOT}")
    results = [check(name, fn) for name, fn in CHECKS]
    if not args.quick:
        check("own worktree", check_owns_worktree)
    failed = results.count(False)
    print(f"checks={len(results)} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())