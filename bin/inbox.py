#!/usr/bin/env python3
"""Pending-event inbox for the session supervisor (no model calls).

  inbox.py list [--json] [--limit N] [--priority high,medium,low]
  inbox.py count
  inbox.py ack <event-id> [<event-id> ...]
  inbox.py ack-all
  inbox.py fold [--dry-run] [--kinds a,b]
"""
import json
import os
import sys
import time

# Windows consoles default to a legacy code page (cp936 on the deployment box), and
# `list --json` prints Chinese summaries verbatim — so a plain `python3 bin/inbox.py
# list --json` died with UnicodeEncodeError and exit 1, i.e. the documented way to read
# a full event body was unusable exactly where the manual says to use it. Force UTF-8 on
# stdout instead of depending on the caller's codepage or on PYTHONIOENCODING being set.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING = os.path.join(WATCH, "inbox", "pending.jsonl")
HANDLED = os.path.join(WATCH, "inbox", "handled.jsonl")

# Mirrored into the SQLite store (bin/store.py) before each file write; see the long
# note in bin/scan.py. `mirror_inbox` only fires when both paths are the live pair, so
# a selftest with its own temp inbox is unaffected.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402


def read_lines(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


# Kinds whose event describes *the session's state at a moment*: a later event of the
# same kind for the same session supersedes the earlier one, because the earlier state
# cannot still be true (a session that stopped again stopped once, and its text is the
# newest one). Deliberately excluded: `failed_run` / `run_error`, whose detail names a
# specific run — two failures are two facts, not one state — and both are `high` anyway,
# so they wake a turn immediately instead of piling up. `external` is here for the other
# identity: an outside event is superseded per thread, not per session (see `fold_key`).
FOLD_KINDS = ("awaiting_user", "new_session", "stalled", "approval_pending", "external")


def fold_key(e):
    """What one event is *about*, for superseding.

    A session event describes the state of one session at a moment, so its identity is
    `(sessionId, kind)` — a later stop supersedes an earlier one. A session that stopped
    three times produced three real events, and only the newest premise still holds.

    An external event has no session to be the state of: its identity is `thread_key`,
    because two comments on one pull request are one conversation, and folding them to one
    line is what keeps a chatty thread from filling the card. Its `kind` is not part of
    the key — `external` is the only kind external events carry.

    `None` when the row carries neither identity: it cannot be folded with anything, so it
    is kept as it is.
    """
    thread = e.get("thread_key") or (e.get("detail") or {}).get("thread_key")
    if thread:
        return ("thread", thread)
    if e.get("sessionId") and e.get("kind"):
        return ("session", e.get("sessionId"), e.get("kind"))
    return None


def fold_superseded(rows, kinds=FOLD_KINDS, now_ms=None):
    """Split `rows` into (keep, folded): one survivor per `fold_key`.

    Why fold instead of rendering only the newest line: the user asked exactly that
    (2026-10-16, phone: 「待处理事件一个会话出现了多条，为什么不能只使用最后一条呢」),
    and the answer is that the ids are also the **ack handles** — an event that never
    appears on the card would never be acked, so it would keep counting in
    `pendingCount`, keep riding the heartbeat, and reappear on every later card as if
    it were new. Folding removes the event from pending *and* records it: it moves to
    `inbox/handled.jsonl` with `foldedInto` = the surviving event's id, so the history
    still shows what was superseded by what.

    What piled up: measured 2026-10-16 21:47, the card carried **6 events for 3
    sessions** and 「蓝色空提问气泡排查」 alone appeared three times (21:17 / 21:36 /
    21:45, all 「已停下，等待用户回复」). Those three were real — the session ran three
    times (`queryCount` 1→2→3) — but only the newest still holds: the earlier stops'
    premises were answered by the run that followed. They accumulated because a turn
    that cannot act yet (target mid-run) deliberately leaves its event pending, and the
    target's next stop fires another event for the same session.
    """
    rows = list(rows or [])
    kinds = tuple(kinds or ())
    if not kinds:
        return rows, []
    # The survivor is the newest by `detectedAtMs`, with file order (append order
    # during a scan) as the tie-break.
    survivor = {}
    for i, e in enumerate(rows):
        if e.get("kind") not in kinds:
            continue
        key = fold_key(e)
        if key is None:
            continue
        prev = survivor.get(key)
        if prev is None or int(e.get("detectedAtMs") or 0) >= int(rows[prev].get("detectedAtMs") or 0):
            survivor[key] = i
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    keep, folded = [], []
    for i, e in enumerate(rows):
        key = fold_key(e)
        if key is not None and key in survivor and survivor[key] != i:
            rec = dict(e)
            rec["handledAtMs"] = now
            rec["foldedInto"] = rows[survivor[key]].get("id")
            rec["foldedReason"] = ("superseded by a later event on the same thread"
                                   if key[0] == "thread" else
                                   "superseded by a later event of the same kind for the same session")
            folded.append(rec)
        else:
            keep.append(e)
    return keep, folded


def apply_fold(pending_path=PENDING, handled_path=HANDLED, kinds=FOLD_KINDS,
               rows=None, now_ms=None, dry=False):
    """`fold_superseded` plus the two writes, so callers share one implementation."""
    rows = read_lines(pending_path) if rows is None else list(rows)
    keep, folded = fold_superseded(rows, kinds=kinds, now_ms=now_ms)
    if folded and not dry:
        store.mirror_inbox(store.mark_handled, folded, pending_path, handled_path)
        write_lines(pending_path, keep)
        os.makedirs(os.path.dirname(handled_path), exist_ok=True)
        with open(handled_path, "a", encoding="utf-8") as fh:
            for r in folded:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return keep, folded


def write_lines(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def ack(ids, pending_path=PENDING, handled_path=HANDLED, all_rows=False, now_ms=None):
    """Retire events from pending and record them in handled. Returns (moved, kept).

    Exposed as a function (the CLI `ack` branch is one caller) because a *script* has
    to retire events too: the wake gate retires the ones it concludes nothing needs
    doing about, and it must do that with the same bookkeeping as a hand-typed
    `inbox.py ack` — an event that leaves the card without landing in handled.jsonl
    is an event whose fate nobody can audit.

    Paths are arguments, not module constants, so a caller that owns a different
    inbox (a selftest with a temp dir) does not ack the live one.
    """
    ids = set(ids or ())
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    kept, moved = [], []
    for e in read_lines(pending_path):
        if all_rows or e.get("id") in ids:
            rec = dict(e)
            rec["handledAtMs"] = now
            moved.append(rec)
        else:
            kept.append(e)
    if moved:
        store.mirror_inbox(store.mark_handled, moved, pending_path, handled_path)
    write_lines(pending_path, kept)
    if moved:
        os.makedirs(os.path.dirname(handled_path), exist_ok=True)
        with open(handled_path, "a", encoding="utf-8") as fh:
            for e in moved:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return moved, kept


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    pending = read_lines(PENDING)

    if cmd == "list":
        prios = None
        limit = 50
        as_json = "--json" in rest
        brief = "--brief" in rest
        for i, a in enumerate(rest):
            if a == "--priority" and i + 1 < len(rest):
                prios = set(rest[i + 1].split(","))
            if a == "--limit" and i + 1 < len(rest):
                limit = int(rest[i + 1])
        rows = [e for e in pending if not prios or e.get("priority") in prios]
        rows = rows[-limit:]
        if brief:
            # Keep a turn's context small: full event bodies can be tens of KB each.
            out = [{"id": e.get("id"), "kind": e.get("kind"), "priority": e.get("priority"),
                    "sessionId": e.get("sessionId"),
                    "sessionName": (e.get("sessionName") or "")[:60],
                    "summary": e.get("summary"),
                    "questionLike": (e.get("detail") or {}).get("questionLike"),
                    "at": e.get("detectedAtMs")} for e in rows]
            print(json.dumps({"pending": len(pending), "shown": len(out), "events": out},
                             ensure_ascii=False, indent=1))
            return 0
        if as_json:
            print(json.dumps({"pending": len(pending), "shown": len(rows), "events": rows},
                             ensure_ascii=False, indent=1))
        else:
            for e in rows:
                print(f"{e.get('id')} [{e.get('priority')}] {e.get('kind')} "
                      f"{e.get('sessionId')} :: {e.get('summary')}")
            print(f"-- {len(rows)} shown / {len(pending)} pending")
        return 0

    if cmd == "count":
        print(len(pending))
        return 0

    if cmd == "fold":
        kinds = FOLD_KINDS
        for i, a in enumerate(rest):
            if a == "--kinds" and i + 1 < len(rest):
                kinds = tuple(x for x in rest[i + 1].split(",") if x)
        keep, folded = apply_fold(kinds=kinds, dry="--dry-run" in rest)
        for r in folded:
            print(f"folded {r.get('id')} [{r.get('kind')}] {r.get('sessionId')}"
                  f" -> {r.get('foldedInto')}")
        print(f"folded {len(folded)}, {len(keep)} still pending")
        return 0

    if cmd in ("ack", "ack-all"):
        ids = set() if cmd == "ack-all" else set(a for a in rest if not a.startswith("-"))
        moved, kept = ack(ids, all_rows=(cmd == "ack-all"))
        print(f"acked {len(moved)}, {len(kept)} still pending")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
