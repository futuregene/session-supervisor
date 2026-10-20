#!/usr/bin/env python3
"""Send a message into another session without ever touching its cwd.

Wraps `future run --session <id> --cwd <the target's own cwd> "<text>"` in the
background with a timeout, records it in the anti-loop guard, and logs the result.

Two sessions are never relayed into:

- one that is still mid-run (see `busy()`): the relay would be based on a snapshot
  that is already stale, and it would only be read after the run in flight
  finishes. Wait for the next wake and send then, with the new progress actually
  in hand (user, 2026-10-16);
- one started by the goal control plane (`future goal`) — see `goal_owned()`. It is
  driven by its own loop and the user does not want relay traffic reaching it
  (2026-10-16). The supervisor session itself is carved out.

And no message is sent whose whole content is "take note": if the target has
nothing to change or execute, the record belongs in journal/reports, not in the
target's context — a send there starts a run and spends the user's credits to tell
an agent to do nothing (see `quiet_intent()`; user, 2026-10-16).

`--force` overrides all of these, for a genuine stop order/correction.

A **new feature** gets a **new session**, not a reused one (user, 2026-10-16:
「如果是某一个仓库的新功能，不要沿用这个仓库下面已有的会话，而是要新开一个会话来实现，
这样就没有上下文的历史包袱」). Reusing a session carries its whole history into the new work —
the agent pays for it on every request and misreads it as context. So:

  send.py --new --cwd <repo> --name "<the feature>" <text-file>

creating the session is this script's job precisely because a brand-new session is not
in the session list yet, and resolving its cwd is the one step that needs the agent.

  send.py <session-id> <text-file> [--timeout 1800] [--note "..."] [--wait] [--force]
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# One definition of "a goal session" for both gates (this relay and `bin/scan.py`).
import goal_sessions  # noqa: E402

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path, bash_exe  # noqa: E402
FUTURE = future_exe()
# The relay is a bash one-liner (`$(cat ...)`, `timeout`), so it needs a real bash.
# On Windows that is Git Bash, which is not on PATH by default — resolve it like
# the other tools instead of assuming the spelling works everywhere.
BASH = bash_exe() or "bash"
LOGDIR = os.path.join(WATCH, "logs")
# A relayed message starts with this heading (see `frame()`); the legacy form
# `[会话总管 <id> 转达] …` is still recognised so old sends keep being counted.
RELAY_HEADING = "### 会话总管转达"
LEGACY_PREFIX = re.compile(r"^\[会话总管[^\]]*\]\s*")
# A topic line written *into the body* by mistake. The header already carries one
# (`**主题** <note>`), so a body-level line produced two of them in the delivered
# message (user, 2026-10-16: 「怎么有两行主题，一行就行了」). Accept both the
# `**主题** x` and plain `主题：x` spellings, with or without a colon.
BODY_TOPIC = re.compile(r"^\s*(?:\*\*)?主题(?:\*\*)?\s*[：:]?\s*(?P<text>\S.*?)\s*$")
# Phrases that *contain* a quiet marker but mean the opposite: they tell the target
# to stop asking and get on with it. Without this list, a directive opening with
# 「不用再问了，直接派 X」 is read as "nothing to do" and refused (measured
# 2026-10-19: a real go-ahead was refused once before the wording was changed).
ANTI_QUIET = ("不用再问", "不要再问", "不必再问", "别问我", "不用等我", "不必等我",
              "别等我", "不需要你回答", "不用回复我")
QUIET = ("只知悉", "不需要你执行", "不必执行", "不必回复", "无需回复", "不用回复",
         "不需要你", "不需要做", "不需要再", "不用做", "不用再", "不必做", "不必再",
         "不必重开", "不用重开", "不要重开", "不用管", "已过期",
         "保持现状", "保持可见", "不要动", "不用动", "到此收尾",
         "无需动作", "无待办", "不需要任何动作")
# The intent label `frame()` derives from those phrases. `quiet_intent()` reads the
# framed first line, so the gate and the header a target sees cannot disagree.
QUIET_LABEL = "只知悉 · 不必回复"
# The goal plane appends this boundary to a worker session's system prompt
# (`orchestration/goal/src/decision/goal_boundary.rs`). Since append settings are
# recorded with the session and readable (`future session status <id> --json`), it
# is the one in-band marker that identifies a goal-launched session no matter
# where the goal lives.
#
# Since 2026-10-16 the detection itself lives in `bin/goal_sessions.py`, because
# the scanner needs the same answer (the user asked that goal sessions not wake
# the supervisor either). These names stay as aliases so the documented API of
# this module — and the pins in `bin/selfcheck.py` — keep meaning what they say.
GOAL_BOUNDARY_MARKER = goal_sessions.BOUNDARY_MARKER
# Where a goal's state may live, for the weaker name-based fallback: goals predate
# the recorded append, so their worker sessions carry no marker. Each root is
# checked for `.future/goal` and the legacy `.future/loop`.


def _cfg_bool(key, default):
    """`bin/config.json` value for `key`, or `default` when absent/unreadable.

    Read at import: a relay run is a short-lived process, and the switch only
    governs relaying — so it stays cheap to flip (no code change, no restart).
    """
    try:
        with open(config_path(), encoding="utf-8") as fh:
            value = json.load(fh).get(key, default)
    except (OSError, ValueError):
        return default
    return value if isinstance(value, bool) else default


# `relay_skip_goal_sessions: true` (the default) means "do not relay into a goal
# worker". Named for what it *does*: the earlier `GOAL_TARGET_ALLOWED = not …`
# read as its own opposite and shipped inverted once.
SKIP_GOAL_SESSIONS = _cfg_bool("relay_skip_goal_sessions", True)


def run(args, timeout=60):
    env = dict(os.environ)
    env["PATH"] = tool_path()
    p = subprocess.run([FUTURE] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def read_state(sid):
    """One `session status` call, shared by both send guards: `(state, ok)`.

    `ok` is False when the state cannot be read — the callers warn and proceed
    rather than treating "unknown" as "idle" (silently sending) or as "blocked"
    (silencing the relay every time the agent restarts).
    """
    rc, out, err = run(["session", "status", sid, "--json"])
    if rc != 0:
        print(f"note: cannot read the run state of {sid} ({err.strip()[:120]}); "
              "sending anyway — check the target yourself")
        return None, False
    try:
        return json.loads(out), True
    except ValueError:
        print(f"note: unparseable run state for {sid}; sending anyway")
        return None, False


def busy(state):
    """`True` when the target is still mid-run, `None` when it cannot be told.

    Why refuse instead of enqueueing: the default `busy_policy` is
    `enqueue_if_busy`, so the text lands *after* the run in flight. The message was
    written from what the scanner had seen *before* that progress, so by the time
    it is read it is answering a superseded state — and the target acts on the
    older instruction (docs/gotcha-send.md A3 measured exactly this: a correction
    queued behind a 34-minute run landed 31 minutes after the merge it meant to
    stop). Waiting for the next wake costs one scan interval and produces a
    message written against the real latest progress.

    `None` is not `False`: an unreadable state must not silently pass for "idle".
    """
    return None if state is None else state.get("isStreaming") is True


def goal_objectives(roots=None):
    """Objectives of the goals whose state lives under the configured roots."""
    return goal_sessions.objectives(roots)


def goal_owned(sid, status=None, roots=None, extra_roots=(), workers=None):
    """`(True, why)` when the target was started by `future goal`.

    Why refuse: a goal worker is driven by its own control plane (one todo per
    turn, claims and leases). Relaying into it mixes a second driver into that
    loop, and the user asked for it to stop (2026-10-16). Nothing is lost by
    waiting: findings worth acting on belong in the goal's own ledger or with the
    user, not as a chat turn inside a worker.

    Detection lives in `bin/goal_sessions.py` (shared with `bin/scan.py`). Three
    signals, strongest first: the plane's own `worker_session_bound` record (exact;
    survives a rename), the goal boundary in the session's system prompt, and the
    session name being the objective. `extra_roots` carries the target's own cwd,
    because a goal's registry sits in the goal's state root — which is where its
    workers run, including a worktree the fixed roots do not cover.
    """
    if workers is None:
        workers = goal_sessions.worker_sessions(roots=roots, extra_roots=extra_roots)
    return goal_sessions.owned(status, roots=roots, extra_roots=extra_roots,
                               workers=workers, sid=sid)


def supervisor_session():
    """The id of the live supervisor conversation, or `""`.

    Carved out of the goal rule: the supervisor runs under a goal of its own
    (it runs under a goal of its own), so a mis-tagged session must never silence the relay
    that the whole facility depends on.
    """
    return goal_sessions.supervisor_session()


def run_py(args, timeout=120):
    """Run a local script with the same interpreter.

    `run()` prepends the `future` binary to every argument list, so using it for
    `bin/guard.py` actually executed `future python3 …/guard.py`. That used to be
    harmless only by accident: before PR #1107 an unknown group printed the help
    and exited 0, so the guard silently "passed" for every send (i.e. the
    anti-loop limit was not being enforced at all). Once #1107 made an unknown
    group an error, the same call returned 1 with an empty stdout, and every send
    was refused with `guard: ` (blank) — a misleading block.
    """
    env = dict(os.environ)
    env["PATH"] = tool_path()
    p = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def take_body_topic(body):
    """Pop a leading `主题…` line out of the body; return `(topic, body)`.

    The relay header already states the topic (`**主题** <note>`), so a topic line
    left in the body is always a second one — the delivered message looked like
    an accident with two.

    When no `--note` was given the body's line is *promoted* to the topic instead
    of being deleted: the author meant it as the subject, and silently dropping it
    would lose the one line a reader scans first.
    """
    first, sep, rest = body.partition("\n")
    match = BODY_TOPIC.match(first)
    if not match:
        return "", body
    return match.group("text").rstrip("* "), (rest.lstrip() if sep else "")


def frame(text, sid, note="", intent=None):
    """Wrap the body in the standard relay header.

    The message lands in another agent session and is rendered as Markdown, so a
    `[会话总管 …]` square-bracket prefix reads as a stray link-ish token. A level-3
    heading carries everything the target actually has to act on and keeps the
    words `会话总管转达` in the first line, which is what `guard.py` counts.

    Two fields were dropped on the user's report (2026-10-16: 「在 markdown 下…
    多余的字段，能删则删」): the 56-character session id, which no target session
    does anything with, and the metadata blockquote that repeated `会话总管` a
    second time. Hiding `非用户本人` inside a quote also invited skimming past it,
    and it is the one fact a target must not misread — whether its authority is
    the *user* or this facility. It rides in the heading now.

    A topic line left in the *body* is folded into the header rather than shown
    twice (same report: 「怎么有两行主题，一行就行了」) — see `take_body_topic`.
    """
    body = text.lstrip()
    if body.startswith(RELAY_HEADING):
        return body  # already framed: never nest two headers
    rest = ""
    m = LEGACY_PREFIX.match(body)
    if m:
        first, _, tail = body[m.end():].partition("\n")
        rest, body = first.strip(), tail.lstrip()
    body_topic, body = take_body_topic(body)
    if not note:
        note = body_topic  # promote, don't discard
    if intent is None:
        # Probe the *opening line* only: a body that asks for work and mentions
        # "keep as is" three paragraphs later is still a message that asks for
        # work. Falls back to the head of the body when there is no line break.
        probe = rest or body.split("\n", 1)[0][:120]
        # Strip the anti-quiet phrases before scanning, so each one neutralises only
        # itself: 「不用再问了，保持现状。」 keeps its 保持现状 (a real "nothing to do").
        for k in ANTI_QUIET:
            probe = probe.replace(k, "")
        quiet = any(k in probe for k in QUIET)
        intent = QUIET_LABEL if quiet else "需要你执行"
    first_line = f"{RELAY_HEADING} · {intent} · 非用户本人"
    if not note:
        return f"{first_line}\n\n{body}"
    return f"{first_line}\n\n**主题** {note}\n\n---\n\n{body}"


def quiet_intent(framed):
    """True when a framed relay only tells the target to *take note*.

    A relay exists to make the target change what it does. When the conclusion is
    "nothing further is needed" — an acknowledgement, a question the user already
    answered, a decision *not* to take an action — the target's next move is the
    same as its current one, and the send buys nothing while costing a full run in
    that session plus the user's credits (user, 2026-10-16: 「不需要会话再做任何事情
    的情况，那就不要发消息给会话了吧」). That case is recorded in `journal/`,
    `reports/decisions/` and, when the user should know, `bin/notice.py`.

    Read the intent off the framed first line rather than re-deriving it, so the
    header the target would see and the gate that refused it cannot disagree.
    """
    return QUIET_LABEL in framed.split("\n", 1)[0]


def resolve_cwd(sid):
    """The target's own cwd, or `(None, why)` when it cannot be trusted.

    `session info` is the documented reader, but a session created by
    `future session new` is not in the session list until its first run, so
    `info` answers "Session not found" for exactly the case `--new` produces.
    `session status` does answer for it (it reports the live session, not the
    stored row), so fall back to that before giving up. Never guess from the
    caller's directory: `future run --cwd` defaults to the *caller's* cwd, which
    is how two earlier relays rewrote their target's cwd and workspace.
    """
    rc, out, err = run(["session", "info", sid, "--json"])
    if rc == 0:
        try:
            cwd = json.loads(out).get("cwd")
        except ValueError:
            cwd = None
        if cwd:
            return cwd, ""
    state, ok = read_state(sid)
    if ok:
        cwd = (state or {}).get("cwd")
        if cwd:
            return cwd, ""
        return None, f"session status reports no cwd for {sid}"
    return None, f"cannot read {sid}: {err.strip()[:200]}"


def create_session(repo_cwd, name):
    """`(session_id, "")` for a fresh session in `repo_cwd`, or `(None, why)`.

    A new feature is dispatched into its own session so the work starts without the
    other sessions' history (user, 2026-10-16). `--cwd` is set here rather than
    inherited for the reason in `resolve_cwd`.
    """
    if not repo_cwd or not os.path.isdir(repo_cwd):
        return None, f"--cwd must be an existing directory (got {repo_cwd!r})"
    args = ["session", "new", "--cwd", repo_cwd]
    if name:
        args += ["--name", name]
    args.append("--json")
    rc, out, err = run(args)
    if rc != 0:
        return None, f"session new failed: {err.strip()[:200]}"
    try:
        return json.loads(out)["sessionId"], ""
    except (ValueError, KeyError):
        return None, f"session new reply unparseable: {out[:200]}"


def parse_opt(argv, flag, default=""):
    """Value of `--flag <value>` in argv, or `default`."""
    return argv[argv.index(flag) + 1] if flag in argv else default


DEFERRED_PATH = os.path.join(WATCH, "state", "deferred-sends.jsonl")
DEFERRED_TTL_HOURS = 24


def defer(sid, text_file, note, reason):
    """Queue a refused relay so the next wake actually retries it.

    A refusal is *timing*, not a verdict on the message: the target was streaming,
    or the anti-loop guard had no budget left this hour. Either way `send.py` used
    to exit with zero side effects, and the body lived only in whatever file the
    caller happened to write — nobody reads those, so the message silently never
    arrived (docs/gotcha-send.md A21; the 2026-10-17 case was a decision the target
    was still waiting for).

    Recording it here is the fix, and doing it *inside* the refusing paths is what
    makes it reliable: the retry is then the card's job (`turn_prompt._pending_sends`),
    not the operator's memory. Same `(session, file)` is updated in place, so three
    blocked attempts in a row still leave exactly one line to retry.

    Returns the queued row (for the caller's message and for the selftest).
    """
    now = datetime.datetime.now().astimezone()
    row = {
        "ts": now.isoformat(timespec="seconds"),
        "session": sid,
        # An absolute path stays valid no matter which cwd the next wake runs in.
        "file": os.path.abspath(text_file),
        "note": note or "",
        "reason": reason,
        "expires": (now + datetime.timedelta(hours=DEFERRED_TTL_HOURS)).isoformat(timespec="seconds"),
    }
    rows = []
    try:
        with open(DEFERRED_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # a corrupt line must not cost the whole queue
    except OSError:
        rows = []
    key = (row["session"], row["file"])
    kept = [r for r in rows if (r.get("session"), r.get("file")) != key]
    kept.append(row)
    os.makedirs(os.path.dirname(DEFERRED_PATH), exist_ok=True)
    with open(DEFERRED_PATH, "w", encoding="utf-8") as fh:
        for r in kept:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return row


def _queue_key(session, path):
    """Identity of a queued relay: its session plus one body file.

    The path is normalised through `WATCH` because rows written before 2026-10-18
    stored a *relative* path (`state/msg-…-PENDING.md`, hand-appended by the
    operator) while `defer()` stores an absolute one. Comparing the raw strings
    left such a row in place after its relay was delivered — the card then nagged
    about work already done, once per wake, until the TTL expired.
    """
    if not path:
        return (session, "")
    return (session, os.path.abspath(os.path.join(WATCH, path)))


def resolve_deferral(sid, text_file):
    """Drop the queue row for a relay that is now actually sent.

    The other half of `defer`: a row left behind after a successful send keeps the
    next card nagging about work already done, and the card's budget is paid on
    every wake. Best-effort — a queue file that cannot be rewritten must not fail
    a send that already happened.
    """
    target = _queue_key(sid, text_file)
    try:
        with open(DEFERRED_PATH, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return
    kept = [r for r in rows if _queue_key(r.get("session"), r.get("file")) != target]
    if len(kept) == len(rows):
        return
    try:
        with open(DEFERRED_PATH, "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main_new(argv):
    """`--new --cwd <repo> [--name <title>] <text-file> [--note …]`: start, then send.

    Kept separate from `main()` because the semantics differ in one way that
    matters: there is no pre-existing session to protect, so the cwd is given
    explicitly instead of resolved. The arguments are re-built explicitly rather
    than filtered, so no flag can be silently dropped on the way through.
    """
    repo_cwd = parse_opt(argv, "--cwd")
    name = parse_opt(argv, "--name")
    note = parse_opt(argv, "--note")
    known = {"--new", "--cwd", repo_cwd, "--name", name, "--note", note}
    positional = [a for a in argv if a not in known and not a.startswith("--")]
    if not positional:
        print("usage: send.py --new --cwd <repo> [--name <title>] [--note <topic>] <text-file>")
        return 2
    text_file = positional[0]

    sid, why = create_session(repo_cwd, name)
    if not sid:
        print(f"cannot create the new session: {why}")
        return 1
    print(f"created session {sid} in {repo_cwd}" + (f" \u300c{name}\u300d" if name else ""))
    # From here it is an ordinary relay: the session is idle, has no relay history,
    # and is not a goal worker, so it goes through the same framing, guard
    # accounting and logging as any other message.
    forwarded = [sid, text_file]
    if note:
        forwarded += ["--note", note]
    if "--force" in argv:
        forwarded.append("--force")
    if "--wait" in argv:
        forwarded.append("--wait")
    return main(forwarded)


def main(argv):
    if "--new" in argv:
        return main_new(argv)
    if len(argv) < 2:
        print(__doc__)
        return 2
    sid, text_file = argv[0], argv[1]
    timeout = 1800
    note = ""
    intent = None
    wait = "--wait" in argv
    if "--timeout" in argv:
        timeout = int(argv[argv.index("--timeout") + 1])
    if "--note" in argv:
        note = argv[argv.index("--note") + 1]
    if "--intent" in argv:
        intent = argv[argv.index("--intent") + 1]

    # The awareness gate runs first and touches nothing: no session lookup, no
    # guard record, no send budget. Refusing *is* the point — the target has
    # nothing to do, so the message must not become a run in its context.
    text = frame(open(text_file, encoding="utf-8").read(), sid, note, intent)
    if quiet_intent(text) and "--force" not in argv:
        print(f"refused: this relay asks {sid} to do nothing (intent = {QUIET_LABEL}). "
              "A send would start a run there and spend the user's credits to say "
              "'take note'. Record it in journal/ and reports/decisions/ instead "
              "(use bin/notice.py when the user should know); the pending event "
              "still gets acked. Pass --force only for a real stop order — when the "
              "target is already acting and must be interrupted.")
        return 5

    rc, out, err = run(["session", "info", sid, "--json"])
    log = os.path.join(LOGDIR, f"send-{sid}-{time.strftime('%H%M%S')}.log")
    cwd, why = resolve_cwd(sid)
    if not cwd or not os.path.isdir(cwd):
        print(f"cannot resolve the target's own cwd ({why or cwd!r}); refusing to send "
              "(a wrong --cwd would rewrite the session's cwd and workspace)")
        return 1

    # Both guards come before the quota check: a refused relay must not consume
    # the target's send budget, and the operator should see the reason without a
    # reassuring `guard: OK` above it.
    state, _known = read_state(sid)
    if SKIP_GOAL_SESSIONS and sid != supervisor_session():
        owned, why = goal_owned(sid, state, extra_roots=[cwd])
        if owned and "--force" not in argv:
            print(f"refused: {sid} was started by `future goal` ({why}); not sending. "
                  "Goal workers are driven by their own control plane, and the user "
                  "asked for no relay traffic to reach them. Record the finding in "
                  "journal/reports instead, or pass --force for a real stop order.")
            return 4
    mid_run = busy(state)
    if mid_run and "--force" not in argv:
        # Not an error in the message, an error in the *timing*: say what to do.
        defer(sid, text_file, note, "target mid-run")
        print(f"deferred: {sid} is still mid-run; not sending now. The relay would "
              "answer a state the target has already moved past, and it would only "
              "be read after the run in flight. Queued in state/deferred-sends.jsonl "
              "so the next wake retries it; the pending event stays open. Pass "
              "--force for a real stop order.")
        return 3

    grc, gout, gerr = run_py([os.path.join(WATCH, "bin", "guard.py"), "check", sid])
    # Show the verdict, and the reason when there is one: a blank `guard:` line
    # (empty stdout from a crashing or misdispatched check) must never be the
    # only thing the operator sees on a refusal.
    print(f"guard: {(gout or gerr).strip()[:300]}")
    if grc != 0 and "--force" not in argv:
        defer(sid, text_file, note, f"guard blocked (exit {grc})")
        print("blocked by the anti-loop guard "
              f"(exit {grc}); queued so the next wake retries it once the send "
              "window rolls. Pass --force only if this is a real correction.")
        return 1

    framed_file = os.path.join(WATCH, "state", "relay-out.txt")
    os.makedirs(os.path.dirname(framed_file), exist_ok=True)
    with open(framed_file, "w", encoding="utf-8") as fh:
        fh.write(text)
    script = (f"cd {json.dumps(os.path.dirname(text_file))} && "
              f"timeout {timeout} {FUTURE} run --session {sid} --cwd {json.dumps(cwd)} "
              f"\"$(cat {json.dumps(framed_file)})\" "
              f">> {json.dumps(log)} 2>&1; echo \"exit:$?\" >> {json.dumps(log)}")
    if wait:
        subprocess.run([BASH, "-c", script], timeout=timeout + 60)
    else:
        subprocess.Popen([BASH, "-c", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    run_py([os.path.join(WATCH, "bin", "guard.py"), "record", sid, note or "send.py"])
    resolve_deferral(sid, text_file)
    print(f"sent to {sid} (cwd kept = {cwd}); log {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))