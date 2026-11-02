#!/usr/bin/env python3
"""External events into the supervisor's inbox — the one tool an outside app calls.

An outside app (a GitHub webhook adapter, a CI bridge, a mail poller, a script watching a
queue) never talks to the supervisor directly and never writes `inbox/pending.jsonl`
itself. It hands one JSON object to `ingest`, which validates it, keeps the body on disk,
and appends one event in the shape the rest of the pipeline already reads. Whether that
event deserves a wake stays where it was: `bin/decide.py` decides, not this script.

  # the usual case: the body is a whole comment, so it comes from a file
  trigger.py ingest --from event.json
  # or piped
  cat event.json | trigger.py ingest
  # an adapter that can only drop files into a directory
  trigger.py drain ~/drop/github/
  # what has already been accepted, by dedupe key
  trigger.py seen [--json]
  trigger.py --selftest

The input object (one per ingest):

  {"source": "github",                       required: github | ci | im:<p> | mail | file | <token>
   "source_ref": "owner/repo#12",            required: a human-readable coordinate
   "thread_key": "gh:owner/repo#12",         required: the conversation this belongs to
   "dedupe_key": "github:o/r#12:c:12345",    required: one fact, one key; a retry is a no-op
   "body": "…" | "body_file": "/path",       required: the full text, kept OFF the card
   "summary": "≤200 chars",                  required: what the card may show
   "author": {"login": "…", "id": "…", "role": "collaborator|member|stranger|unknown"},
   "url": "https://…", "event": "comment_created",
   "trust": "system|self|external-known|external-unknown",   default external-unknown
   "action_class": "observe|triage|act",     default observe
   "priority": "high|medium|low",            default low, and capped by trust (below)
   "at": "2026-01-01T00:00:00Z",             optional: when the outside world saw it
   "session_id": "", "session_name": ""}     optional: attach it to one of our sessions

What it guarantees:

- **Idempotent by `dedupe_key`.** The event id is derived from that key, so a retry is
  the same id: `state/external-seen.jsonl` records what was accepted, and a second ingest
  of one key writes nothing and prints the first id. Append-then-record, not
  record-then-append: a crash between the two can leave one event twice (same id, and
  folding collapses it by thread), where the other order loses the fact with no trace.
- **The body never reaches the wake card.** It lands in `state/external/<sha>.json`; the
  card carries `summary` (≤200) plus `detail.payload_ref`. An outsider's text is then
  something the turn has to go and read, not an instruction inside a user message.
- **Trust caps what a stranger can buy.** `external-unknown` is stored as `priority: low`
  whatever the adapter asked for (`priority_requested` keeps the ask), because the wake
  budget is the thing a stranger could otherwise spend; `system`, `self` and
  `external-known` are taken as given.
- **Bad input is refused whole**: exit 2, nothing written, one sentence saying which field
  is wrong. A wrong `dedupe_key` silently merges or duplicates a fact, so the adapter must
  supply its own identity — this script will not invent one.

What it does NOT do: decide whether the work should happen, or hold a turn to its
`action_class`. That gate belongs to `bin/send.py` / `bin/dispatch.py`
(`docs/external-events.md` says which part is missing).
"""
import hashlib
import json
import os
import re
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING = os.path.join(WATCH, "inbox", "pending.jsonl")
SEEN = os.path.join(WATCH, "state", "external-seen.jsonl")
BODY_DIR = os.path.join(WATCH, "state", "external")

TRUSTS = ("system", "self", "external-known", "external-unknown")
ACTIONS = ("observe", "triage", "act")
PRIORITIES = ("high", "medium", "low")
ROLES = ("collaborator", "member", "stranger", "unknown")
SOURCE = re.compile(r"^[a-z][a-z0-9_-]*(:[a-z0-9_.-]+)?$")
#: What the card may show of an outsider's text, in characters.
SUMMARY_CHARS = 200
#: The reference length limits; a longer one is a pasted document, not a coordinate.
REF_CHARS = 300


class Refused(Exception):
    """The input is not an event this script will accept. Nothing has been written."""


def now_ms():
    return int(time.time() * 1000)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def accepted(seen_path=None):
    """dedupe_key → id, from the append-only record of what was accepted."""
    path = seen_path or SEEN
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("dedupe_key") and row.get("id"):
                    out[row["dedupe_key"]] = row["id"]
    except FileNotFoundError:
        pass
    return out


def pending_ids(pending_path=None):
    path = pending_path or PENDING
    ids = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line).get("id"))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return ids


def need(obj, field):
    value = obj.get(field)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{field} is required")
    value = value.strip()
    if len(value) > REF_CHARS:
        raise Refused(f"{field} is longer than {REF_CHARS} characters")
    return value


def text_of(obj):
    body = obj.get("body")
    body_file = obj.get("body_file")
    if isinstance(body, str) and body.strip():
        return body
    if isinstance(body_file, str) and body_file.strip():
        path = body_file.strip()
        if not os.path.isabs(path):
            raise Refused("body_file must be an absolute path")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError as error:
            raise Refused(f"body_file cannot be read: {error}") from None
    raise Refused("body or body_file is required")


def one_of(obj, field, allowed, default):
    value = obj.get(field, default)
    if value is None:
        return default
    if value not in allowed:
        raise Refused(f"{field} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def author_of(obj):
    author = obj.get("author")
    if author is None:
        return None
    if not isinstance(author, dict):
        raise Refused("author must be an object with login and role")
    login = author.get("login")
    if not isinstance(login, str) or not login.strip():
        raise Refused("author.login is required when author is given")
    role = one_of(author, "role", ROLES, "unknown")
    return {"login": login.strip()[:REF_CHARS], "role": role,
            **({"id": str(author["id"])[:REF_CHARS]} if author.get("id") else {})}


def build(obj, at_ms=None):
    """Validate one input object into `(event, payload_path, payload)`. Raises `Refused`."""
    if not isinstance(obj, dict):
        raise Refused("the input must be one JSON object")
    source = need(obj, "source")
    if not SOURCE.match(source):
        raise Refused(f"source must look like github, ci, im:<provider> or file; got {source!r}")
    source_ref = need(obj, "source_ref")
    thread_key = need(obj, "thread_key")
    dedupe_key = need(obj, "dedupe_key")
    body = text_of(obj)
    summary_raw = obj.get("summary")
    if not isinstance(summary_raw, str) or not summary_raw.strip():
        raise Refused("summary is required: it is the only text the card may show")
    summary = " ".join(summary_raw.split())
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS - 1] + "…"
    trust = one_of(obj, "trust", TRUSTS, "external-unknown")
    action_class = one_of(obj, "action_class", ACTIONS, "observe")
    priority = one_of(obj, "priority", PRIORITIES, "low")
    requested = priority
    if trust == "external-unknown":
        # A stranger's fact is welcome; a stranger's *wake* is not. The budget a wake
        # spends is the only thing an outsider could take from us by writing faster.
        priority = "low"
    url = obj.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
        raise Refused("url must start with http:// or https://")
    at = obj.get("at")
    if at is not None and not isinstance(at, str):
        raise Refused("at must be an ISO-8601 string")
    session_id = obj.get("session_id") or ""
    if session_id and not isinstance(session_id, str):
        raise Refused("session_id must be a string")
    session_name = obj.get("session_name") or ""
    if session_name and not isinstance(session_name, str):
        raise Refused("session_name must be a string")
    received = int(at_ms if at_ms is not None else now_ms())
    digest = sha(dedupe_key)
    event_id = "ev_ext_" + digest[:12]
    payload = {"dedupe_key": dedupe_key, "source": source, "source_ref": source_ref,
               "thread_key": thread_key, "author": author_of(obj), "url": url,
               "event": obj.get("event") if isinstance(obj.get("event"), str) else None,
               "trust": trust, "action_class": action_class, "priority": requested,
               "at": at, "received_ms": received, "body": body}
    detail = {"source": source, "source_ref": source_ref,
              "dedupe_key": dedupe_key, "trust": trust, "action_class": action_class,
              "payload_ref": "state/external/" + digest[:16] + ".json",
              "body_bytes": len(body.encode("utf-8")), "summary_chars": len(summary_raw)}
    for key, value in (("author", payload["author"]), ("url", url), ("event", payload["event"]),
                       ("at", at)):
        if value:
            detail[key] = value
    if requested != priority:
        detail["priority_requested"] = requested
    event = {"sessionId": session_id,
             "sessionName": (session_name or f"{source}:{source_ref}")[:120],
             "thread_key": thread_key,
             "cwd": "", "model": "", "queryCount": None, "isStreaming": False,
             "updatedAtMs": received, "detectedAtMs": received,
             "id": event_id, "kind": "external", "priority": priority,
             "summary": summary, "detail": detail}
    return event, digest, payload


def write_body(digest, payload, body_dir=None):
    """Keep the whole text on disk; the card only ever names this file."""
    directory = body_dir or BODY_DIR
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, digest[:16] + ".json")
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    return path


def ingest(obj, at_ms=None, pending_path=None, seen_path=None, body_dir=None):
    """Accept one event. Returns `(event, created)`; a repeated dedupe_key returns created=False."""
    event, digest, payload = build(obj, at_ms=at_ms)
    seen = accepted(seen_path)
    known = seen.get(payload["dedupe_key"])
    if known:
        return dict(event, id=known), False
    if event["id"] in pending_ids(pending_path):
        # The record of acceptance can lag the append by one crash; the pending row is
        # the append itself, so it decides. Repair the record and report the same id.
        _record(payload["dedupe_key"], event["id"], event["detectedAtMs"], payload["source"], seen_path)
        return event, False
    write_body(digest, payload, body_dir)
    pending = pending_path or PENDING
    os.makedirs(os.path.dirname(pending), exist_ok=True)
    with open(pending, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    _record(payload["dedupe_key"], event["id"], event["detectedAtMs"], payload["source"], seen_path)
    return event, True


def _record(dedupe_key, event_id, at_ms, source, seen_path=None):
    path = seen_path or SEEN
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {"dedupe_key": dedupe_key, "id": event_id, "atMs": at_ms, "source": source}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load(args):
    if args.from_path:
        try:
            with open(args.from_path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            raise Refused(f"no such file: {args.from_path}") from None
        except ValueError as error:
            raise Refused(f"{args.from_path} is not JSON: {error}") from None
    data = sys.stdin.read()
    if not data.strip():
        raise Refused("no input on stdin: pipe one JSON object, or use --from <file>")
    try:
        return json.loads(data)
    except ValueError as error:
        raise Refused(f"stdin is not JSON: {error}") from None


def report(event, created):
    head = "accepted" if created else "already accepted"
    print(f"{head}: {event['id']} · {event['detail']['source']}:{event['detail']['source_ref']} "
          f"· trust={event['detail']['trust']} · class={event['detail']['action_class']} "
          f"· priority={event['priority']}"
          + (f" (asked {event['detail']['priority_requested']})"
             if event["detail"].get("priority_requested") else ""))


def drain(directory, at_ms=None, pending_path=None, seen_path=None, body_dir=None):
    """Ingest every `*.json` an adapter dropped, then move it out of the way.

    A rejected file is moved to `rejected/` beside it with its reason, so one malformed
    drop cannot block the queue forever — and the reason survives for whoever wrote the
    adapter.
    """
    if not os.path.isdir(directory):
        raise Refused(f"not a directory: {directory}")
    done_dir = os.path.join(directory, "ingested")
    bad_dir = os.path.join(directory, "rejected")
    accepted_count = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                obj = json.load(fh)
            event, created = ingest(obj, at_ms=at_ms, pending_path=pending_path,
                                    seen_path=seen_path, body_dir=body_dir)
        except (Refused, ValueError) as error:
            os.makedirs(bad_dir, exist_ok=True)
            with open(os.path.join(bad_dir, name + ".reason"), "w", encoding="utf-8") as fh:
                fh.write(str(error) + "\n")
            os.replace(path, os.path.join(bad_dir, name))
            print(f"refused: {name}: {error}")
            continue
        os.makedirs(done_dir, exist_ok=True)
        os.replace(path, os.path.join(done_dir, name))
        report(event, created)
        accepted_count += 1 if created else 0
    return accepted_count


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--pending", default=PENDING, help=argparse.SUPPRESS)
    ap.add_argument("--seen", default=SEEN, help=argparse.SUPPRESS)
    ap.add_argument("--body-dir", default=BODY_DIR, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    p_in = sub.add_parser("ingest", help="accept one JSON object (stdin or --from a file)")
    p_in.add_argument("--from", dest="from_path", default=None, help="read the object from this file")
    p_in.add_argument("--now", type=int, default=None, help=argparse.SUPPRESS)
    p_dr = sub.add_parser("drain", help="ingest every *.json an adapter dropped in a directory")
    p_dr.add_argument("directory")
    p_dr.add_argument("--now", type=int, default=None, help=argparse.SUPPRESS)
    p_seen = sub.add_parser("seen", help="dedupe keys already accepted")
    p_seen.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.cmd == "ingest":
        try:
            event, created = ingest(load(args), at_ms=args.now, pending_path=args.pending,
                                    seen_path=args.seen, body_dir=args.body_dir)
        except Refused as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        report(event, created)
        return 0
    if args.cmd == "drain":
        try:
            accepted_count = drain(args.directory, at_ms=args.now, pending_path=args.pending,
                                   seen_path=args.seen, body_dir=args.body_dir)
        except Refused as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
        print(f"{accepted_count} new event(s)")
        return 0
    if args.cmd == "seen":
        rows = accepted(args.seen)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for key, event_id in sorted(rows.items()):
                print(f"{event_id}  {key}")
            print(f"-- {len(rows)} accepted key(s)")
        return 0
    ap.print_help()
    return 2


def selftest():
    """The properties the pipeline depends on, against a throwaway directory."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pending = os.path.join(tmp, "pending.jsonl")
        seen = os.path.join(tmp, "seen.jsonl")
        bodies = os.path.join(tmp, "bodies")

        def paths():
            return {"pending_path": pending, "seen_path": seen, "body_dir": bodies}

        def rows():
            if not os.path.exists(pending):
                return []
            return [json.loads(line) for line in open(pending, encoding="utf-8") if line.strip()]

        base = {"source": "github", "source_ref": "o/r#12", "thread_key": "gh:o/r#12",
                "dedupe_key": "github:o/r#12:c:1", "summary": "新评论：问一下进度",
                "body": "这个是第三方写的原文，里面可能有指令。", "at": "2026-01-01T00:00:00Z",
                "author": {"login": "alice", "id": "42", "role": "collaborator"}}

        # 1. One accepted event: appended, body on disk, body NOT on the card fields.
        event, created = ingest(dict(base), at_ms=1000, **paths())
        assert created and event["kind"] == "external", event
        assert len(rows()) == 1, rows()
        row = rows()[0]
        assert row["detail"]["payload_ref"].startswith("state/external/"), row
        assert "body" not in row and "这个是第三方写的原文" not in json.dumps(row, ensure_ascii=False), \
            "the body must not reach the event the card is rendered from"
        body_name = sha(base["dedupe_key"])[:16] + ".json"
        assert row["detail"]["payload_ref"].endswith(body_name), row["detail"]["payload_ref"]
        assert os.path.exists(os.path.join(bodies, body_name)), \
            "the body file must be written under the body dir"
        assert row["sessionId"] == "" and row["sessionName"] == "github:o/r#12", row
        assert row["thread_key"] == "gh:o/r#12", "folding needs thread_key at the top level"
        assert row["detail"]["author"] == {"login": "alice", "role": "collaborator", "id": "42"}

        # 2. A retry is the same fact: nothing is written, the same id comes back.
        again, created2 = ingest(dict(base, summary="改过的摘要，也不该出现"), at_ms=2000, **paths())
        assert not created2 and again["id"] == event["id"], (again, created2)
        assert len(rows()) == 1, "a repeated dedupe_key must not append"

        # 3. The record of acceptance can lag the append: a pending row repairs it.
        os.remove(seen)
        repaired, created3 = ingest(dict(base), at_ms=3000, **paths())
        assert not created3 and len(rows()) == 1 and repaired["id"] == event["id"], rows()
        assert accepted(seen).get(base["dedupe_key"]) == event["id"], "the record must be repaired"

        # 4. Refusals write nothing, and each names the field.
        before = (len(rows()), accepted(seen))
        for broken, hint in ((dict(base, source=""), "source"),
                             (dict(base, thread_key=""), "thread_key"),
                             (dict(base, dedupe_key=""), "dedupe_key"),
                             ({k: v for k, v in base.items() if k != "body"}, "body"),
                             (dict(base, summary=""), "summary"),
                             (dict(base, trust="friend"), "trust"),
                             (dict(base, action_class="auto"), "action_class"),
                             (dict(base, priority="urgent"), "priority"),
                             (dict(base, url="javascript:alert(1)"), "url"),
                             (dict(base, author={"role": "member"}), "author.login")):
            try:
                ingest(broken, at_ms=4000, **paths())
            except Refused as error:
                assert hint in str(error), (hint, str(error))
            else:
                raise AssertionError(f"accepted a broken event: {broken}")
        assert (len(rows()), accepted(seen)) == before, "a refused event must not write"

        # 5. Trust caps the wake: a stranger cannot buy priority, and the ask is kept.
        stranger, _ = ingest(dict(base, dedupe_key="k2", priority="high",
                                  trust="external-unknown"), at_ms=5000, **paths())
        assert stranger["priority"] == "low", stranger
        assert stranger["detail"]["priority_requested"] == "high", stranger
        known, _ = ingest(dict(base, dedupe_key="k3", priority="high",
                               trust="external-known"), at_ms=6000, **paths())
        assert known["priority"] == "high", known
        assert "priority_requested" not in known["detail"], known
        assert stranger["detail"]["action_class"] == "observe", "default class is observe"

        # 6. Summary is cut for the card; the original length stays recorded.
        long_summary = "很长" * 300
        cut, _ = ingest(dict(base, dedupe_key="k4", summary=long_summary), at_ms=7000, **paths())
        assert len(cut["summary"]) == SUMMARY_CHARS and cut["summary"].endswith("…"), cut["summary"]
        assert cut["detail"]["summary_chars"] == len(long_summary), cut["detail"]

        # 7. body_file works, and a relative one is refused rather than guessed.
        body_file = os.path.join(tmp, "body.md")
        with open(body_file, "w", encoding="utf-8") as fh:
            fh.write("来自文件的原文")
        no_body = {k: v for k, v in base.items() if k != "body"}
        from_file, _ = ingest(dict(no_body, dedupe_key="k5", body_file=body_file),
                              at_ms=8000, **paths())
        assert from_file["detail"]["body_bytes"] == len("来自文件的原文".encode()), from_file
        try:
            ingest(dict(no_body, dedupe_key="k6", body_file="relative/body.md"),
                   at_ms=9000, **paths())
        except Refused as error:
            assert "absolute" in str(error), error
        else:
            raise AssertionError("accepted a relative body_file")

        # 8. Folding: one survivor per thread_key, older ones recorded in handled.
        import inbox as inbox_mod
        keep, folded = inbox_mod.fold_superseded(rows(), kinds=("external",))
        by_thread = {}
        for row in keep:
            by_thread.setdefault(row.get("thread_key") or row["detail"]["thread_key"], []).append(row)
        assert all(len(items) == 1 for items in by_thread.values()), by_thread
        assert folded, "the older events on one thread must fold"
        assert all(f["foldedInto"] for f in folded), folded
        assert len(keep) + len(folded) == len(rows()), (len(keep), len(folded), len(rows()))

        # 9. Session events keep their own key: (sessionId, kind) still folds, and a
        #    thread-less row is never folded into another one.
        session_rows = [{"id": "a", "kind": "awaiting_user", "sessionId": "s1", "detectedAtMs": 1},
                        {"id": "b", "kind": "awaiting_user", "sessionId": "s1", "detectedAtMs": 2},
                        {"id": "c", "kind": "new_session", "sessionId": "s1", "detectedAtMs": 3}]
        keep2, folded2 = inbox_mod.fold_superseded(session_rows)
        assert [r["id"] for r in keep2] == ["b", "c"], keep2
        assert [r["id"] for r in folded2] == ["a"], folded2

        # 10. drain: files land, bad ones are set aside with a reason, and a second
        #     drain of the same drop is a no-op.
        drop = os.path.join(tmp, "drop")
        os.makedirs(drop)
        for i, name in enumerate(("a.json", "b.json")):
            with open(os.path.join(drop, name), "w", encoding="utf-8") as fh:
                json.dump(dict(base, dedupe_key=f"drain{i}", thread_key=f"gh:o/r#{i}",
                               source_ref=f"o/r#{i}"), fh)
        with open(os.path.join(drop, "bad.json"), "w", encoding="utf-8") as fh:
            json.dump({"source": "github"}, fh)
        created_count = drain(drop, at_ms=10000, **paths())
        assert created_count == 2, created_count
        assert sorted(os.listdir(os.path.join(drop, "ingested"))) == ["a.json", "b.json"]
        assert os.path.exists(os.path.join(drop, "rejected", "bad.json"))
        assert os.path.exists(os.path.join(drop, "rejected", "bad.json.reason"))
        assert drain(drop, at_ms=11000, **paths()) == 0, "a drained file must not come back"

        # 11. `seen` is the cross-process record, not an in-memory set.
        again_rows = accepted(seen)
        assert len(again_rows) == len(rows()), (len(again_rows), len(rows()))

    print("trigger selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
