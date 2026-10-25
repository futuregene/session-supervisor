#!/usr/bin/env python3
"""Cost ledger: keep the spend a history reset is about to erase.

`future session reset` runs `DELETE FROM runs WHERE session_id=?`
(`future-os/agent/src/session/sqlite_store/transcript.rs:187`), so every
in-place reset also deletes this session's own token record. Measured on
2026-10-25: eight resets left 42 rows starting at epoch 4, and the local sum
(¥2.56) was two days of a bill the app showed as ¥44+.

So `bin/monitor_roll.py` calls `snapshot()` immediately **before** a reset and
appends one line to `state/cost-ledger.jsonl`. The lifetime total is the sum of
those lines plus whatever the live database still holds.

    python3 bin/cost.py                 # current episode + ledger + total
    python3 bin/cost.py --json

Unit prices come from `.future/memory/budget.md` (reverse-engineered from the
`runs` table on 2026-10-16). Never use `credit_cost` for this: its unit is not
CNY.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(WATCH, "state", "cost-ledger.jsonl")
DB = os.path.expanduser(os.path.join("~", ".future", "agent", "agent.db"))

# CNY per million tokens (budget.md).
IN_MISS = 1.00
CACHE_READ = 0.02
OUT = 4.00


def cost_of(input_tokens, cache_read_tokens, output_tokens):
    """CNY for one episode.

    `runs.input_tokens` already includes the cached part, so the miss charge is
    the difference. Charging `input_tokens * IN_MISS` would bill the same tokens
    twice — the error budget.md records as the first of its two aggregation bugs.
    """
    miss = max(0, (input_tokens or 0) - (cache_read_tokens or 0))
    return (miss / 1e6 * IN_MISS
            + (cache_read_tokens or 0) / 1e6 * CACHE_READ
            + (output_tokens or 0) / 1e6 * OUT)


def episode(session, db=DB):
    """What the live database still holds for this session, or None."""
    if not os.path.exists(db):
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT COUNT(*), MIN(epoch), MAX(epoch), SUM(input_tokens),"
                " SUM(cache_read_tokens), SUM(output_tokens), SUM(duration_ms),"
                " MIN(started_at_ms), MAX(completed_at_ms)"
                " FROM runs WHERE session_id=?", (session,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    runs, lo, hi, tin, tcache, tout, dur, first, last = row
    if not runs:
        return None
    return {
        "runs": runs,
        "epochs": [lo, hi],
        "input": tin or 0,
        "cache_read": tcache or 0,
        "output": tout or 0,
        "seconds": round((dur or 0) / 1000.0, 1),
        "first_ms": first,
        "last_ms": last,
        "cost_cny": round(cost_of(tin, tcache, tout), 4),
    }


def read_ledger():
    out = []
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


def snapshot(session, reason=""):
    """Append the pre-reset episode to the ledger. Never raises.

    Called from monitor_roll before the reset: a ledger that cannot be written
    must not block a reset the session needs to stay usable.
    """
    try:
        ep = episode(session)
        if not ep or ep.get("error"):
            return None
        prior = [r for r in read_ledger() if r.get("session") == session]
        known = {tuple(r.get("epochs") or []) for r in prior}
        if tuple(ep["epochs"]) in known:
            return None  # already recorded; a retry must not double-count
        record = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session": session,
            "reason": reason,
            **ep,
        }
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
    except Exception:  # noqa: BLE001 - never block a reset
        return None


def summary(session):
    rows = [r for r in read_ledger() if r.get("session") == session]
    archived = sum(r.get("cost_cny") or 0 for r in rows)
    live = episode(session) or {}
    # A snapshot is taken *before* its reset; if that reset then failed, the same
    # episode is still in the database and would be counted twice. Same epoch
    # range means same rows, so the ledger already holds them.
    known = {tuple(r.get("epochs") or []) for r in rows}
    if tuple(live.get("epochs") or []) in known:
        live = {}
    live_cost = live.get("cost_cny") or 0
    return {
        "session": session,
        "archived_episodes": len(rows),
        "archived_cost_cny": round(archived, 2),
        "live": live,
        "live_cost_cny": round(live_cost, 2),
        "total_cny": round(archived + live_cost, 2),
        "ledger": LEDGER,
    }


def fmt(ms):
    if not isinstance(ms, (int, float)):
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


def main(argv):
    ap = argparse.ArgumentParser(description="Cost ledger for a session's own spend.")
    ap.add_argument("session", nargs="?", help="session id (default: the monitor session)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--snapshot", metavar="REASON", help="record the current episode before a reset")
    a = ap.parse_args(argv[1:])

    session = a.session
    if not session:
        try:
            session = open(os.path.join(WATCH, "state", "monitor_session.txt"),
                           encoding="utf-8").read().strip()
        except FileNotFoundError:
            session = ""
    if not session:
        print("no session given and none registered", file=sys.stderr)
        return 2

    if a.snapshot is not None:
        rec = snapshot(session, a.snapshot)
        print(json.dumps(rec, ensure_ascii=False) if a.json else
              ("recorded " + json.dumps(rec, ensure_ascii=False) if rec else "nothing to record"))
        return 0

    s = summary(session)
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0
    live = s["live"] or {}
    print(f"session {s['session']}")
    print(f"  归档 {s['archived_episodes']} 段 · ¥{s['archived_cost_cny']}")
    if live:
        print(f"  当前库 {live.get('runs')} 次 run · epoch {live.get('epochs')}"
              f" · {fmt(live.get('first_ms'))} → {fmt(live.get('last_ms'))}"
              f" · ¥{s['live_cost_cny']}")
    else:
        print("  当前库 无记录")
    print(f"  合计（本会话可算部分） ¥{s['total_cny']}")
    print("  注意：重置前没走 snapshot 的段落无法补算，服务端 usage 才是权威。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
