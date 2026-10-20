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

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING = os.path.join(WATCH, "inbox", "pending.jsonl")
HANDLED = os.path.join(WATCH, "inbox", "handled.jsonl")


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
# so they wake a turn immediately instead of piling up.
FOLD_KINDS = ("awaiting_user", "new_session", "stalled", "approval_pending")


def fold_superseded(rows, kinds=FOLD_KINDS, now_ms=None):
    """Split `rows` into (keep, folded): one survivor per (sessionId, kind).

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
        if e.get("kind") not in kinds or not e.get("sessionId"):
            continue
        key = (e.get("sessionId"), e.get("kind"))
        prev = survivor.get(key)
        if prev is None or int(e.get("detectedAtMs") or 0) >= int(rows[prev].get("detectedAtMs") or 0):
            survivor[key] = i
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    keep, folded = [], []
    for i, e in enumerate(rows):
        key = (e.get("sessionId"), e.get("kind"))
        if key in survivor and survivor[key] != i:
            rec = dict(e)
            rec["handledAtMs"] = now
            rec["foldedInto"] = rows[survivor[key]].get("id")
            rec["foldedReason"] = "superseded by a later event of the same kind for the same session"
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
        kept, moved = [], []
        for e in pending:
            if cmd == "ack-all" or e.get("id") in ids:
                e["handledAtMs"] = int(time.time() * 1000)
                moved.append(e)
            else:
                kept.append(e)
        write_lines(PENDING, kept)
        if moved:
            os.makedirs(os.path.dirname(HANDLED), exist_ok=True)
            with open(HANDLED, "a", encoding="utf-8") as fh:
                for e in moved:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"acked {len(moved)}, {len(kept)} still pending")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
