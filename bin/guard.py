#!/usr/bin/env python3
"""Anti-loop guard: how often may the supervisor speak into one session?

  guard.py check <sessionId> [--limit-per-turn N]   exit 0 allowed / exit 1 blocked
  guard.py record <sessionId> [note]
  guard.py show [sessionId]

Limits come from bin/config.json (defaults: 1 message per session per 4h,
3 per session per day, 3 sessions touched per turn).
"""
import json
import os
import subprocess
import sys
import time

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = config_path()
SENT = os.path.join(WATCH, "state", "sent.jsonl")

DEFAULTS = {"max_messages_per_session_4h": 1,
            "max_messages_per_session_day": 3,
            "max_messages_per_turn": 3,
            "count_as_turn_window_seconds": 600}


def cfg():
    try:
        c = json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:
        c = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in c.items() if k in DEFAULTS})
    return out


def read_sent():
    rows = []
    try:
        with open(SENT, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return rows


CACHE = os.path.join(WATCH, "state", "observed_sends.json")
# The supervisor's relayed messages carry one of these on their first line; the
# bracketed form is the pre-2026-10-16 spelling, still counted for old sends.
MARKERS = ("会话总管转达", "[会话总管")


def observed_sends(sid, future_bin=None):
    """Count the supervisor's messages into `sid` from the session's own records.

    Returns (within_4h, within_24h). Cached briefly: one pass over recent sessions.
    """
    exe = future_exe(future_bin)
    env = dict(os.environ)
    env["PATH"] = tool_path()
    now = time.time()
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        cache = {}
    if now - float(cache.get("ts") or 0) > 180 or sid not in (cache.get("hits") or {}):
        hits = {}
        try:
            p = subprocess.run([exe, "session", "list", "--json"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=45, env=env)
            for s in json.loads(p.stdout).get("sessions", []):
                if now * 1000 - int(s.get("updatedAtMs") or 0) > 36 * 3600 * 1000:
                    continue
                p2 = subprocess.run([exe, "session", "transcript", "--session", s["id"],
                                     "--select", "user", "--limit", "3", "--json"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, env=env)
                if p2.returncode != 0:
                    continue
                for e in json.loads(p2.stdout).get("entries", []):
                    txt = " ".join(b.get("text", "") for b in e.get("blocks", [])
                                   if b.get("kind") == "text")
                    if any(m in txt for m in MARKERS):
                        hits.setdefault(s["id"], []).append(int(e.get("createdAtMs") or 0))
        except Exception:
            return (0, 0)
        cache = {"ts": now, "hits": hits}
        try:
            with open(CACHE, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception:
            pass
    stamps = (cache.get("hits") or {}).get(sid) or []
    now_ms = now * 1000
    return (sum(1 for t in stamps if now_ms - t < 4 * 3600 * 1000),
            sum(1 for t in stamps if now_ms - t < 86400 * 1000))


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    c = cfg()
    rows = read_sent()
    now = time.time()

    if cmd == "check":
        sid = argv[1]
        mine = [r for r in rows if r.get("sessionId") == sid]
        n4 = sum(1 for r in mine if now - float(r.get("ts") or 0) < 4 * 3600)
        nd = sum(1 for r in mine if now - float(r.get("ts") or 0) < 86400)
        # The supervisor does not always record its own sends, so also trust the
        # sessions' own transcripts: count turns that carry the origin marker.
        real4, real24 = observed_sends(sid, c.get("future_bin"))
        n4 = max(n4, real4)
        nd = max(nd, real24)
        turn_window = float(c.get("count_as_turn_window_seconds") or 600)
        n_turn = len({r.get("sessionId") for r in rows if now - float(r.get("ts") or 0) < turn_window})
        # `c.get`, never `c[...]`: every other reader in bin/ tolerates a missing key
        # by falling back to its default, and a config file that omitted one used to
        # crash the gate that decides whether a message may be sent at all.
        limit_4h = int(c.get("max_messages_per_session_4h") or 1)
        limit_day = int(c.get("max_messages_per_session_day") or 3)
        limit_turn = int(c.get("max_messages_per_turn") or 3)
        if n4 >= limit_4h:
            print(f"BLOCKED session hit 4h limit ({n4}/{limit_4h})")
            return 1
        if nd >= limit_day:
            print(f"BLOCKED session hit daily limit ({nd}/{limit_day})")
            return 1
        if sid not in {r.get("sessionId") for r in rows if now - float(r.get("ts") or 0) < turn_window} \
                and n_turn >= limit_turn:
            print(f"BLOCKED turn touched {n_turn} sessions already (max {limit_turn})")
            return 1
        print(f"OK (4h {n4}, 24h {nd}, sessions this turn {n_turn})")
        return 0

    if cmd == "record":
        sid = argv[1]
        note = argv[2] if len(argv) > 2 else ""
        os.makedirs(os.path.dirname(SENT), exist_ok=True)
        with open(SENT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now, "sessionId": sid, "note": note}, ensure_ascii=False) + "\n")
        print("recorded")
        return 0

    if cmd == "show":
        sid = argv[1] if len(argv) > 1 else None
        for r in rows[-40:]:
            if sid and r.get("sessionId") != sid:
                continue
            print(time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"]))), r.get("sessionId"), r.get("note"))
        print(f"-- {len(rows)} total")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
