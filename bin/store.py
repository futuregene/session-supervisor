#!/usr/bin/env python3
"""The supervisor's hot state and ledgers, in one SQLite database.

Why this exists: every 30 s `bin/scan.py` read and rewrote *all* of `state/state.json`
(626 KB, 2738 sessions) and `bin/approvals.py` rewrote `state/approval-cache.json`
(177 KB, 1702 sessions), i.e. ~1.7 GB/day of rewriting to record that almost nothing
changed. The ledgers were the same shape at a smaller scale (`inbox/handled.jsonl`
3392 rows, `state/wakes.jsonl` 1130, `state/sent.jsonl` 416, `state/dispatch.jsonl`
120). This module gives those three roles — per-session hot state, the event ledger
(pending + handled, one table), and the append-only ledgers — a store whose write
cost is O(rows that actually changed).

What is deliberately **not** in here: `journal/`, `reports/`, `.future/memory/`,
`logs/`. Those are read by people, grepped, and quoted as evidence; they stay plain
text. The split is "machine bookkeeping → database, human record → text".

The migration is in three independently revertible steps (see `docs/state-store.md`):

  1. this file, additive: `--from-json` imports today's files, `--export-json`
     writes them back in exactly today's formats. Nothing calls it yet.
  2. dual-write per module (`scan`, `inbox`, `approvals`, `dispatch`, `taskruns`,
     `decide`, `sent`): the module keeps writing its legacy file, and mirrors the
     same write here first. A mirror failure is logged and never changes the
     caller's result — the legacy path stays authoritative, so the scanner cannot
     stop because of this.
  3. stop the legacy writes per module (`--disable <module>` is the kill switch for
     every step), and `--export-json` remains the way back to the file layout.

Every mirror is gated twice: by the module switch in `state/store-mode.json`, and by
the target path — a selftest writing into a temp directory never touches the live
database, without having to know this module exists.

  store.py --from-json [--root DIR] [--db PATH]     import today's files
  store.py --export-json DIR [--db PATH] [--root DIR]
  store.py stat [--root DIR]
  store.py --check-consistency [--root DIR]         live DB vs live files
  store.py --selftest                               fixture round-trip (CI-safe)
  store.py --disable scan,inbox | --enable scan     the per-module switch
  store.py modules                                  what is currently on
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(WATCH, "state", "supervisor.db")
MODE_PATH = os.path.join(WATCH, "state", "store-mode.json")
FAIL_LOG = os.path.join(WATCH, "logs", "store.log")

SCHEMA_VERSION = 1

# One name per legacy file, and the only place the layout is written down. Export
# reproduces these paths under the output directory; `--from-json` reads them under
# the root. Keeping the list in one place is what makes "the database can always be
# turned back into these files" checkable instead of aspirational.
LEGACY = {
    "state": "state/state.json",
    "health": "state/health.json",
    "pending": "inbox/pending.jsonl",
    "handled": "inbox/handled.jsonl",
    "approval_cache": "state/approval-cache.json",
    "approval_ledger": "state/approvals.jsonl",
    "dispatch": "state/dispatch.jsonl",
    "sent": "state/sent.jsonl",
    "wakes": "state/wakes.jsonl",
    "taskruns": "state/taskruns.json",
}

# Which module owns which writes. Turn one off and only that module goes back to
# files-only; the rest keep mirroring. Default: on (step 2 shipped enabled).
MODULES = ("scan", "inbox", "approvals", "dispatch", "taskruns", "decide", "sent")

# How many per-session dedupe keys are remembered. This is `scan.py`'s `del seen[:-25]`
# written down once: the store must not silently widen the window, or an event that
# the file-based logic would legitimately re-emit would be suppressed instead — a
# behaviour change that no consistency check would catch.
EVENT_KEY_WINDOW = 25

# How many health rows to keep. 288 × 30 s = 24 h of scan history, which is the span
# the "why did the scan go blind" question is asked over (incident-9: 18.4 h).
HEALTH_KEEP = 288


# --------------------------------------------------------------------- plumbing


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    # A torn line is one lost row, never a lost file: the same
                    # tolerance the file readers have, so export does not become
                    # stricter than the code it is replacing.
                    continue
    except OSError:
        pass
    return rows


def _write_json(path, obj, indent=1, sort_keys=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    os.replace(tmp, path)


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _dumps(obj):
    """One canonical spelling of a JSON value, for "did this row change" comparisons."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def connect(path=None, create=True):
    """Open the store with the pragmas a second writer needs to not fail.

    `journal_mode=WAL` lets the 30 s scanner and a wake turn read while the other
    writes; `busy_timeout=5000` turns "database is locked" into "wait for the other
    transaction" — the failure this assertion exists for is a scan that quietly stops
    landing its state. `synchronous=NORMAL` is the documented pairing with WAL: it
    survives a process crash (`kill -9`, which is the case measured here) and only
    risks the last commits on an OS crash, which is the trade the old `os.replace`
    of a whole JSON file already made (it never fsynced either).
    """
    path = os.fspath(path or DB_PATH)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    if create:
        ensure_schema(conn)
    return conn


@contextmanager
def tx(conn):
    """One write transaction that waits instead of failing when another writer holds it.

    `BEGIN IMMEDIATE` takes the write lock up front. With a deferred transaction the
    same work fails immediately with SQLITE_BUSY if another connection got there
    first and the transaction has to upgrade its lock — which is exactly how
    `busy_timeout` ends up looking useless under concurrency.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def _write_block(conn, in_tx=False):
    """One write transaction, or the one the caller already opened."""
    if in_tx:
        yield conn
        return
    with tx(conn):
        yield conn


def is_live(path, name, root=None):
    """True when `path` is this deployment's own file for `name`.

    This is what keeps a selftest writing into a temp directory from also writing
    the live database: the module passes the path it is about to write, and the
    mirror happens only when that path *is* the live one. No selftest had to learn
    about this module for that to hold.
    """
    try:
        want = paths(root)[name]
    except KeyError:
        return False
    return path is not None and _norm(path) == _norm(want)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Per-session hot state, one row per session. The JSON is the session's record
-- verbatim, so nothing in this migration has to interpret it: `sig`, `emitted`,
-- `lastOrdinal`, `pendingNew`, whatever a later scan adds, round-trips as-is.
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  last_upd_ms INTEGER NOT NULL DEFAULT 0,
  state_json TEXT NOT NULL);

-- The event ledger: pending and handled in one table, told apart by `state`.
-- `id` is NOT unique on purpose — measured on the live inbox, 3392 handled rows
-- carry 3 duplicated ids (a task_review id that repeated across polls), so making
-- it a primary key would silently drop rows on import.
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL,
  kind TEXT NOT NULL,
  priority TEXT NOT NULL,
  session_id TEXT,
  emitted_ms INTEGER NOT NULL DEFAULT 0,
  handled_ms INTEGER,
  handled_seq INTEGER,
  state TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  row_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_state ON events(state, emitted_ms);
CREATE INDEX IF NOT EXISTS events_by_id ON events(id);

-- Dedupe: one row per emitted key, bounded per session to the same window the
-- file-based `emitted` list uses (EVENT_KEY_WINDOW).
CREATE TABLE IF NOT EXISTS event_keys(
  key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  kind TEXT,
  seq INTEGER NOT NULL,
  PRIMARY KEY (session_id, key));
CREATE INDEX IF NOT EXISTS event_keys_by_session ON event_keys(session_id, seq);

CREATE TABLE IF NOT EXISTS approvals(
  key TEXT PRIMARY KEY,
  ts REAL NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approval_ledger(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL DEFAULT 0,
  session_id TEXT,
  request_id TEXT,
  action TEXT,
  row_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS dispatch(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session TEXT,
  ts TEXT,
  closed INTEGER NOT NULL DEFAULT 0,
  row_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dispatch_by_session ON dispatch(session);

CREATE TABLE IF NOT EXISTS sent(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL DEFAULT 0,
  session_id TEXT,
  row_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sent_by_ts ON sent(ts);

CREATE TABLE IF NOT EXISTS wakes(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL DEFAULT 0,
  reason TEXT,
  pending INTEGER,
  high_medium INTEGER,
  row_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS task_review(
  task_id TEXT PRIMARY KEY,
  last_reviewed_ms INTEGER NOT NULL DEFAULT 0,
  seen_runs INTEGER NOT NULL DEFAULT 0,
  state_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS health(
  ts REAL PRIMARY KEY,
  ok INTEGER,
  detail_json TEXT NOT NULL);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        with tx(conn):
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                         (str(SCHEMA_VERSION),))


def paths(root=None):
    root = root or WATCH
    return {name: os.path.join(root, rel) for name, rel in LEGACY.items()}


def _norm(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


# ------------------------------------------------------------------- import


def import_from_files(conn, root=None, log=None, in_tx=False):
    """Load every legacy file into the store. Returns per-file row counts.

    Idempotent in the sense that matters for a rollout: it is called once, inside the
    transaction that marks the store seeded, so two processes racing to seed cannot
    both import (the second sees the marker and returns).
    """
    root = root or WATCH
    p = paths(root)
    counts = {}

    with _write_block(conn, in_tx):
        # --- per-session hot state + the small top-level keys
        state = _load_json(p["state"], None)
        if isinstance(state, dict):
            sessions = state.get("sessions") or {}
            for sid, rec in sessions.items():
                conn.execute(
                    "INSERT OR REPLACE INTO sessions(id, last_upd_ms, state_json) VALUES(?,?,?)",
                    (sid, int((rec or {}).get("lastUpdMs") or 0), _dumps(rec)))
                keys = (rec or {}).get("emitted") or []
                keep = list(keys)[-EVENT_KEY_WINDOW:]
                for i, key in enumerate(keep):
                    conn.execute(
                        "INSERT OR REPLACE INTO event_keys(key, session_id, kind, seq) "
                        "VALUES(?,?,?,?)",
                        (str(key), sid, str(key).split("|", 1)[0], i))
            counts["sessions"] = len(sessions)
            for key, value in state.items():
                if key == "sessions":
                    continue
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                             ("state." + key, _dumps(value)))
            counts["state_keys"] = len(state) - (1 if "sessions" in state else 0)

        # --- pending before handled, so a pending row keeps the lower seq
        pending = _read_jsonl(p["pending"])
        for ev in pending:
            _insert_event(conn, ev, state="pending")
        counts["pending"] = len(pending)

        handled = _read_jsonl(p["handled"])
        for i, ev in enumerate(handled, 1):
            _insert_event(conn, ev, state="handled", handled_seq=i)
        counts["handled"] = len(handled)

        # --- health: only the latest snapshot survives in the file; one row here
        health = _load_json(p["health"], None)
        if isinstance(health, dict):
            conn.execute("INSERT OR REPLACE INTO health(ts, ok, detail_json) VALUES(?,?,?)",
                         (float(health.get("ts") or 0), int(bool(health.get("ok"))),
                          _dumps(health)))
            counts["health"] = 1

        cache = _load_json(p["approval_cache"], None)
        if isinstance(cache, dict):
            for key, value in cache.items():
                ts = float((value or {}).get("checkedAt") or 0) if isinstance(value, dict) else 0.0
                conn.execute("INSERT OR REPLACE INTO approvals(key, ts, payload_json) VALUES(?,?,?)",
                             (key, ts, _dumps(value)))
            counts["approvals"] = len(cache)

        ledger = _read_jsonl(p["approval_ledger"])
        for row in ledger:
            conn.execute(
                "INSERT INTO approval_ledger(ts, session_id, request_id, action, row_json) "
                "VALUES(?,?,?,?,?)",
                (float(row.get("ts") or 0), row.get("sessionId"), row.get("requestId"),
                 row.get("action"), _dumps(row)))
        counts["approval_ledger"] = len(ledger)

        dispatch = _read_jsonl(p["dispatch"])
        for row in dispatch:
            conn.execute("INSERT INTO dispatch(session, ts, closed, row_json) VALUES(?,?,?,?)",
                         (row.get("session"), row.get("ts"), int(bool(row.get("closed"))),
                          _dumps(row)))
        counts["dispatch"] = len(dispatch)

        sent = _read_jsonl(p["sent"])
        for row in sent:
            conn.execute("INSERT INTO sent(ts, session_id, row_json) VALUES(?,?,?)",
                         (float(row.get("ts") or 0), row.get("sessionId"), _dumps(row)))
        counts["sent"] = len(sent)

        wakes = _read_jsonl(p["wakes"])
        for row in wakes:
            conn.execute(
                "INSERT INTO wakes(ts, reason, pending, high_medium, row_json) VALUES(?,?,?,?,?)",
                (float(row.get("ts") or 0), row.get("reason"),
                 _int_or_none(row.get("pending")), _int_or_none(row.get("highMedium")),
                 _dumps(row)))
        counts["wakes"] = len(wakes)

        taskruns = _load_json(p["taskruns"], None)
        if isinstance(taskruns, dict):
            tasks = taskruns.get("tasks") or {}
            for tid, rec in tasks.items():
                rec = rec or {}
                conn.execute(
                    "INSERT OR REPLACE INTO task_review(task_id, last_reviewed_ms, seen_runs, "
                    "state_json) VALUES(?,?,?,?)",
                    (tid, int(rec.get("lastReviewMs") or 0),
                     int(rec.get("runsSinceReview") or 0), _dumps(rec)))
            counts["tasks"] = len(tasks)
            for key in ("lastPollMs", "lastError"):
                if key in taskruns:
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                                 ("taskruns." + key, _dumps(taskruns[key])))
        if log:
            log(f"imported {counts}")
    return counts


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _insert_event(conn, ev, state, handled_seq=None):
    conn.execute(
        "INSERT INTO events(id, kind, priority, session_id, emitted_ms, handled_ms, "
        "handled_seq, state, detail_json, row_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(ev.get("id") or ""), str(ev.get("kind") or ""), str(ev.get("priority") or ""),
         ev.get("sessionId"), int(ev.get("detectedAtMs") or ev.get("emittedAtMs") or 0),
         _int_or_none(ev.get("handledAtMs")), handled_seq, state,
         _dumps(ev.get("detail") or {}), _dumps(ev)))


# ------------------------------------------------------------------- export


def export_to_files(conn, out_root):
    """Write the store back out in today's file formats. The revert path.

    Read-only with respect to the database: `--export-json` must never be the thing
    that loses data, so it only ever SELECTs.
    """
    p = paths(out_root)
    counts = {}

    # --- state.json: sessions rebuilt from rows, everything else from meta
    state = {}
    sessions = {}
    for row in conn.execute("SELECT id, state_json FROM sessions ORDER BY id"):
        sessions[row["id"]] = json.loads(row["state_json"])
    for row in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'state.%'"):
        state[row["key"][len("state."):]] = json.loads(row["value"])
    state["sessions"] = sessions
    _write_json(p["state"], state)
    counts["sessions"] = len(sessions)

    pending = [json.loads(r["row_json"]) for r in conn.execute(
        "SELECT row_json FROM events WHERE state='pending' ORDER BY seq")]
    _write_jsonl(p["pending"], pending)
    counts["pending"] = len(pending)

    handled = [json.loads(r["row_json"]) for r in conn.execute(
        "SELECT row_json FROM events WHERE state='handled' "
        "ORDER BY COALESCE(handled_seq, 9223372036854775807), seq")]
    _write_jsonl(p["handled"], handled)
    counts["handled"] = len(handled)

    health = conn.execute("SELECT detail_json FROM health ORDER BY ts DESC LIMIT 1").fetchone()
    _write_json(p["health"], json.loads(health["detail_json"]) if health
                else {"ts": 0, "ok": False, "error": "no health rows in the store"})
    counts["health"] = 1 if health else 0

    cache = {r["key"]: json.loads(r["payload_json"])
             for r in conn.execute("SELECT key, payload_json FROM approvals")}
    _write_json(p["approval_cache"], cache, indent=None, sort_keys=True)
    counts["approvals"] = len(cache)

    _write_jsonl(p["approval_ledger"], [json.loads(r["row_json"]) for r in conn.execute(
        "SELECT row_json FROM approval_ledger ORDER BY seq")])

    _write_jsonl(p["dispatch"], [json.loads(r["row_json"]) for r in conn.execute(
        "SELECT row_json FROM dispatch ORDER BY seq")])

    _write_jsonl(p["sent"], [json.loads(r["row_json"]) for r in conn.execute(
        "SELECT row_json FROM sent ORDER BY seq")])

    _write_jsonl(p["wakes"], [json.loads(r["row_json"]) for r in conn.execute(
        "SELECT row_json FROM wakes ORDER BY seq")])

    taskruns = {"tasks": {r["task_id"]: json.loads(r["state_json"])
                          for r in conn.execute("SELECT task_id, state_json FROM task_review")}}
    for row in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'taskruns.%'"):
        taskruns[row["key"][len("taskruns."):]] = json.loads(row["value"])
    _write_json(p["taskruns"], taskruns)
    counts["tasks"] = len(taskruns["tasks"])
    return counts


# ------------------------------------------------------- semantic comparison


def _json_equal(a, b):
    return _load_json(a, None) == _load_json(b, None)


def _jsonl_equal(a, b):
    """Ordered comparison for a log whose order is part of its meaning.

    `handled.jsonl` is exported in *handling* order, not event-id order, and the
    import assigns `handled_seq` from the file's line number, so this can be strict.
    """
    return _read_jsonl(a) == _read_jsonl(b)


def compare_trees(left_root, right_root):
    """Every legacy file in two trees, compared semantically. Returns problems."""
    lp, rp = paths(left_root), paths(right_root)
    problems = []
    for name in ("state", "health", "approval_cache", "taskruns"):
        if not os.path.exists(lp[name]) and not os.path.exists(rp[name]):
            continue
        if not _json_equal(lp[name], rp[name]):
            problems.append(name)
    for name in ("pending", "handled", "approval_ledger", "dispatch", "sent", "wakes"):
        la, ra = _read_jsonl(lp[name]), _read_jsonl(rp[name])
        if la != ra:
            problems.append(f"{name} ({len(la)} vs {len(ra)} rows)")
    return problems


def diff_trees(file_root, out_root):
    """What the files have and the database export does not — and the reverse.

    Two different questions hide behind "the store and the files agree":

      * **missing** — the file has a row the store does not. That is a mirror that
        failed, i.e. a write that reached the file and not the database. This is the
        only one that is a defect while dual-writing.
      * **extra** — the store has a row the file does not. While every module writes
        the store *first* and the file second, this is the normal one-millisecond
        window between the two writes, not a defect.

    Keeping them apart is what makes this checkable on a live deployment: an exact
    comparison would go red on a race nobody cares about, and then someone would
    weaken it until it caught nothing.
    """
    from collections import Counter
    lp, rp = paths(file_root), paths(out_root)
    diff = {}

    def rows(path):
        return Counter(_dumps(r) for r in _read_jsonl(path))

    for name in ("pending", "handled", "approval_ledger", "dispatch", "sent", "wakes"):
        a, b = rows(lp[name]), rows(rp[name])
        diff[name] = {"missing": sum((a - b).values()), "extra": sum((b - a).values())}

    for name in ("state", "health", "approval_cache", "taskruns"):
        fa, fb = _load_json(lp[name], None), _load_json(rp[name], None)
        if isinstance(fa, dict) and isinstance(fb, dict):
            changed = [k for k in fa if k in fb and fa[k] != fb[k]]
            diff[name] = {"missing": len([k for k in fa if k not in fb]) + len(changed),
                          "extra": len([k for k in fb if k not in fa]),
                          "changed": changed[:5]}
        else:
            diff[name] = {"missing": 0 if fa == fb else (0 if fa is None else 1),
                          "extra": 0 if fa == fb else (1 if fb is not None else 0)}
    return diff


# ------------------------------------------------------------- dedupe window


def event_key(kind, key_extra, stamp, stable=False):
    """The same identity `scan.event_key` builds, spelled once here for the store.

    Duplicated rather than imported because `scan.py` is a script whose import has
    side effects (it resolves paths and imports sibling modules); the shape is pinned
    by a selftest that compares the two implementations over generated inputs.
    """
    return f"{kind}|{'stable' if stable else stamp}|{key_extra}"


def dedupe_admit(conn, session_id, key, kind=None, window=EVENT_KEY_WINDOW):
    """True the first time this key is seen, within the per-session window.

    Returns False when the key is already remembered. The window is trimmed to
    `window` keys per session — the same `del seen[:-25]` the file-based `emitted`
    list does, which is why this can be compared for equality against it.
    """
    kind = kind or str(key).split("|", 1)[0]
    with tx(conn):
        if conn.execute("SELECT 1 FROM event_keys WHERE session_id=? AND key=?",
                        (session_id, key)).fetchone():
            return False
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM event_keys "
                           "WHERE session_id=?", (session_id,)).fetchone()[0]
        conn.execute("INSERT INTO event_keys(key, session_id, kind, seq) VALUES(?,?,?,?)",
                     (key, session_id, kind, seq))
        stale = [r["key"] for r in conn.execute(
            "SELECT key FROM event_keys WHERE session_id=? ORDER BY seq", (session_id,))][:-window]
        for key_old in stale:
            conn.execute("DELETE FROM event_keys WHERE session_id=? AND key=?",
                         (session_id, key_old))
    return True


def sync_event_keys(conn, session_id, keys, window=EVENT_KEY_WINDOW):
    """Make the stored window for one session equal to `keys` (its `emitted` list)."""
    keep = [str(k) for k in list(keys)[-window:]]
    have = [r["key"] for r in conn.execute(
        "SELECT key FROM event_keys WHERE session_id=? ORDER BY seq", (session_id,))]
    if have == keep:
        return
    conn.execute("DELETE FROM event_keys WHERE session_id=?", (session_id,))
    for i, key in enumerate(keep):
        conn.execute("INSERT OR REPLACE INTO event_keys(key, session_id, kind, seq) "
                     "VALUES(?,?,?,?)", (key, session_id, str(key).split("|", 1)[0], i))


# ---------------------------------------------------------------- mirror API


_mode_cache = {"mtime": None, "value": None, "path": None}


def _mode(path=None):
    path = path or MODE_PATH
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {name: True for name in MODULES}
    cached = _mode_cache
    if cached["path"] == path and cached["mtime"] == mtime:
        return cached["value"]
    data = _load_json(path, {})
    value = {name: bool((data.get("dual_write") or {}).get(name, True)) for name in MODULES}
    _mode_cache.update({"path": path, "mtime": mtime, "value": value})
    return value


def enabled(module, mode_path=None):
    """Is this module mirroring into the store right now?"""
    return bool(_mode(mode_path).get(module, True))


def set_mode(module, on, mode_path=None):
    """Flip one module's switch. This is the per-module revert step."""
    path = mode_path or MODE_PATH
    data = _load_json(path, {})
    if not isinstance(data, dict):
        data = {}
    dual = dict(data.get("dual_write") or {})
    dual[module] = bool(on)
    data["dual_write"] = dual
    data["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_json(path, data)
    return dual


def log_failure(module, exc):
    """A mirror failure must never be silent, and must never reach the caller."""
    try:
        os.makedirs(os.path.dirname(FAIL_LOG), exist_ok=True)
        with open(FAIL_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{module}] mirror failed: "
                     f"{type(exc).__name__}: {exc}\n")
    except OSError:
        pass


_conn = None


def live_conn(db_path=None):
    """The process-wide connection, seeded from the legacy files on first use.

    Seeding lazily is what makes step 2 deployable without a maintenance window: the
    first scan after the merge imports the files it is about to keep writing, inside
    one transaction guarded by the `seeded_at` marker, so a second process racing it
    imports nothing.
    """
    global _conn
    if _conn is not None:
        return _conn
    conn = connect(db_path or DB_PATH)
    with tx(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='seeded_at'").fetchone()
        if row is None:
            # Import inside the same transaction that marks the store seeded, so two
            # processes racing on the first scan cannot both import the files.
            import_from_files(conn, in_tx=True)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('seeded_at', ?)",
                         (str(int(time.time())),))
    _conn = conn
    return conn


def close():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        finally:
            _conn = None


def mirror_at(module, action, path, name, *args, **kwargs):
    """Mirror only when `path` is the live file for `name` (see `is_live`)."""
    if not is_live(path, name):
        return None
    return mirror(module, action, *args, **kwargs)


def mirror_inbox(action, rows, pending_path, handled_path, *args, **kwargs):
    """The inbox owns two files; mirror only when both are the live pair."""
    if not (is_live(pending_path, "pending") and is_live(handled_path, "handled")):
        return None
    return mirror("inbox", action, rows, *args, **kwargs)


def mirror(module, action, *args, **kwargs):
    """Run one store write for a module; never raise, never change its result."""
    try:
        if not enabled(module):
            return None
        return action(live_conn(), *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the legacy write must still happen
        log_failure(module, exc)
        _close_quietly()
        return None


def _close_quietly():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
        _conn = None


# ------------------------------------------------------------ write actions


def write_state(conn, state, session_ids=None):
    """Mirror `state.json`: only the sessions whose record actually changed.

    `session_ids` lets a caller say "these are the ones I touched" (the scan knows);
    without it every session's JSON is compared against the stored row, which is a
    read of ~600 KB and no write at all. Either way the write is O(changes): the
    whole point of the exercise.
    """
    sessions = state.get("sessions") or {}
    before = {r["id"]: r["state_json"] for r in conn.execute("SELECT id, state_json FROM sessions")}
    changed, removed = [], [sid for sid in before if sid not in sessions]
    with tx(conn):
        for sid, rec in sessions.items():
            payload = _dumps(rec)
            if before.get(sid) == payload:
                continue
            changed.append(sid)
            conn.execute(
                "INSERT INTO sessions(id, last_upd_ms, state_json) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET last_upd_ms=excluded.last_upd_ms, "
                "state_json=excluded.state_json",
                (sid, int((rec or {}).get("lastUpdMs") or 0), payload))
            sync_event_keys(conn, sid, (rec or {}).get("emitted") or [])
        for sid in removed:
            conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            conn.execute("DELETE FROM event_keys WHERE session_id=?", (sid,))
        for key, value in state.items():
            if key == "sessions":
                continue
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                         ("state." + key, _dumps(value)))
        # A top-level key that disappeared from the document must disappear here too,
        # or `--export-json` would resurrect it and the file would gain a key the
        # running code never wrote.
        present = ["state." + k for k in state if k != "sessions"]
        stale = [r["key"] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'state.%'")]
        for key in stale:
            if key not in present:
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
    return {"changed": len(changed), "removed": len(removed), "unchanged": len(sessions) - len(changed)}


def write_health(conn, health):
    """Append one scan's health row and keep the last HEALTH_KEEP."""
    ts = float(health.get("ts") or time.time())
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO health(ts, ok, detail_json) VALUES(?,?,?)",
                     (ts, int(bool(health.get("ok"))), _dumps(health)))
        conn.execute("DELETE FROM health WHERE ts NOT IN "
                     "(SELECT ts FROM health ORDER BY ts DESC LIMIT ?)", (HEALTH_KEEP,))
    return 1


def write_events(conn, events):
    """Append new pending events. An id already pending is not inserted twice."""
    added = 0
    with tx(conn):
        for ev in events or []:
            exists = conn.execute(
                "SELECT 1 FROM events WHERE id=? AND state='pending'",
                (str(ev.get("id") or ""),)).fetchone()
            if exists:
                continue
            _insert_event(conn, ev, state="pending")
            added += 1
    return added


def mark_handled(conn, rows):
    """Move events to handled (ack or fold), preserving handling order.

    A row the store has never seen is inserted as handled rather than dropped: the
    store must be able to answer "what happened to that event" for events that were
    pending before the database was seeded.
    """
    moved = 0
    with tx(conn):
        next_seq = (conn.execute("SELECT COALESCE(MAX(handled_seq), 0) FROM events")
                    .fetchone()[0]) + 1
        for rec in rows or []:
            payload = _dumps(rec)
            handled_at = _int_or_none(rec.get("handledAtMs")) or _now_ms()
            cur = conn.execute("UPDATE events SET state='handled', handled_ms=?, handled_seq=?, "
                               "row_json=? WHERE id=? AND state='pending'",
                               (handled_at, next_seq, payload, str(rec.get("id") or "")))
            if cur.rowcount:
                moved += 1
                next_seq += 1
                continue
            already = conn.execute(
                "SELECT 1 FROM events WHERE id=? AND state='handled' AND row_json=?",
                (str(rec.get("id") or ""), payload)).fetchone()
            if already:
                # The *same* handling act replayed (a crash between the database and
                # the file, then a retry) is a no-op. A different act that reuses an
                # id is a new row, because the file appends one too: `task_review`
                # genuinely produced the same id on three separate polls.
                continue
            _insert_event(conn, rec, state="handled", handled_seq=next_seq)
            moved += 1
            next_seq += 1
    return moved


def write_approval_cache(conn, cache):
    """Sync the per-session approval cache: upsert what changed, drop what left."""
    changed = 0
    have = {r["key"]: r["payload_json"] for r in conn.execute(
        "SELECT key, payload_json FROM approvals")}
    with tx(conn):
        for key, value in (cache or {}).items():
            payload = _dumps(value)
            if have.get(key) == payload:
                continue
            changed += 1
            ts = float((value or {}).get("checkedAt") or 0) if isinstance(value, dict) else 0.0
            conn.execute("INSERT OR REPLACE INTO approvals(key, ts, payload_json) "
                         "VALUES(?,?,?)", (key, ts, payload))
        for key in have:
            if key not in (cache or {}):
                conn.execute("DELETE FROM approvals WHERE key=?", (key,))
    return {"changed": changed, "removed": len([k for k in have if k not in (cache or {})])}


def append_approval_ledger(conn, row):
    with tx(conn):
        conn.execute(
            "INSERT INTO approval_ledger(ts, session_id, request_id, action, row_json) "
            "VALUES(?,?,?,?,?)",
            (float(row.get("ts") or 0), row.get("sessionId"), row.get("requestId"),
             row.get("action"), _dumps(row)))
    return 1


def append_dispatch(conn, row):
    with tx(conn):
        conn.execute("INSERT INTO dispatch(session, ts, closed, row_json) VALUES(?,?,?,?)",
                     (row.get("session"), row.get("ts"), int(bool(row.get("closed"))),
                      _dumps(row)))
    return 1


def prune_dispatch(conn, sessions):
    """`dispatch.prune` drops a finished session whole; mirror exactly that."""
    sessions = list(sessions or ())
    if not sessions:
        return 0
    dropped = 0
    with tx(conn):
        for sid in sessions:
            dropped += conn.execute("DELETE FROM dispatch WHERE session=?", (sid,)).rowcount
    return dropped


def append_sent(conn, row):
    with tx(conn):
        conn.execute("INSERT INTO sent(ts, session_id, row_json) VALUES(?,?,?)",
                     (float(row.get("ts") or 0), row.get("sessionId"), _dumps(row)))
    return 1


def append_wake(conn, row):
    with tx(conn):
        conn.execute(
            "INSERT INTO wakes(ts, reason, pending, high_medium, row_json) VALUES(?,?,?,?,?)",
            (float(row.get("ts") or 0), row.get("reason"),
             _int_or_none(row.get("pending")), _int_or_none(row.get("highMedium")),
             _dumps(row)))
    return 1


def write_taskruns(conn, st):
    """Sync `taskruns.json`: one row per task, plus the two top-level keys."""
    st = st or {}
    tasks = st.get("tasks") or {}
    have = {r["task_id"]: r["state_json"] for r in conn.execute(
        "SELECT task_id, state_json FROM task_review")}
    changed = 0
    with tx(conn):
        for tid, rec in tasks.items():
            payload = _dumps(rec)
            if have.get(tid) == payload:
                continue
            changed += 1
            rec = rec or {}
            conn.execute(
                "INSERT INTO task_review(task_id, last_reviewed_ms, seen_runs, state_json) "
                "VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "last_reviewed_ms=excluded.last_reviewed_ms, seen_runs=excluded.seen_runs, "
                "state_json=excluded.state_json",
                (tid, int(rec.get("lastReviewMs") or 0),
                 int(rec.get("runsSinceReview") or 0), payload))
        for tid in have:
            if tid not in tasks:
                conn.execute("DELETE FROM task_review WHERE task_id=?", (tid,))
        for key in ("lastPollMs", "lastError"):
            if key in st:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                             ("taskruns." + key, _dumps(st[key])))
            else:
                conn.execute("DELETE FROM meta WHERE key=?", ("taskruns." + key,))
    return {"changed": changed, "tasks": len(tasks)}


# --------------------------------------------------------------------- stats


def store_stat(conn=None, root=None):
    own = conn is None
    conn = conn or connect()
    try:
        out = {
            "db": os.fspath(conn.execute("PRAGMA database_list").fetchone()[2]),
            "schema_version": (conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                               .fetchone() or ["?"])[0],
            "tables": {},
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "sizes": {},
        }
        for name in ("sessions", "events", "event_keys", "approvals", "approval_ledger",
                     "dispatch", "sent", "wakes", "task_review", "health", "meta"):
            out["tables"][name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        out["events_pending"] = conn.execute(
            "SELECT COUNT(*) FROM events WHERE state='pending'").fetchone()[0]
        for label, path in (("db", out["db"]), ("wal", out["db"] + "-wal"),
                            ("shm", out["db"] + "-shm")):
            out["sizes"][label] = os.path.getsize(path) if os.path.exists(path) else 0
        root = root or WATCH
        for name, path in paths(root).items():
            out["sizes"]["legacy_" + name] = os.path.getsize(path) if os.path.exists(path) else 0
        return out
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- selftest


def _fixture(root):
    """A small legacy tree carrying every shape the live files have.

    Deliberately includes the awkward ones: 27 dedupe keys (so the 25-key window has
    to trim), 3 handled rows sharing an id (the real inbox has that), a folded row, an
    external event with `thread_key`, an unknown top-level state key (forward
    compatibility), a closed dispatch plus its tombstone, a task with no
    `lastReviewMs`, and non-ASCII text in every file.
    """
    p = paths(root)
    emitted = [f"awaiting_user|{1792000000000 + i}|{i}" for i in range(27)]
    state = {
        "sessions": {
            "s1": {"lastUpdMs": 1792111449609, "emitted": emitted, "failedRuns": 0,
                   "sig": "1792111449609|0", "lastOrdinal": 657, "lastKind": "assistant"},
            "s2": {"sig": "1|1", "streamingSinceMs": 1792111000000, "pendingNew": True,
                   "rewound": ["stalled"]},
            "s3": {},
        },
        "deferred": ["s2", "s3"],
        "lastScanTs": 1792111449.5,
        "peakSessions": 1702,
        "peakSessionsTs": 1792100000.25,
        "somethingNew": {"nested": [1, 2, {"中文": "值"}]},
    }
    _write_json(p["state"], state)
    _write_json(p["health"], {"ts": 1792111449.5, "ok": True, "baseline": False,
                              "sessionsTotal": 1702, "active": 83, "events": 0,
                              "degraded": "会话列表收缩 1900 -> 1702"})
    pending = [
        {"id": "ev_1", "kind": "awaiting_user", "priority": "medium", "sessionId": "s1",
         "sessionName": "甲会话", "detectedAtMs": 1792111000000,
         "detail": {"questionLike": True, "lastAssistantText": "要我做吗？"}},
        {"id": "ev_2", "kind": "external", "priority": "high", "sessionId": "",
         "detectedAtMs": 1792111100000,
         "detail": {"action_class": "observe", "thread_key": "owner/repo#12",
                    "payload_ref": "state/x.md"}},
        {"id": "ev_3", "kind": "task_review", "priority": "medium", "sessionId": "",
         "detectedAtMs": 1792111200000,
         "detail": {"task_id": "tsk_1", "runsSinceReview": 6}},
    ]
    _write_jsonl(p["pending"], pending)
    handled = []
    for i in range(5):
        handled.append({"id": f"ev_h{i}", "kind": "new_session", "priority": "low",
                        "sessionId": f"s{i}", "detectedAtMs": 1792100000000 + i,
                        "handledAtMs": 1792101000000 + i, "detail": {}})
    for i in range(3):
        handled.append({"id": "evt_task_review_dup", "kind": "task_review",
                        "priority": "medium", "sessionId": "",
                        "detectedAtMs": 1792102000000 + i,
                        "handledAtMs": 1792103000000 + i, "detail": {"task_id": "tsk_1"}})
    handled.append({"id": "ev_folded", "kind": "awaiting_user", "priority": "low",
                    "sessionId": "s1", "detectedAtMs": 1792104000000,
                    "handledAtMs": 1792105000000, "foldedInto": "ev_1",
                    "foldedReason": "superseded by a later event of the same kind "
                                    "for the same session", "detail": {}})
    _write_jsonl(p["handled"], handled)
    _write_json(p["approval_cache"],
                {"s1": {"checkedAt": 1792111449.1, "updatedAtMs": 1792111449609},
                 "s2": {"checkedAt": 1792111449.2, "updatedAtMs": 1792111000000}},
                indent=None, sort_keys=True)
    _write_jsonl(p["approval_ledger"], [
        {"ts": 1792110000.5, "at": "2026-10-15 21:40:00", "sessionId": "s1",
         "sessionName": "甲", "requestId": "req_1", "tool": "bash",
         "detail": "{\"cmd\": \"ls\"}", "action": "approve", "rc": 0,
         "result": "ok"},
        {"ts": 1792110100.5, "at": "2026-10-15 21:41:40", "sessionId": "s2",
         "requestId": "req_2", "tool": "write", "action": "escalated",
         "reason": "deny-pattern: --force"},
    ])
    _write_jsonl(p["dispatch"], [
        {"ts": "2026-10-25T20:43:35+08:00", "session": "ss1", "title": "验证",
         "accept": "贴出复现原文", "repo": "/tmp/repo", "note": "备注", "closed": False},
        {"ts": "2026-10-25T21:42:46+08:00", "session": "ss2", "title": "落地",
         "accept": "提交号", "repo": "", "note": "", "closed": False,
         "close_when": "pr-merged:o/r#7"},
        {"ts": "2026-10-25T22:00:00+08:00", "session": "ss1", "title": "验证",
         "accept": "贴出复现原文", "repo": "/tmp/repo", "note": "备注", "closed": True,
         "closed_why": "done", "closed_at": "2026-10-26T09:00:00+08:00"},
    ])
    _write_jsonl(p["sent"], [
        {"ts": 1792087489.719229, "sessionId": "s1", "note": "授权查外键错误"},
        {"ts": 1792087668.984959, "sessionId": "s2", "note": "不要自己删 APK"},
    ])
    _write_jsonl(p["wakes"], [
        {"ts": 1792038059.5198138, "reason": "events", "pending": 1, "highMedium": 1},
        {"ts": 1792039215.279207, "reason": "heartbeat", "pending": 0, "highMedium": 0},
    ])
    _write_json(p["taskruns"], {
        "tasks": {
            "tsk_1": {"name": "AI 日报简报", "seenRunIds": ["trn_a", "trn_b"],
                      "runsSinceReview": 3, "lastReviewMs": 1792100000000,
                      "firstSeenMs": 1792000000000, "failureStreak": 1,
                      "reviewPending": False, "reviewEmittedRuns": 0},
            "tsk_2": {"name": "PR sweep", "seenRunIds": [], "runsSinceReview": 0},
        },
        "lastPollMs": 1792111449000,
    })
    return root


def selftest():
    """Fixture round-trip: import → export → every file semantically unchanged."""
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        out = os.path.join(tmp, "out")
        db = os.path.join(tmp, "fixture.db")
        _fixture(src)
        conn = connect(db)
        counts = import_from_files(conn, src)
        check("import covers every file", all(counts.get(k) for k in (
            "sessions", "pending", "handled", "approvals", "dispatch", "sent", "wakes",
            "tasks")), str(counts))
        check("duplicate handled ids survive the import", counts["handled"] == 9,
              f"handled={counts['handled']}")
        export_to_files(conn, out)
        problems = compare_trees(src, out)
        check("import → export is semantically identical", not problems, ", ".join(problems))

        # Round-trip twice: a second export must equal the first (no drift).
        out2 = os.path.join(tmp, "out2")
        conn2 = connect(os.path.join(tmp, "fixture2.db"))
        import_from_files(conn2, out)
        export_to_files(conn2, out2)
        check("a second import→export does not drift", not compare_trees(src, out2),
              ", ".join(compare_trees(src, out2)))

        # The 25-key window is applied on import, and the export still reproduces the
        # stored `emitted` list (which the file logic trims to 25 itself).
        kept = conn.execute("SELECT COUNT(*) FROM event_keys WHERE session_id='s1'").fetchone()[0]
        check("dedupe window trimmed to 25", kept == 25, f"keys={kept}")
        check("a record keeps keys the store does not interpret",
              len(json.loads(conn.execute(
                  "SELECT state_json FROM sessions WHERE id='s1'").fetchone()[0])["emitted"]) == 27)

        # Every write is a mirror: a failure must never raise into the caller.
        class Boom:
            def execute(self, *a):
                raise RuntimeError("no such table")

        try:
            write_state(Boom(), {"sessions": {}})
        except RuntimeError:
            check("write_state still raises on a broken connection (caller decides)", True)
        else:
            check("write_state still raises on a broken connection (caller decides)", False)
        global DB_PATH, MODE_PATH, FAIL_LOG
        saved_paths = (DB_PATH, MODE_PATH, FAIL_LOG)
        DB_PATH = os.path.join(tmp, "live.db")
        MODE_PATH = os.path.join(tmp, "store-mode.json")
        FAIL_LOG = os.path.join(tmp, "logs", "store.log")
        _mode_cache.update({"path": None, "mtime": None, "value": None})
        close()
        try:
            check("mirror() swallows a failure and reports it",
                  mirror("scan", lambda _c: 1 / 0) is None)
            logged = (open(FAIL_LOG, encoding="utf-8").read()
                      if os.path.exists(FAIL_LOG) else "")
            check("the mirror failure was logged, not swallowed silently",
                  "[scan] mirror failed" in logged, logged.strip()[:80])
            set_mode("inbox", False)
            check("one module can be switched off", not enabled("inbox") and enabled("scan"))
            set_mode("inbox", True)
            check("and switched back on", enabled("inbox"))
            check("a temp path is not mistaken for the live one",
                  is_live(os.path.join(tmp, "pending.jsonl"), "pending") is False
                  and is_live(paths()["pending"], "pending") is True)
        finally:
            close()
            DB_PATH, MODE_PATH, FAIL_LOG = saved_paths
            _mode_cache.update({"path": None, "mtime": None, "value": None})

        # A second, independent writer must not see "database is locked".
        conn3 = connect(db)
        with tx(conn3):
            conn3.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('probe','1')")
        check("a second connection can write the same database", True)
        conn.close()
        conn2.close()
        conn3.close()

    failed = [c for c in checks if not c[1]]
    print(f"-- store selftest: {len(checks) - len(failed)}/{len(checks)} ok")
    return 0 if not failed else 1


# ---------------------------------------------------------------------- CLI


def main(argv):
    ap = argparse.ArgumentParser(description="SQLite store for the supervisor's state")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--root", default=None, help="legacy tree (default: this checkout)")
    ap.add_argument("--from-json", action="store_true", help="import today's files")
    ap.add_argument("--export-json", metavar="DIR", default=None,
                    help="write the store back out in today's file formats")
    ap.add_argument("--check-consistency", action="store_true",
                    help="compare the live database against the live files")
    ap.add_argument("--no-lost", action="store_true",
                    help="with --check-consistency: only a row the files have and the "
                         "store does not is a failure (the other direction is the "
                         "write-store-first window, and is reported only)")
    ap.add_argument("--disable", default=None, metavar="MODULES",
                    help="comma-separated module switch to turn off")
    ap.add_argument("--enable", default=None, metavar="MODULES")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("cmd", nargs="?", default=None, choices=[None, "stat", "modules"])
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.disable or args.enable:
        names = (args.disable or args.enable or "").split(",")
        for name in [n.strip() for n in names if n.strip()]:
            if name not in MODULES:
                print(f"unknown module {name!r}; known: {', '.join(MODULES)}", file=sys.stderr)
                return 2
        for name in [n.strip() for n in (args.disable or "").split(",") if n.strip()]:
            set_mode(name, False)
        for name in [n.strip() for n in (args.enable or "").split(",") if n.strip()]:
            set_mode(name, True)
        print(json.dumps(_mode(), ensure_ascii=False))
        return 0

    if args.cmd == "modules":
        print(json.dumps(_mode(), ensure_ascii=False, indent=1))
        return 0

    if args.from_json:
        root = args.root or WATCH
        existed = os.path.exists(args.db)
        conn = connect(args.db)
        try:
            counts = import_from_files(conn, root, log=lambda m: print(m))
        finally:
            conn.close()
        print(f"imported {json.dumps(counts, ensure_ascii=False)} "
              f"from {root} into {args.db}"
              + ("" if existed else " (new database)"))
        return 0

    if args.export_json:
        conn = connect(args.db)
        try:
            counts = export_to_files(conn, args.export_json)
        finally:
            conn.close()
        print(f"exported {json.dumps(counts, ensure_ascii=False)} to {args.export_json}")
        return 0

    if args.check_consistency:
        root = args.root or WATCH
        db = args.db
        if not os.path.exists(db):
            print(f"no database at {db}: nothing to compare (this is not the deployment "
                  f"checkout, or step 1 has not run yet)")
            return 0
        conn = connect(db)
        tmp = tempfile.mkdtemp(prefix="store-consistency-")
        try:
            export_to_files(conn, tmp)
        finally:
            conn.close()
        try:
            diff = diff_trees(root, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        lost = {k: v for k, v in diff.items() if v.get("missing")}
        ahead = {k: v for k, v in diff.items() if v.get("extra")}
        if lost or (ahead and not args.no_lost):
            bits = []
            if lost:
                bits.append("missing from db: " + ", ".join(
                    f"{k}={v['missing']}" + (f" {v['changed']}" if v.get('changed') else "")
                    for k, v in lost.items()))
            if ahead:
                bits.append("only in db: " + ", ".join(
                    f"{k}={v['extra']}" for k, v in ahead.items()))
            print("INCONSISTENT: " + "; ".join(bits))
            return 1
        if ahead:
            print("consistent for lost writes; store ahead of the files in "
                  + ", ".join(f"{k}={v['extra']}" for k, v in ahead.items())
                  + " (the write-store-first window, normal while dual-writing)")
            return 0
        print(f"consistent: {db} agrees with {root} on all {len(LEGACY)} legacy files")
        return 0

    if args.cmd == "stat":
        print(json.dumps(store_stat(root=args.root), ensure_ascii=False, indent=1))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
