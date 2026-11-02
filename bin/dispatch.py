#!/usr/bin/env python3
"""The dispatches I opened and still owe a follow-up on.

Why this exists (user, 2026-10-25):

> 「如果一个会话结束后需要后续跟进，并且跟进的事项可以另开会话的话，或者需要开新会话探索的话，
> 请你积极开更多会话，在多个会话中完成，并跟进所有这些会话」

Opening sessions is the easy half; the failure mode is that a session I opened in one wake is
never looked at again — its `new_session`/`awaiting_user` events arrive on a later wake with no
hint that *I* asked for it, so it reads like any other session and gets acked. `state/sent.jsonl`
records what I said but nothing about what I am waiting for, and `send.py --new` never recorded
anything at all.

So a dispatch row carries what a wake needs to judge it:

    {"ts", "session", "title", "accept", "repo", "note", "closed", "closed_why", "closed_at",
     "close_when"}

- `accept` is the point: "what result would let me close this row". A row without one is a row
  nobody can judge, so `add` refuses it. It is prose, read by a person or a model.
- `close_when` is the same judgement in a form a script can check: an optional spec that `list`
  evaluates **now** and reports as `closable: true | false | "unknown"`. It is a hint for the
  reader, never an actuator:

      pr-merged:<owner>/<repo>#<n>   the pull request is merged
      no-diff:<path>                that path has no uncommitted change (a file or a directory;
                                    a relative path needs the row's `repo`)
      branch-empty:<branch>         recognised and stored, but **not decided** here: a squash
                                    merge leaves the branch's commits outside the baseline, so
                                    ancestry keeps saying "unmerged" after the content landed.
                                    Use `pr-merged` instead.

  Anything else is stored verbatim and judged `unknown` — `add` never refuses or rewrites a spec,
  because the fact worth keeping is that someone meant to check it. Evaluating is read-only (`gh pr
  view` is a GET; `git status` runs with `GIT_OPTIONAL_LOCKS=0`, so it cannot rewrite the index),
  one failure marks that row `unknown` with a reason and never hides the others, and a row with no
  spec costs nothing — no subprocess at all.

- `bin/send.py --new` appends a row itself, so a dispatch cannot be created without being tracked.
- `bin/turn_prompt.py` renders the open rows into every wake card, which is the whole mechanism:
  the card is the only thing that reliably reaches a later me.

**`closable: true` never closes anything.** `close` stays the explicit act of a person or a wake;
this field only puts the judgement in front of whoever is reading instead of in their memory.
There is no auto-close behind it and no plan for one.

**Unknown flags are an error.** argparse rejects them (exit 2, usage on stderr); this
script never invents a lenient second parser, because a silently dropped flag is how a
restricted call becomes a real action (see `bin/send.py`).

**`add` is gated by `action_class`.** An event from `bin/trigger.py` carries one, and only
`act` may open work: `--action-class act` (or `--event <id>`, which reads the class from the
inbox record and cannot be argued around) is required for a dispatch that belongs to such an
event. `observe`, `triage`, an empty or an unknown value is refused with exit 6 and no row
written. A call with neither flag is the manual path and behaves exactly as it always did.
Closing and acking are never gated: recording what happened is exactly what a read-only
event is allowed to produce.

Rows are append-only; `close` appends a tombstone row with the same session, and `open_rows`
takes the last row per session. That keeps the file a log (auditable, and a mis-close is one
more row away) instead of a mutable table that can be corrupted by two writers.

Usage:
  dispatch.py add --session <id> --title "…" --accept "…" [--repo <path>] [--note "…"]
                   [--close-when <spec>]
  dispatch.py list [--json] [--all]      # computes closable, writes nothing
  dispatch.py close <session-id> [--why "…"]
  dispatch.py prune [--days N]        # drop closed rows older than N days (default 14)
  dispatch.py --selftest
"""
import argparse
import datetime
import json
import os
import sys

# The action gate, spelled the same way as in `bin/send.py` (that file carries the long
# reasoning): only `act` writes, everything else — `observe`, `triage`, an empty value, a
# value this build has never seen — is refused before anything is written.
ACTION_WRITE = "act"
#: Refusal because of the action class, distinct from 1 (unknown row) and 2 (bad usage).
EXIT_ACTION = 6


def event_action(event_id):
    """The `action_class` the inbox recorded for that event; None when it is not there."""
    import inbox
    for path in (inbox.PENDING, inbox.HANDLED):
        for row in inbox.read_lines(path):
            if row.get("id") == event_id:
                return (row.get("detail") or {}).get("action_class")
    return None


def action_refusal(action, event_id=None):
    """One sentence when this call may not open work; None when it may (see send.py)."""
    if event_id:
        recorded = event_action(event_id)
        if recorded is None:
            return (f"refused: --event {event_id} is in neither the pending nor the handled "
                    f"inbox, so its action_class cannot be checked (exit {EXIT_ACTION}, "
                    "nothing written)")
        if action is not None and action != recorded:
            return (f"refused: --action-class {action!r} contradicts the event record "
                    f"({event_id} says {recorded!r}); the record wins (exit "
                    f"{EXIT_ACTION}, nothing written)")
        action = recorded
    if action is None:
        return None
    if action != ACTION_WRITE:
        detail = f" (event {event_id})" if event_id else ""
        return (f"refused: action_class={action!r} is read-only{detail}. Only "
                f"{ACTION_WRITE!r} may open work; an event at this class can be acked, "
                "recorded in journal/, or escalated through bin/notice.py "
                f"(exit {EXIT_ACTION}, nothing written)")
    return None

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(WATCH, "state", "dispatch.jsonl")

# close_when: the specs this build decides, the ones it deliberately does not, and the limits.
PR_MERGED = "pr-merged"
NO_DIFF = "no-diff"
# Recognised but not decided, with the reason a reader gets. Not a placeholder for a future feature:
# ancestry is the wrong question in a squash-merge repository, so a check built on it would answer
# `false` forever and be trusted.
UNJUDGED = {
    "branch-empty": ("not decided by this build: a squash merge leaves the branch's commits outside "
                     "the baseline, so ancestry keeps saying \"unmerged\" after the content landed; "
                     "use pr-merged:<owner>/<repo>#<n>"),
}
GH_TIMEOUT_SECONDS = 15
GIT_TIMEOUT_SECONDS = 10


class CheckUnavailable(Exception):
    """One check could not answer; its row becomes `unknown` with this message."""


def _run(cmd, timeout, env=None, cwd=None):
    """Start a process — only ever from a check that a `close_when` asked for.

    `subprocess` is imported here rather than at module scope: `list` usually has no spec to
    evaluate, and the import alone costs a few milliseconds on a command that runs often.
    """
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise CheckUnavailable(f"no answer within {timeout}s") from None
    except FileNotFoundError:
        raise CheckUnavailable(f"{cmd[0]} is not installed") from None
    except OSError as error:
        raise CheckUnavailable(f"{cmd[0]} could not start: {error}") from None


def parse_pr(arg):
    """`<owner>/<repo>#<n>` → `(repo, number)`, or `None` when it is not one."""
    repo, sep, number = arg.rpartition("#")
    if not sep or not number.isdigit() or "#" in repo:
        return None
    owner, slash, name = repo.partition("/")
    if not slash or not owner or not name or "/" in name or any(c.isspace() for c in arg):
        return None
    return repo, number


def now():
    return datetime.datetime.now().astimezone()


def stamp(t=None):
    return (t or now()).isoformat(timespec="seconds")


def read(path=None):
    path = path or PATH
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                # A torn line is not a reason to lose every other dispatch: the
                # card must keep showing the rows that do parse.
                continue
    return rows


def append(row, path=None):
    path = path or PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def open_rows(path=None):
    path = path or PATH
    """The last row per session whose state is open — a closed row hides it."""
    latest = {}
    for row in read(path):
        sid = row.get("session")
        if not sid:
            continue
        latest[sid] = row
    return [row for row in latest.values() if not row.get("closed")]


def closed_rows(path=None):
    path = path or PATH
    latest = {}
    for row in read(path):
        sid = row.get("session")
        if sid:
            latest[sid] = row
    return [row for row in latest.values() if row.get("closed")]


def spec_of(spec):
    """`<kind>:<argument>` → `(kind in lower case, argument)`, or `(None, spec)` when unreadable.

    The stored value is never rewritten: `add` keeps it as given, and this only reads it.
    """
    kind, sep, arg = (spec or "").strip().partition(":")
    if not sep or not arg.strip():
        return None, (spec or "").strip()
    return kind.strip().lower(), arg.strip()


def _tail(text, fallback):
    """The last non-empty line of a tool's stderr — where its actual complaint is."""
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1] if lines else fallback


def judge_pr_merged(arg):
    """`(closable, reason)` from the pull request's own state; `gh` answers a GET."""
    parsed = parse_pr(arg)
    if parsed is None:
        return "unknown", f"unreadable spec: expected <owner>/<repo>#<n>, got {arg!r}"
    repo, number = parsed
    try:
        done = _run(["gh", "pr", "view", number, "--repo", repo, "--json", "state,mergedAt"],
                    GH_TIMEOUT_SECONDS)
    except CheckUnavailable as error:
        return "unknown", f"gh: {error}"
    if done.returncode != 0:
        return "unknown", "gh pr view failed: " + _tail(done.stderr, f"exit {done.returncode}")
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return "unknown", "gh printed something that is not JSON"
    if not isinstance(data, dict):
        return "unknown", "gh printed something that is not one pull request"
    if data.get("mergedAt") or data.get("state") == "MERGED":
        return True, ""
    return False, ""


def judge_no_diff(arg, repo=""):
    """`(closable, reason)` when nothing under `arg` is uncommitted.

    A relative path needs the row's `repo`: resolving it against whatever directory `list` happens
    to run in would make the verdict depend on the caller, which is the kind of quiet wrongness this
    field exists to avoid. `git status` runs with `GIT_OPTIONAL_LOCKS=0` so that evaluating cannot
    rewrite the repository's index — `list` promises to write nothing at all.
    """
    if not os.path.isabs(arg) and not repo:
        return "unknown", "a relative path needs --repo on the row: " + arg
    path = os.path.normpath(arg if os.path.isabs(arg) else os.path.join(repo, arg))
    if not os.path.exists(path):
        return "unknown", f"path does not exist: {path}"
    where = path if os.path.isdir(path) else os.path.dirname(path)
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        top = _run(["git", "-C", where, "rev-parse", "--show-toplevel"], GIT_TIMEOUT_SECONDS, env=env)
        root = (top.stdout or "").strip()
        if top.returncode != 0 or not root:
            return "unknown", "not inside a git work tree: " + _tail(top.stderr, "git rev-parse failed")
        rel = os.path.relpath(path, root)
        status = _run(["git", "-C", root, "status", "--porcelain", "--", rel],
                      GIT_TIMEOUT_SECONDS, env=env)
    except CheckUnavailable as error:
        return "unknown", f"git: {error}"
    if status.returncode != 0:
        return "unknown", "git status failed: " + _tail(status.stderr, f"exit {status.returncode}")
    return ((status.stdout or "").strip() == ""), ""


def judge(close_when, row, cache):
    """`(closable, reason)` for one spec, memoised per target within one `list`.

    Two rows naming the same pull request ask `gh` once: `list` runs on every wake.
    """
    kind, arg = spec_of(close_when)
    if kind is None:
        return "unknown", f"unreadable spec: {close_when!r}"
    if kind in UNJUDGED:
        return "unknown", UNJUDGED[kind]
    if kind not in (PR_MERGED, NO_DIFF):
        return "unknown", f"unknown spec kind: {kind}"
    repo = row.get("repo") or ""
    key = (kind, arg, repo if kind == NO_DIFF else "")
    if key not in cache:
        try:
            cache[key] = (judge_pr_merged(arg) if kind == PR_MERGED else judge_no_diff(arg, repo))
        except Exception as error:  # a broken check is one `unknown` row, never a failed list
            cache[key] = ("unknown", f"{type(error).__name__}: {error}")
    return cache[key]


def evaluate(rows, cache=None):
    """Copy the rows that carry a `close_when`, adding `closable` computed now.

    Read-only and additive by construction. A row without a spec is returned untouched and
    starts no process — `list` runs on every wake, and the zero-cost path is the common one.
    A closed row is not evaluated: a verdict there answers nothing.
    """
    cache = {} if cache is None else cache
    out = []
    for row in rows:
        if row.get("closed") or not row.get("close_when"):
            out.append(row)
            continue
        verdict, reason = judge(row["close_when"], row, cache)
        judged = dict(row, closable=verdict)
        if verdict == "unknown" and reason:
            judged["closable_reason"] = reason
        out.append(judged)
    return out


def add(session, title, accept, repo="", note="", path=None, close_when=None):
    path = path or PATH
    """Record one dispatch. `accept` is required: a row nobody can judge is noise."""
    if not session:
        raise ValueError("a dispatch needs the session id it was opened for")
    if not (accept or "").strip():
        raise ValueError("a dispatch needs an acceptance check (--accept): "
                         "what result lets this row be closed?")
    row = {
        "ts": stamp(),
        "session": session,
        "title": (title or "").strip(),
        "accept": accept.strip(),
        "repo": repo or "",
        "note": note or "",
        "closed": False,
    }
    if close_when is not None:
        spec = close_when.strip()
        if not spec:
            raise ValueError("--close-when was given but is empty: give a spec such as "
                             "pr-merged:<owner>/<repo>#<n>, or leave the flag out")
        # Stored as given (case and all): a spec this build cannot read is still the fact
        # that someone meant to check it, and `list` says `unknown` rather than dropping it.
        row["close_when"] = spec
    append(row, path)
    return row


def close(session, why="", path=None):
    path = path or PATH
    rows = {row["session"]: row for row in open_rows(path)}
    row = rows.get(session)
    if row is None:
        return None
    tomb = dict(row)
    tomb.update({"closed": True, "closed_why": why or "", "closed_at": stamp()})
    append(tomb, path)
    return tomb


def age_hours(row, at=None):
    try:
        then = datetime.datetime.fromisoformat(row["ts"])
    except (KeyError, ValueError):
        return None
    return max(0.0, ((at or now()) - then).total_seconds() / 3600.0)


def prune(days=14, path=None):
    path = path or PATH
    """Drop sessions whose dispatch closed longer than `days` ago.

    **All** rows of such a session go, not just the tombstone. `open_rows` reads the
    last row per session, so deleting a tombstone while keeping the row it hid would
    resurrect a finished dispatch as open — measured while building this tool: the
    card showed 「测试派工 · 0.0h」 again right after `prune --days 0`. A finished
    session's history is what the log keeps until it is pruned; after that it is one
    unit, not two.
    """
    cutoff = now() - datetime.timedelta(days=days)
    rows = read(path)
    last = {}
    for row in rows:
        if row.get("session"):
            last[row["session"]] = row
    finished = set()
    for session, row in last.items():
        if not row.get("closed"):
            continue
        try:
            if datetime.datetime.fromisoformat(row.get("closed_at") or row.get("ts")) < cutoff:
                finished.add(session)
        except (TypeError, ValueError):
            continue
    if not finished:
        return 0
    keep = [row for row in rows if row.get("session") not in finished]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in keep)
    return len(rows) - len(keep)


def _verdict(row):
    """The words for a computed `closable`; nothing when the row was not evaluated."""
    if "closable" not in row:
        return ""
    word = {True: "可关闭", False: "未完成"}.get(row["closable"], "未判定")
    reason = row.get("closable_reason")
    return f" → {word}" + (f"（{reason}）" if reason else "")


def render(rows, at=None, verdicts=False):
    """One line per open dispatch, in the shape a wake card reads.

    The age is in the line because a dispatch nobody closed for two days is the
    interesting one, and comparing timestamps by eye does not happen at 3am.

    `verdicts` is off for the wake card, which renders rows as stored: the card is built
    by another script that must not pay for a `gh` call per row. The spec itself is shown
    either way, so a reader knows a machine-checkable criterion exists and can ask
    `list --json` for its verdict.
    """
    out = []
    for row in sorted(rows, key=lambda r: r.get("ts") or ""):
        age = age_hours(row, at)
        when = f"{age:.1f}h" if age is not None and age < 48 else (
            f"{age / 24:.1f}d" if age is not None else "?")
        bits = [f"- `{row.get('session', '?')}`（{when}）{row.get('title') or '(无标题)'}"]
        if row.get("accept"):
            bits.append(f"  验收：{row['accept']}")
        if row.get("repo"):
            bits.append(f"  仓：`{row['repo']}`")
        if row.get("note"):
            bits.append(f"  我要求的：{row['note']}")
        if row.get("close_when"):
            bits.append("  完成判据：" + row["close_when"] + (_verdict(row) if verdicts else ""))
        out.append("\n".join(bits))
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=PATH, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="record a session I opened and must follow up")
    p_add.add_argument("--session", required=True)
    p_add.add_argument("--title", default="")
    p_add.add_argument("--accept", required=True,
                       help="what result lets this row be closed")
    p_add.add_argument("--repo", default="")
    p_add.add_argument("--note", default="")
    p_add.add_argument("--action-class", default=None,
                       help="the class of the event this dispatch belongs to; only `act` may "
                            "open work (see bin/send.py). Refused with exit 6 otherwise")
    p_add.add_argument("--event", default=None,
                       help="id of the inbox event this dispatch answers; its recorded class "
                            "decides, and an id the inbox does not know is refused")
    p_add.add_argument("--close-when", default=None,
                       help="machine-checkable completion criterion, stored verbatim and judged "
                            "by `list`: pr-merged:<owner>/<repo>#<n> or no-diff:<path>; anything "
                            "else is kept and reported unknown. It never closes a row")

    p_list = sub.add_parser("list", help="open dispatches, with closable computed now")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--all", action="store_true", help="include closed rows")

    p_close = sub.add_parser("close", help="close one dispatch")
    p_close.add_argument("session")
    p_close.add_argument("--why", default="")

    p_prune = sub.add_parser("prune", help="drop closed rows older than N days")
    p_prune.add_argument("--days", type=int, default=14)

    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.cmd == "add":
        refusal = action_refusal(args.action_class, args.event)
        if refusal:
            print(refusal, file=sys.stderr)
            return EXIT_ACTION
        try:
            row = add(args.session, args.title, args.accept, args.repo, args.note,
                      path=args.path, close_when=args.close_when)
        except ValueError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        print(f"tracking {row['session']} — {row['title'] or '(no title)'}"
              + (f"（完成判据：{row['close_when']}）" if row.get("close_when") else ""))
        return 0
    if args.cmd == "list":
        rows = evaluate(read(args.path) if args.all else open_rows(args.path))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if not rows:
            print("no open dispatches")
            return 0
        print("\n".join(render(rows, verdicts=True)))
        return 0
    if args.cmd == "close":
        row = close(args.session, args.why, args.path)
        if row is None:
            print(f"not tracked (or already closed): {args.session}", file=sys.stderr)
            return 1
        print(f"closed {args.session}" + (f" — {args.why}" if args.why else ""))
        return 0
    if args.cmd == "prune":
        dropped = prune(args.days, args.path)
        print(f"pruned {dropped} closed row(s)")
        return 0
    ap.print_help()
    return 2


def selftest():
    """The three properties the card depends on, against a throwaway file."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "dispatch.jsonl")

        # 1. An acceptance check is mandatory: a row nobody can judge is noise.
        for bad in ("", "   "):
            try:
                add("s1", "t", bad, path=path)
            except ValueError:
                pass
            else:
                raise AssertionError("add accepted an empty --accept")
        if open_rows(path):
            raise AssertionError("a refused add wrote a row")

        # 2. A close hides the row but keeps the log: the last row per session wins.
        add("s1", "first", "its test passes", path=path)
        add("s2", "second", "the PR merges", path=path)
        assert [r["session"] for r in open_rows(path)] == ["s1", "s2"], open_rows(path)
        assert close("s1", "done", path=path) is not None
        assert [r["session"] for r in open_rows(path)] == ["s2"], open_rows(path)
        assert len(read(path)) == 3, "the log must keep both rows, not rewrite them"
        assert [r["session"] for r in closed_rows(path)] == ["s1"]
        assert close("s1", "again", path=path) is None, "closing twice must be a no-op"
        assert close("ghost", "", path=path) is None

        # 3. Rendering carries the judgement, not just the id: age + acceptance.
        line = "\n".join(render(open_rows(path)))
        assert "the PR merges" in line, line
        assert "s2" in line, line

        # 4. A torn line cannot hide the rows around it.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        add("s3", "third", "the doc is pair-complete", path=path)
        assert {r["session"] for r in open_rows(path)} == {"s2", "s3"}, open_rows(path)

        # 5. Pruning drops a finished session **whole**: dropping only the tombstone
        #    would resurrect the row it hid (open_rows reads the last row per session).
        old = {"ts": "2000-01-01T00:00:00+08:00", "session": "old", "title": "t",
               "accept": "a", "closed": False}
        append(old, path)
        append(dict(old, closed=True, closed_at="2000-01-02T00:00:00+08:00"), path)
        append(dict(old, session="old-open", title="still open"), path)
        prune(14, path)
        sessions = {r["session"] for r in open_rows(path)} | {r["session"] for r in closed_rows(path)}
        assert "old" not in sessions, "a finished session past the cutoff must go"
        assert "old-open" in {r["session"] for r in open_rows(path)}, \
            "an open row is never pruned (and must not be resurrected either)"
        assert "old" not in {r.get("session") for r in read(path)}, \
            "both the row and its tombstone go, or the row comes back"

        # 6. `close_when` is judged now and acted on never. `_run` is replaced so this stays
        #    offline: the selftest runs in CI, where `gh` has no credentials and a network call
        #    would be a flaky check rather than a test.
        global _run
        real_run, calls = _run, []

        class Done:
            def __init__(self, out="", err="", code=0):
                self.stdout, self.stderr, self.returncode = out, err, code

        def fake(cmd, timeout, env=None, cwd=None):
            calls.append(list(cmd))
            if cmd[0] == "gh":
                repo, number = cmd[5], cmd[3]
                if repo == "foo/bar":
                    return Done(err="Could not resolve to a Repository named 'foo/bar'", code=1)
                if number == "7":
                    return Done('{"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}')
                if number == "8":
                    return Done('{"state": "OPEN", "mergedAt": null}')
                raise CheckUnavailable(f"no answer within {timeout}s")
            if cmd[0] == "git" and "rev-parse" in cmd:
                return Done(out=tmp + "\n")
            if cmd[0] == "git" and "status" in cmd:
                return Done(out=" M bin/x.py\n" if cmd[-1].endswith("dirty") else "")
            return Done(code=2)

        try:
            _run = fake
            dirty_file = os.path.join(tmp, "dirty")
            open(dirty_file, "w").close()
            base = {"ts": stamp(), "title": "t", "accept": "a", "closed": False}
            merged = dict(base, session="d7", close_when="pr-merged:o/r#7")
            still_open = dict(base, session="d8", close_when="pr-merged:o/r#8")
            missing = dict(base, session="d9", close_when="pr-merged:foo/bar#1")
            hung = dict(base, session="d10", close_when="pr-merged:o/r#10")
            branchy = dict(base, session="d11", close_when="branch-empty:dev-x")
            junk = dict(base, session="d12", close_when="nonsense")
            plain = dict(base, session="d13")
            done_row = dict(base, session="d14", closed=True, close_when="pr-merged:o/r#7")
            clean = dict(base, session="d15", close_when="no-diff:" + tmp, repo=tmp)
            dirty = dict(base, session="d16", close_when="no-diff:" + dirty_file, repo=tmp)
            judged = {row["session"]: row for row in evaluate(
                [merged, still_open, missing, hung, branchy, junk, plain, done_row, clean, dirty])}

            assert judged["d7"]["closable"] is True, judged["d7"]
            assert judged["d8"]["closable"] is False, judged["d8"]
            for sid, hint in (("d9", "gh pr view failed"), ("d10", "no answer within"),
                              ("d11", "squash"), ("d12", "unreadable")):
                assert judged[sid]["closable"] == "unknown", judged[sid]
                assert hint in judged[sid]["closable_reason"], judged[sid]
            assert judged["d15"]["closable"] is True, judged["d15"]
            assert judged["d16"]["closable"] is False, judged["d16"]
            assert "closable" not in judged["d13"], "a row without a spec must be untouched"
            assert "closable" not in judged["d14"], "a closed row is not evaluated"
            assert len(judged) == 10, "one failing row must not drop the others"

            before = len([c for c in calls if c[0] == "gh"])
            evaluate([merged, dict(merged, session="d18")])
            after = len([c for c in calls if c[0] == "gh"])
            assert after - before == 1, "the same pull request is asked once per list"

            before = len(calls)
            evaluate([plain, done_row])
            assert len(calls) == before, "a row without a spec must start no process"

            text = "\n".join(render(list(judged.values()), verdicts=True))
            assert "可关闭" in text and "未完成" in text and "未判定" in text, text
            card = "\n".join(render([merged]))
            assert "完成判据：pr-merged:o/r#7" in card and "可关闭" not in card, card
        finally:
            _run = real_run

        # 7. The rest is the write path `close_when` must not disturb: stored as given, refusable
        #    when empty, and carried by the tombstone so a closed row still says how it ended.
        try:
            add("s9", "t", "a", path=path, close_when="   ")
        except ValueError:
            pass
        else:
            raise AssertionError("add accepted an empty --close-when")
        kept = add("s10", "t", "a", path=path, close_when="  Weird:Thing#1  ")
        assert kept["close_when"] == "Weird:Thing#1", kept
        assert read(path)[-1]["close_when"] == "Weird:Thing#1", read(path)[-1]
        add("s12", "t", "a", path=path)
        assert "close_when" not in close("s12", "done", path=path), \
            "a close must not invent the key on a row that never had one"
        add("s11", "t", "a", path=path, close_when="pr-merged:o/r#7")
        assert close("s11", "done", path=path)["close_when"] == "pr-merged:o/r#7"

        # 8. `add` is gated by action_class: only `act` may open work, and a refusal
        #    writes nothing. `close` is not gated — recording an outcome is exactly what
        #    a read-only event is allowed to produce.
        gated = os.path.join(tmp, "gated.jsonl")
        for value in ("observe", "triage", "auto", ""):
            code = main(["--path", gated, "add", "--session", "s", "--accept", "a",
                         "--action-class", value])
            assert code == EXIT_ACTION, (value, code)
            assert not os.path.exists(gated), f"{value} wrote the ledger"
        assert main(["--path", gated, "add", "--session", "s", "--accept", "a"]) == 0, \
            "no flag is the manual path and must still work"
        assert len(read(gated)) == 1, read(gated)
        assert main(["--path", gated, "add", "--session", "s2", "--accept", "a",
                     "--action-class", "act"]) == 0
        assert len(read(gated)) == 2, read(gated)

        # The inbox record wins over what the caller typed.
        import inbox as inbox_mod
        saved = (inbox_mod.PENDING, inbox_mod.HANDLED)
        inbox_mod.PENDING = os.path.join(tmp, "inbox-pending.jsonl")
        inbox_mod.HANDLED = os.path.join(tmp, "inbox-handled.jsonl")
        with open(inbox_mod.PENDING, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "ev_observe", "kind": "external",
                                 "detail": {"action_class": "observe"}}) + "\n")
        with open(inbox_mod.HANDLED, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "ev_act", "kind": "external",
                                 "detail": {"action_class": "act"}}) + "\n")
        assert action_refusal("act", "ev_observe") is not None, "a claimed act must not override"
        assert action_refusal(None, "ev_observe") is not None
        assert action_refusal(None, "ev_missing") is not None
        assert action_refusal(None, "ev_act") is None
        assert action_refusal("observe", "ev_act") is not None, "a contradiction is refused"
        assert main(["--path", gated, "add", "--session", "s3", "--accept", "a",
                     "--event", "ev_observe"]) == EXIT_ACTION
        assert len(read(gated)) == 2, "a refused --event add must not write"
        inbox_mod.PENDING, inbox_mod.HANDLED = saved

        # 9. Evaluating is read-only: the ledger's bytes and mtime survive it.
        ledger = os.path.join(tmp, "readonly.jsonl")
        add("r1", "t", "a", path=ledger, close_when="pr-merged:o/r#7")
        add("r2", "t", "a", path=ledger)
        before = (open(ledger, "rb").read(), os.stat(ledger).st_mtime_ns)
        evaluate(open_rows(ledger))
        assert before == (open(ledger, "rb").read(), os.stat(ledger).st_mtime_ns), \
            "evaluating must not touch the ledger"

    print("dispatch selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
