#!/usr/bin/env python3
"""recall — search the user's own history, in one place.

Three sources, one command (keeps the supervisor from having to remember which
tool covers which period):

  1. Life Journal (recordings / photo OCR / notes)  -> life-search
  2. This facility's own records (journal/, reports/, inbox handled+archive,
     state/sent.jsonl) -> plain grep, they are small
  3. Local FutureOS sessions (transcripts) -> `future session transcript --grep`

Output is deliberately compact: every hit is clipped, and the total is bounded.
Reading history is exactly where context gets burned (see docs/gotcha-tools.md),
so the default is "tell me where it is", not "paste everything".

  recall.py <query> [--limit N] [--full] [--source journal|life|sessions|facility|all]
  recall.py --timeline 2026-10-14            # what happened that day (life journal)
  recall.py --when "上次聊到掼蛋"             # paraphrase -> searches all three
"""
import os
import re
import subprocess
import sys

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIFE_SEARCH = os.path.expanduser("~/.local/bin/life-search")
FUTURE = future_exe()
CLIP = 180
MAX_BYTES = 6000


def run(args, timeout=90):
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return p.stdout
    except Exception:  # noqa: BLE001
        return ""


def emit(budget, text):
    """Clip a block so one source cannot eat the whole budget."""
    blob = text[: max(0, budget)]
    if len(text) > budget:
        blob += f"\n… [+{len(text) - budget} B clipped]"
    return blob


def search_life(query, limit):
    if not os.path.exists(LIFE_SEARCH):
        return "(life-search not installed)"
    return run([LIFE_SEARCH, "search", query, "--top", str(limit)])


def search_facility(query, limit):
    """grep our own records — they are the durable memory of what the facility did."""
    out = []
    roots = ["journal", "reports", "docs"]
    files = []
    for r in roots:
        for dirpath, _dirs, names in os.walk(os.path.join(WATCH, r)):
            for n in names:
                if n.endswith(".md"):
                    files.append(os.path.join(dirpath, n))
    for extra in ("state/sent.jsonl", "inbox/handled.jsonl"):
        p = os.path.join(WATCH, extra)
        if os.path.exists(p):
            files.append(p)
    for f in files:
        try:
            for i, line in enumerate(open(f, encoding="utf-8", errors="replace"), 1):
                if re.search(re.escape(query), line, re.IGNORECASE):
                    rel = os.path.relpath(f, WATCH)
                    out.append(f"{rel}:{i}: {line.strip()[:CLIP]}")
                    if len(out) >= limit:
                        return "\n".join(out)
        except OSError:
            continue
    return "\n".join(out) if out else "(no hit in facility records)"


def search_sessions(query, limit):
    """Which local sessions mention this? Cheap list first, then peek the top ones."""
    listing = run([FUTURE, "session", "list", "--json"])
    hits = []
    try:
        import json
        for s in json.loads(listing).get("sessions", []):
            name = (s.get("sessionName") or "") + " " + (s.get("firstMessage") or "")
            if re.search(re.escape(query), name, re.IGNORECASE):
                hits.append((s["id"], (s.get("sessionName") or "")[:60], s.get("updatedAtMs")))
    except Exception:  # noqa: BLE001
        pass
    lines = []
    for sid, name, _ts in hits[:limit]:
        body = run([FUTURE, "session", "transcript", "--session", sid,
                    "--grep", query, "--limit", "2", "--truncate", "200"])
        lines.append(f"[session {sid}] {name}\n{body.strip()[:CLIP * 2]}")
    return "\n\n".join(lines) if lines else "(no session title matched; try --source all)"


def timeline(when):
    if not os.path.exists(LIFE_SEARCH):
        return "(life-search not installed)"
    return run([LIFE_SEARCH, "timeline", "--from", when, "--to", when, "--compact"])


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    when = None
    if "--timeline" in argv:
        when = argv[argv.index("--timeline") + 1]
    source = "all"
    if "--source" in argv:
        source = argv[argv.index("--source") + 1]
    limit = 6
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    query = argv[0] if argv[0] not in ("--timeline", "--source", "--limit") else ""

    if when:
        print(f"=== life journal timeline: {when} ===")
        print(emit(MAX_BYTES, timeline(when)))
        return 0
    if not query:
        print(__doc__)
        return 2

    print(f"# recall {query!r} (source={source}, limit={limit})\n")
    budget = MAX_BYTES // 3
    if source in ("all", "life"):
        print("=== 1. Life Journal（录音/照片/笔记）===")
        print(emit(budget, search_life(query, limit)))
        print()
    if source in ("all", "facility", "journal"):
        print("=== 2. 本设施记录（journal/reports/docs/发送台账）===")
        print(emit(budget, search_facility(query, limit)))
        print()
    if source in ("all", "sessions"):
        print("=== 3. 本地会话 ===")
        print(emit(budget, search_sessions(query, limit)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))