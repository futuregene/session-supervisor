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

    {"ts", "session", "title", "accept", "repo", "note", "closed", "closed_why", "closed_at"}

- `accept` is the point: "what result would let me close this row". A row without one is a row
  nobody can judge, so `add` refuses it.
- `bin/send.py --new` appends a row itself, so a dispatch cannot be created without being tracked.
- `bin/turn_prompt.py` renders the open rows into every wake card, which is the whole mechanism:
  the card is the only thing that reliably reaches a later me.

Rows are append-only; `close` appends a tombstone row with the same session, and `open_rows`
takes the last row per session. That keeps the file a log (auditable, and a mis-close is one
more row away) instead of a mutable table that can be corrupted by two writers.

Usage:
  dispatch.py add --session <id> --title "…" --accept "…" [--repo <path>] [--note "…"]
  dispatch.py list [--json] [--all]
  dispatch.py close <session-id> [--why "…"]
  dispatch.py prune [--days N]        # drop closed rows older than N days (default 14)
  dispatch.py --selftest
"""
import argparse
import datetime
import json
import os
import sys

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(WATCH, "state", "dispatch.jsonl")


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


def add(session, title, accept, repo="", note="", path=None):
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


def render(rows, at=None):
    """One line per open dispatch, in the shape a wake card reads.

    The age is in the line because a dispatch nobody closed for two days is the
    interesting one, and comparing timestamps by eye does not happen at 3am.
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

    p_list = sub.add_parser("list", help="open dispatches")
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
        try:
            row = add(args.session, args.title, args.accept, args.repo, args.note, args.path)
        except ValueError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        print(f"tracking {row['session']} — {row['title'] or '(no title)'}")
        return 0
    if args.cmd == "list":
        rows = (read(args.path) if args.all else open_rows(args.path))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if not rows:
            print("no open dispatches")
            return 0
        print("\n".join(render(rows)))
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

    print("dispatch selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
