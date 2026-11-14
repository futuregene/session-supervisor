#!/usr/bin/env python3
"""The assertions the state migration has to keep, in one runnable place.

`bin/store.py --selftest` proves the database can be turned back into today's files.
This file proves the four properties the migration would otherwise be trusted on:

  1. **dedupe equivalence** — the store's `event_keys` window admits exactly the
     events the file-based `emitted` list admits, on the same input stream. This is
     the one that silently changes behaviour if it drifts: a wider window suppresses
     real news, a narrower one re-fires old news.
  2. **crash safety** — `kill -9` in the middle of a write transaction, 20 times, at
     random points. After each kill: the event rows form a prefix with no gaps, a
     handled event never comes back as pending, re-acking is a no-op, and the next
     write still lands. A store that "looks like it is writing" but did not commit is
     exactly the failure the scan had for 18.4 h (docs/gotcha-scan.md B15).
  3. **concurrency** — three writers at once (a scan, a wake turn, task polling, in
     effect) produce **zero** `database is locked`, and `busy_timeout` is shown to
     actually wait rather than fail fast.
  4. **write amplification** — one scan's mirror is O(rows changed), not O(state).
     Measured against the live files when this runs on the deployment host, and
     against the fixture everywhere else.

  python3 bin/selftest_store.py           # all of it
  python3 bin/selftest_store.py --quick   # skip the kill -9 loop (8 instead of 20)
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time

BIN = os.path.dirname(os.path.abspath(__file__))
WATCH = os.path.dirname(BIN)
sys.path.insert(0, BIN)

import store  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


# ------------------------------------------------------------------ 1. dedupe


def dedupe_equivalence(rounds=600):
    """Old `emitted` list vs `event_keys`: same admissions, same final window."""
    import scan  # noqa: E402  (imported here: it resolves paths at import time)

    rnd = random.Random(20261114)
    kinds = ["awaiting_user", "new_session", "failed_run", "run_error", "stalled"]
    sessions = [f"s{i}" for i in range(3)]
    db = os.path.join(tempfile.mkdtemp(prefix="dedupe-"), "d.db")
    conn = store.connect(db)
    old = {sid: {"emitted": []} for sid in sessions}
    admitted_old, admitted_new = [], []
    for i in range(rounds):
        sid = rnd.choice(sessions)
        kind = rnd.choice(kinds)
        # A small key space on purpose: replay, rewind and "same run appended again"
        # are the cases where the two implementations can disagree.
        extra = str(rnd.randrange(6))
        stable = kind in ("run_error",)
        stamp = rnd.choice([1792000000000, 1792000001000, 1792000002000])
        key = scan.event_key(kind, extra, stamp, stable)
        assert key == store.event_key(kind, extra, stamp, stable), "key shape drifted"

        seen = old[sid].setdefault("emitted", [])
        is_new_old = key not in seen
        if is_new_old:
            seen.append(key)
            del seen[:-store.EVENT_KEY_WINDOW]
        is_new_new = store.dedupe_admit(conn, sid, key, kind)
        admitted_old.append((sid, i, is_new_old))
        admitted_new.append((sid, i, is_new_new))
        if is_new_old != is_new_new:
            check("dedupe equivalence", False,
                  f"diverged at step {i}: old={is_new_old} new={is_new_new} key={key}")
            conn.close()
            return
    if admitted_old != admitted_new:
        check("dedupe equivalence", False, f"{sum(a[2] for a in admitted_old)} vs "
              f"{sum(a[2] for a in admitted_new)} admissions")
        conn.close()
        return
    windows_ok = True
    for sid in sessions:
        stored = sorted(r["key"] for r in conn.execute(
            "SELECT key FROM event_keys WHERE session_id=?", (sid,)))
        if stored != sorted(old[sid]["emitted"]):
            windows_ok = False
    conn.close()
    check("dedupe equivalence", windows_ok,
          f"{rounds} steps, {sum(a[2] for a in admitted_old)} admissions, "
          f"windows match on {len(sessions)} sessions")

    # The same key twice, from two writers at the same moment: one row.
    db2 = os.path.join(tempfile.mkdtemp(prefix="dedupe2-"), "d.db")
    c1, c2 = store.connect(db2), store.connect(db2)
    store.dedupe_admit(c1, "s9", "k|1")
    store.dedupe_admit(c2, "s9", "k|1")
    rows = c1.execute("SELECT COUNT(*) FROM event_keys WHERE session_id='s9' AND key='k|1'").fetchone()[0]
    c1.close()
    c2.close()
    check("the same event key is admitted once", rows == 1, f"rows={rows}")


# ------------------------------------------------------------------- 2. crash


def _count(db):
    """How many event rows the child has committed so far (0 before its first)."""
    if not os.path.exists(db):
        return 0
    try:
        # create=False: the child holds the write lock, and this probe must not
        # queue behind it for the schema's DDL.
        conn = store.connect(db, create=False)
        try:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0

CRASH_CHILD = r"""
import sys, time
sys.path.insert(0, sys.argv[2])
import store
conn = store.connect(sys.argv[1])
i = 0
while True:
    i += 1
    with store.tx(conn):
        conn.execute("INSERT INTO events(id, kind, priority, session_id, emitted_ms, state,"
                     " detail_json, row_json) VALUES(?,?,?,?,?,?,?,?)",
                     ("ev_k%d" % i, "crash", "low", "s1", i, "pending", "{}",
                      '{"id": "ev_k%d"}' % i))
        if i > 1:
            conn.execute("UPDATE events SET state='handled', handled_ms=?, row_json=?"
                         " WHERE id=? AND state='pending'",
                         (i * 10, '{"id": "ev_k%d", "handled": true}' % (i - 1),
                          "ev_k%d" % (i - 1)))
        # One wake row per step, in the same transaction: a crash must not be able to
        # leave this wake recorded twice, and a retry must not wake again for it.
        conn.execute("INSERT INTO wakes(ts, reason, pending, high_medium, row_json)"
                     " VALUES(?,?,?,?,?)", (i, "step-%d" % i, 1, 0, '{"step": %d}' % i))
    time.sleep(0.002)
"""


def crash_safety(rounds=20):
    """`kill -9` mid-transaction, `rounds` times; each invariant counted on its own.

    The question is not "does it restart" but "which of the promises still hold after
    a SIGKILL at an arbitrary point inside a write transaction":

      1. `quick_check` — the file is not corrupt;
      2. no gap — every committed row is there and the ids are contiguous (a lost
         commit shows up as a gap: this is the "已写入不丢" half);
      3. handled is a prefix — one transaction per step, so acked events are 1..n-1;
      4. pending is exactly the last step — nothing was half-applied;
      5. no row is both handled and pending — an ack cannot half-happen;
      6. no duplicate wake — one wake row per step, exactly once, even after the kill;
      7. an acked event never returns to pending;
      8. acking it twice adds no row;
      9. the next write still lands (the kill left no lock behind).
    """
    tmp = tempfile.mkdtemp(prefix="crash-")
    child = os.path.join(tmp, "child.py")
    with open(child, "w", encoding="utf-8") as fh:
        fh.write(CRASH_CHILD)
    rnd = random.Random(424242)
    names = ["quick_check", "no_gap", "handled_prefix", "pending_is_last",
             "never_both", "no_dup_wake", "ack_no_revive", "ack_idempotent",
             "next_write_lands"]
    tally = {name: 0 for name in names}
    empty = 0
    bad = []
    rows_total = 0
    for it in range(rounds):
        db = os.path.join(tmp, "c%d.db" % it)
        proc = subprocess.Popen([sys.executable, child, db, BIN],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 5.0
        while time.time() < deadline and not _count(db):
            time.sleep(0.01)
        time.sleep(rnd.uniform(0.01, 0.12))
        proc.kill()          # SIGKILL: no cleanup, no rollback hook
        proc.wait()
        if proc.returncode not in (-9, 137):
            bad.append(f"round {it}: child died on its own "
                       f"({(proc.stderr.read() or b'').decode('utf-8', 'replace')[:120]})")
            continue
        conn = store.connect(db)
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                tally["quick_check"] += 1
            rows = [(r["id"], r["state"]) for r in conn.execute(
                "SELECT id, state FROM events ORDER BY CAST(SUBSTR(id, 5) AS INTEGER)")]
            ids = [int(r[0][4:]) for r in rows]
            handled = [int(r[0][4:]) for r in rows if r[1] == "handled"]
            pending = [int(r[0][4:]) for r in rows if r[1] == "pending"]
            wakes = [r["reason"] for r in conn.execute("SELECT reason FROM wakes ORDER BY seq")]
            if not ids:
                empty += 1
                continue
            rows_total += len(ids)
            if ids == list(range(1, len(ids) + 1)):
                tally["no_gap"] += 1
            else:
                bad.append(f"round {it}: id set has a gap or duplicate ({ids[:6]}…)")
            if handled == list(range(1, len(handled) + 1)):
                tally["handled_prefix"] += 1
            else:
                bad.append(f"round {it}: handled is not a prefix ({handled[-3:]})")
            if len(pending) == 1 and pending[0] == len(ids):
                tally["pending_is_last"] += 1
            else:
                bad.append(f"round {it}: pending={pending[:3]} rows={len(ids)}")
            if not (set(handled) & set(pending)):
                tally["never_both"] += 1
            else:
                bad.append(f"round {it}: an event is both handled and pending")
            want = ["step-%d" % i for i in range(1, len(ids) + 1)]
            if wakes == want:
                tally["no_dup_wake"] += 1
            else:
                bad.append(f"round {it}: wakes {wakes[-3:]} vs expected {want[-3:]}")

            victim = f"ev_k{pending[0]}"
            conn2 = store.connect(db)
            try:
                store.mark_handled(conn2, [{"id": victim, "handledAtMs": 1792000000000}])
                store.mark_handled(conn2, [{"id": victim, "handledAtMs": 1792000000000}])
                state = [r[0] for r in conn2.execute(
                    "SELECT state FROM events WHERE id=?", (victim,))]
                if state == ["handled"]:
                    tally["ack_no_revive"] += 1
                else:
                    bad.append(f"round {it}: acked event state={state}")
                total = conn2.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                if total == len(ids):
                    tally["ack_idempotent"] += 1
                else:
                    bad.append(f"round {it}: double ack added a row ({total} vs {len(ids)})")
                store.append_wake(conn2, {"ts": time.time(), "reason": "after-kill",
                                          "pending": 0, "highMedium": 0})
                landed = conn2.execute(
                    "SELECT COUNT(*) FROM wakes WHERE reason='after-kill'").fetchone()[0]
                if landed == 1:
                    tally["next_write_lands"] += 1
            finally:
                conn2.close()
        finally:
            conn.close()

    avg = rows_total // max(rounds - empty, 1)
    detail = (f"{rounds} kills, rows/round avg {avg}, "
              + ", ".join(f"{n} {tally[n]}/{rounds}" for n in names)
              + (f", {empty} died before its first commit" if empty else ""))
    ok = (not bad and empty < max(2, rounds // 5)
          and all(tally[n] >= rounds - empty for n in names))
    check("crash safety (kill -9 mid-transaction)", ok,
          detail if not bad else "; ".join(bad[:4]))


# ------------------------------------------------------------- 3. concurrency

CONC_WRITER = r"""
import sys, time, sqlite3
sys.path.insert(0, sys.argv[1])
import store
db, tag, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
conn = store.connect(db)
locked = 0
start = time.time()
for i in range(n):
    try:
        with store.tx(conn):
            conn.execute("INSERT INTO wakes(ts, reason, pending, high_medium, row_json)"
                         " VALUES(?,?,?,?,?)", (time.time(), tag, i, 0, '{"tag": "%s"}' % tag))
            conn.execute("INSERT INTO sessions(id, last_upd_ms, state_json) VALUES(?,?,?)"
                         " ON CONFLICT(id) DO UPDATE SET last_upd_ms=excluded.last_upd_ms",
                         (tag + "-s", i, '{"i": %d}' % i))
            conn.execute("INSERT INTO events(id, kind, priority, session_id, emitted_ms,"
                         " state, detail_json, row_json) VALUES(?,?,?,?,?,?,?,?)",
                         ("%s-e%d" % (tag, i), "concurrent", "low", tag, i, "pending", "{}",
                          '{"i": %d}' % i))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            locked += 1
        else:
            raise
print("locked=%d elapsed=%.2f" % (locked, time.time() - start))
"""

CONC_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import store
conn = store.connect(sys.argv[2])
with store.tx(conn):
    conn.execute("INSERT INTO wakes(ts, reason, pending, high_medium, row_json)"
                 " VALUES(?,?,?,?,?)", (time.time(), "holder", 0, 0, '{}'))
    time.sleep(float(sys.argv[3]))
"""


def concurrency(rounds=120):
    tmp = tempfile.mkdtemp(prefix="conc-")
    db = os.path.join(tmp, "c.db")
    store.connect(db).close()
    child = os.path.join(tmp, "writer.py")
    with open(child, "w", encoding="utf-8") as fh:
        fh.write(CONC_WRITER)
    procs = [subprocess.Popen([sys.executable, child, BIN, db, tag, str(rounds)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for tag in ("scan", "wake", "taskruns")]
    locked = 0
    failures = []
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        text = (out or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            failures.append((err or b"").decode("utf-8", "replace").strip()[:160])
            continue
        for token in text.split():
            if token.startswith("locked="):
                locked += int(token.split("=")[1])
    conn = store.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM wakes WHERE reason IN "
                        "('scan','wake','taskruns')").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    check("three concurrent writers, no 'database is locked'",
          locked == 0 and not failures and rows == rounds * 3 and events == rounds * 3,
          f"locked={locked} writes={rows} events={events} rounds={rounds}"
          + (f" failures={failures[:2]}" if failures else ""))

    # busy_timeout really waits: hold a write transaction, then try to write.
    holder = os.path.join(tmp, "holder.py")
    with open(holder, "w", encoding="utf-8") as fh:
        fh.write(CONC_HOLDER)
    proc = subprocess.Popen([sys.executable, holder, BIN, db, "1.2"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(0.30)  # let the holder take the write lock
    conn = store.connect(db)
    t0 = time.time()
    error = ""
    try:
        with store.tx(conn):
            conn.execute("INSERT INTO wakes(ts, reason, pending, high_medium, row_json)"
                         " VALUES(?,?,?,?,?)", (time.time(), "waiter", 0, 0, '{}'))
    except sqlite3.Error as exc:
        error = f"{type(exc).__name__}: {exc}"
    waited = time.time() - t0
    conn.close()
    proc.wait(timeout=30)
    check("busy_timeout waits for the other writer instead of failing",
          not error and waited >= 0.6,
          f"waited {waited:.2f}s" + (f", {error}" if error else ", no error"))


# ------------------------------------- 3b. the three named processes, at the same time


FAKE_FUTURE = r'''#!/usr/bin/env python3
"""A canned `future` CLI: the shapes scan / taskruns / decide ask for, nothing live."""
import json, sys

args = sys.argv[1:]
if args[:2] == ["session", "list"]:
    print(json.dumps({"sessions": [
        {"id": "s%03d" % i, "sessionName": "会话 %d" % i, "cwd": "/tmp/x",
         "model": "m", "queryCount": 3, "isStreaming": False,
         "updatedAtMs": 1792111449000 - i * 1000} for i in range(12)]}))
elif args[:2] == ["session", "transcript"]:
    print(json.dumps({"scannedEntries": 2, "ledger": [
        {"status": "completed", "runId": "r1"}], "entries": [
        {"kind": "user", "ordinal": 1, "createdAtMs": 1792111000000, "blocks": []},
        {"kind": "assistant", "ordinal": 2, "createdAtMs": 1792111100000,
         "blocks": [{"kind": "text", "text": "已停下，等你决定"}]}]}))
elif args[:2] == ["task", "list"]:
    print(json.dumps([{"id": "tsk_1", "name": "日报", "trigger": "1h"}]))
elif args[:2] == ["task", "runs"]:
    print(json.dumps([{"id": "trn_%d" % i, "status": "completed",
                       "startedAt": 1792111100000 + i, "promptVersion": 1}
                      for i in range(3)]))
elif args[:2] == ["session", "approvals"]:
    print(json.dumps({"pendingApprovals": []}))
else:
    print("{}")
'''


def _sandbox(root, source):
    """A throwaway WATCH: the real modules, a fake CLI, one stub wake.sh.

    Uses the live files when they are there (so the contention runs against the real
    sessions), the fixture otherwise. `future` is faked so a test never reads the
    user's other sessions; `wake.sh` is stubbed so a test never spawns an agent turn —
    `decide.py` still writes its wake row, which is the write being contended.
    """
    import shutil
    shutil.copytree(os.path.join(WATCH, "bin"), os.path.join(root, "bin"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("state", "inbox", "logs"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    for name, src in store.paths(source).items():
        if os.path.exists(src):
            dst = os.path.join(root, store.LEGACY[name])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    cfg_path = os.path.join(root, "bin", "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    cfg.update({"future_bin": os.path.join(root, "fake-future.py"),
                "task_watch_enabled": True, "service_probe_enabled": False,
                "balance_probe_enabled": False, "max_deep_checks_per_scan": 2,
                "auto_approve": False, "max_events_per_scan": 5,
                "task_poll_seconds": 0, "task_review_every_runs": 999,
                "min_wake_gap_seconds": 0, "max_wakes_per_hour": 999,
                "max_wakes_per_day": 999})
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    fake = os.path.join(root, "fake-future.py")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write(FAKE_FUTURE)
    os.chmod(fake, 0o755)
    with open(os.path.join(root, "state", "monitor_session.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("monitor-0001")
    wake = os.path.join(root, "bin", "wake.sh")
    with open(wake, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/bash\nexit 0\n")
    os.chmod(wake, 0o755)
    return root


def live_concurrency(rounds=4):
    """Three *real* modules writing the same database at the same time.

    `scan.py`, `taskruns.py collect` and a wake turn (`decide.py`) are the three
    writers the deployment actually has. The synthetic writer test below proves the
    locking discipline; this one proves the *modules* use it and that a real cycle
    produces no `database is locked` in any output or in `logs/store.log`.
    """
    source = WATCH if os.path.exists(os.path.join(WATCH, "state", "state.json")) else None
    tmp = tempfile.mkdtemp(prefix="liveconc-")
    if source is None:
        store._fixture(os.path.join(tmp, "fx"))
        source = os.path.join(tmp, "fx")
    root = _sandbox(os.path.join(tmp, "watch"), source)
    db = os.path.join(root, "state", "supervisor.db")
    pending_file = os.path.join(root, "inbox", "pending.jsonl")
    locked, failures, woke = 0, [], 0
    for i in range(rounds):
        # A fresh high-priority event each round: `decide.py` only wakes for news, and
        # the wake row is the write this test is contending for.
        with open(pending_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "ev_conc_%d" % i, "kind": "external",
                                 "priority": "high", "sessionId": "s000",
                                 "sessionName": "并发 %d" % i,
                                 "detectedAtMs": 1792111449000 + i,
                                 "detail": {"action_class": "observe"}}) + "\n")
        procs = [
            ("scan", [sys.executable, os.path.join(root, "bin", "scan.py")]),
            ("taskruns", [sys.executable, os.path.join(root, "bin", "taskruns.py"),
                          "collect", "--force"]),
            ("decide", [sys.executable, os.path.join(root, "bin", "decide.py")]),
        ]
        started = [(name, subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE))
                   for name, cmd in procs]
        for name, proc in started:
            out, err = proc.communicate(timeout=180)
            text = ((out or b"") + (err or b"")).decode("utf-8", "replace")
            if proc.returncode != 0:
                failures.append(f"round {i} {name}: exit {proc.returncode} "
                                f"{text.strip()[-160:]}")
            if "locked" in text.lower():
                locked += 1
    fail_log = os.path.join(root, "logs", "store.log")
    if os.path.exists(fail_log):
        text = open(fail_log, encoding="utf-8").read()
        locked += text.lower().count("locked")
    decide_log = os.path.join(root, "logs", "decide.log")
    if os.path.exists(decide_log):
        woke = open(decide_log, encoding="utf-8").read().count("woke supervisor")
    conn = store.connect(db)
    wakes = conn.execute("SELECT COUNT(*) FROM wakes").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    check("three real modules at once (scan + wake turn + taskruns), no 'database is locked'",
          locked == 0 and not failures and wakes >= rounds and events >= rounds,
          f"{rounds} rounds, lock_errors={locked}, wake rows={wakes}, "
          f"event rows={events}, decide woke {woke}/{rounds}"
          + (f" | {failures[:2]}" if failures else ""))


# -------------------------------------------------------- 4. write amplification


def write_amplification():
    """One scan's mirror, measured against the whole-file rewrite it replaces."""
    live_state = os.path.join(WATCH, "state", "state.json")
    live_handled = os.path.join(WATCH, "inbox", "handled.jsonl")
    tmp = tempfile.mkdtemp(prefix="amp-")
    db = os.path.join(tmp, "amp.db")
    if os.path.exists(live_state) and os.path.exists(live_handled):
        source = WATCH
        label = "live files"
    else:
        source = tmp
        store._fixture(os.path.join(tmp, "src"))
        source = os.path.join(tmp, "src")
        label = "fixture (no live state on this host)"
    conn = store.connect(db)
    store.import_from_files(conn, source)
    before_rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    # Close before measuring: the last connection to close checkpoints and removes
    # the WAL, so leaving it open would make the "bytes written" delta meaningless.
    conn.close()
    state = store._load_json(os.path.join(source, "state", "state.json"), {})

    # The old shape: dump the whole state document, which is what `save_json` did
    # every 30 s. Measured as JSON bytes + wall time, no filesystem involved.
    t0 = time.time()
    whole = json.dumps(state, ensure_ascii=False, indent=1)
    old_ms = (time.time() - t0) * 1000
    old_bytes = len(whole.encode("utf-8"))

    # The new shape: three sessions changed, one event emitted, one health row.
    sessions = state.get("sessions") or {}
    # 3 sessions on a real deployment (2738 of them), 1 on the tiny fixture.
    touched = sorted(sessions)[:max(1, min(3, len(sessions) // 10))]
    for sid in touched:
        sessions[sid] = dict(sessions[sid], lastOrdinal="changed")
    event = {"id": "ev_amp_1", "kind": "awaiting_user", "priority": "medium",
             "sessionId": touched[0] if touched else "", "detectedAtMs": 1792111999000,
             "detail": {"questionLike": True}}
    health = {"ts": 1792111999.0, "ok": True, "sessionsTotal": len(sessions),
              "active": 3, "events": 1}

    # What the mirror actually hands to SQLite, independent of page granularity.
    logical = (sum(len(store._dumps(sessions[sid]).encode("utf-8")) for sid in touched)
               + len(store._dumps(event).encode("utf-8"))
               + len(store._dumps(health).encode("utf-8")))

    conn = store.connect(db)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    size_before = _db_bytes(db)
    t0 = time.time()
    wrote = store.write_state(conn, state)
    store.write_events(conn, [event])
    store.write_health(conn, health)
    new_ms = (time.time() - t0) * 1000
    size_after = _db_bytes(db)
    new_bytes = max(0, size_after - size_before)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    after_rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()

    ratio = logical / old_bytes if old_bytes else 1.0
    numbers = (f"{label}: {len(sessions)} sessions, {len(touched)} changed → "
               f"{logical} B of payload in {new_ms:.1f} ms ({new_bytes} B of WAL pages), "
               f"vs {old_bytes} B in {old_ms:.1f} ms for the whole document "
               f"({ratio * 100:.1f}%)")
    # The scale-independent property: the rewrite is exactly the changed rows.
    check("the mirror rewrites only the rows that changed",
          wrote["changed"] == len(touched)
          and wrote["unchanged"] == len(sessions) - len(touched), numbers)
    if label == "live files":
        check("one scan writes O(changes), not O(state)", logical < old_bytes * 0.25, numbers)
    else:
        print("      (fixture: 3 sessions, so the ratio says nothing; the O(changes) "
              "assertion runs against the live files on the deployment host)")
    check("and the event actually landed", after_rows == before_rows + 1,
          f"{before_rows} → {after_rows} events")
    print(f"      daily scale: {new_bytes * 2880 / 1e6:.1f} MB/day now vs "
          f"{old_bytes * 2880 / 1e6:.0f} MB/day before (2880 scans/day)")


def _db_bytes(db):
    total = 0
    for suffix in ("", "-wal"):
        path = db + suffix
        if os.path.exists(path):
            total += os.path.getsize(path)
    return total


# ---------------------------------------------------------------------- main


def mirror_actions_export_back():
    """One call per module's mirror action, then export: it must equal the files.

    This is the per-module half of "the database and the files agree". The import
    path and the mirror path are different code (the importer writes rows directly,
    the mirror actions are what the running modules call), so a round-trip through
    the importer would not catch a mirror action that writes the wrong shape — e.g.
    a forecast that forgets `handled_seq` and reorders the handled log.
    """
    tmp = tempfile.mkdtemp(prefix="mirror-")
    src = os.path.join(tmp, "src")
    out = os.path.join(tmp, "out")
    db = os.path.join(tmp, "m.db")
    store._fixture(src)
    p = store.paths(src)
    conn = store.connect(db)
    wrote = store.write_state(conn, store._load_json(p["state"], {}))
    store.write_health(conn, store._load_json(p["health"], {}))
    store.write_events(conn, store._read_jsonl(p["pending"]))
    store.mark_handled(conn, store._read_jsonl(p["handled"]))
    store.write_approval_cache(conn, store._load_json(p["approval_cache"], {}))
    for row in store._read_jsonl(p["approval_ledger"]):
        store.append_approval_ledger(conn, row)
    for row in store._read_jsonl(p["dispatch"]):
        store.append_dispatch(conn, row)
    for row in store._read_jsonl(p["sent"]):
        store.append_sent(conn, row)
    for row in store._read_jsonl(p["wakes"]):
        store.append_wake(conn, row)
    store.write_taskruns(conn, store._load_json(p["taskruns"], {}))
    store.export_to_files(conn, out)
    conn.close()
    problems = store.compare_trees(src, out)
    check("every module's mirror action exports back to its own file",
          not problems and wrote["changed"] == 3,
          ", ".join(problems) if problems else f"{wrote['changed']} sessions written")


def consistency_is_one_sided():
    """The consistency verdict must not fire on the write-order window.

    Every module writes the database first and the file second, so "the store has a
    row the files do not" is normal for a millisecond and "the files have a row the
    store does not" is a defect. A check that reported both would go red at random
    in production; the first person to see it would weaken it, and then it would
    catch nothing. So this pins both directions, deterministically.
    """
    tmp = tempfile.mkdtemp(prefix="onesided-")
    root = os.path.join(tmp, "root")
    db = os.path.join(tmp, "live.db")
    store._fixture(root)
    conn = store.connect(db)
    store.import_from_files(conn, root)
    conn.close()

    # (a) the store is ahead: a wake row exists in the database only.
    conn = store.connect(db)
    store.append_wake(conn, {"ts": 1792200000.0, "reason": "events", "pending": 1,
                             "highMedium": 1})
    conn.close()
    diff = store.consistency_diff(root, db)
    caught = {k: v for k, v in diff.items() if v.get("missing")}
    check("a store that is ahead of the files is not a defect",
          not caught and diff["wakes"]["extra"] == 1,
          f"missing={caught} extra_wakes={diff['wakes']['extra']}")

    # (b) the files are ahead: a wake row reached the file and not the store.
    with open(os.path.join(root, "state", "wakes.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1792200100.0, "reason": "heartbeat", "pending": 0,
                             "highMedium": 0}) + "\n")
    diff = store.consistency_diff(root, db)
    caught = {k: v for k, v in diff.items() if v.get("missing")}
    check("a write that reached the file and not the store IS a defect",
          caught.get("wakes", {}).get("missing") == 1, f"missing={caught}")


# --------------------------------------------------- 7. retention is lossless in identity


def retention_keeps_identity():
    """Retention may drop the prose; it may not drop the row, or hide it from the check.

    The store's job is the *working set*: `handled.jsonl` is the permanent record and
    is never rewritten. So the policy is allowed to lose text, and the one thing it
    must not lose is the ability to answer "did this event happen, for which session,
    when". Both halves are pinned here, on a tree built for it rather than on the
    shared fixture (whose timestamps are all the same age).
    """
    tmp = tempfile.mkdtemp(prefix="retain-")
    root = os.path.join(tmp, "root")
    db = os.path.join(tmp, "r.db")
    p = store.paths(root)
    now_ms = 1792111449000
    day = 86400 * 1000
    old_ms, young_ms = now_ms - 40 * day, now_ms - day
    rows = [
        {"id": "old1", "kind": "awaiting_user", "priority": "medium", "sessionId": "s1",
         "detectedAtMs": old_ms - 1000, "handledAtMs": old_ms,
         "detail": {"questionLike": True,
                    "lastAssistantText": "旧会话的原文，最多一千五百字" * 60}},
        {"id": "young1", "kind": "awaiting_user", "priority": "low", "sessionId": "s2",
         "detectedAtMs": young_ms - 1000, "handledAtMs": young_ms,
         "detail": {"questionLike": False, "lastAssistantText": "新会话的原文" * 60}},
        {"id": "pending1", "kind": "stalled", "priority": "medium", "sessionId": "s3",
         "detectedAtMs": now_ms - 500, "detail": {"stalledMs": 900000}},
    ]
    store._write_jsonl(p["handled"], rows[:2])
    store._write_jsonl(p["pending"], rows[2:])
    conn = store.connect(db)
    store.import_from_files(conn, root)

    report = store.retain(conn, days=30, max_payload_bytes=None, now_ms=now_ms)
    got = {r["id"]: json.loads(r["row_json"]) for r in conn.execute(
        "SELECT id, row_json FROM events")}
    check("retention slims exactly the rows older than the window",
          report["slimmed_by_age"] == 1 and got["old1"].get("_slim") is True
          and "_slim" not in got["young1"] and "_slim" not in got["pending1"],
          f"age={report['slimmed_by_age']} budget={report['slimmed_by_budget']}")
    check("a slimmed row keeps who/what/when, and says it was trimmed",
          got["old1"].get("id") == "old1" and got["old1"].get("kind") == "awaiting_user"
          and got["old1"].get("sessionId") == "s1"
          and got["old1"].get("handledAtMs") == old_ms
          and got["old1"]["detail"].get("questionLike") is True,
          json.dumps(got["old1"], ensure_ascii=False)[:160])
    check("the prose is gone from the store",
          "lastAssistantText" not in json.dumps(got["old1"], ensure_ascii=False)
          and "lastAssistantText" in json.dumps(got["young1"], ensure_ascii=False))
    check("the check still finds every row after retention",
          store.consistency_diff(root, db)["handled"]["missing"] == 0
          and store.consistency_diff(root, db)["pending"]["missing"] == 0)

    # A row that is actually gone must still be a lost write.
    gone = conn.execute("DELETE FROM events WHERE id='pending1' AND state='pending'").rowcount
    diff = store.consistency_diff(root, db)
    check("a deleted event is still reported missing (retention did not blind the check)",
          gone == 1 and diff["pending"]["missing"] == 1,
          f"missing={diff['pending']['missing']}")

    # The byte budget bounds the full-detail payload even when nothing is old: here
    # the age window is ten years, so only the budget can slim the young row.
    before = conn.execute(
        "SELECT SUM(LENGTH(row_json)) FROM events WHERE row_json NOT LIKE '%\"_slim\"%'"
    ).fetchone()[0]
    report = store.retain(conn, days=3650, max_payload_bytes=200, now_ms=now_ms)
    after = conn.execute(
        "SELECT SUM(LENGTH(row_json)) FROM events WHERE row_json NOT LIKE '%\"_slim\"%'"
    ).fetchone()[0]
    check("a byte budget bounds the full-detail payload",
          report["slimmed_by_budget"] >= 1 and (after or 0) <= 200 and before > 200,
          f"budget-slimmed={report['slimmed_by_budget']} full-detail {before} -> {after}")
    conn.close()


# --------------------------------------------------------- 8. the mirror is wired


#: Every module that writes a mirrored file, and the call that mirrors it. A mirror
#: that was deleted (or renamed) is invisible at runtime — the file still gets written —
#: so the wiring is checked against the source instead of trusted.
WIRING = {
    "scan.py": ['store.write_events', 'store.write_state', 'store.write_health'],
    "inbox.py": ['store.mirror_inbox'],
    "approvals.py": ['store.write_approval_cache', 'store.append_approval_ledger'],
    "dispatch.py": ['store.append_dispatch', 'store.prune_dispatch'],
    "taskruns.py": ['store.write_taskruns'],
    "decide.py": ['store.append_wake'],
    "guard.py": ['store.append_sent'],
    "trigger.py": ['store.write_events'],
    "service_probe.py": ['store.write_events'],
}


def mirror_is_wired():
    missing = []
    for name, tokens in WIRING.items():
        text = open(os.path.join(BIN, name), encoding="utf-8").read()
        for token in tokens:
            if token not in text:
                missing.append(f"{name}: {token}")
    check("every writer of a mirrored file still calls the store", not missing,
          ", ".join(missing) or f"{sum(len(v) for v in WIRING.values())} calls in "
                                f"{len(WIRING)} modules")


# ------------------------------------ 9. what the wake card reads survives retention


def retention_preserves_what_the_card_reads():
    """Retention must not blind the card. `lastAssistantText` is the only casualty.

    `decide.py` builds the card from `TRIAGE_FIELDS` plus `detail.questionLike` (the
    quiet rule), `turn_prompt.py` renders exactly that payload, and `bin/peek.py` reads
    session transcripts from the CLI, never the inbox — so the fields below are the
    whole surface a slimmed row could break. This compares them field by field, which
    would fail the moment a field is added to the card and not to `SLIM_FIELDS`.
    """
    import decide  # noqa: E402

    row = {"id": "ev_x", "kind": "awaiting_user", "priority": "medium",
           "sessionId": "s1", "sessionName": "甲会话（60 字以内）", "cwd": "/tmp/repo",
           "model": "m", "detectedAtMs": 1792111000000, "handledAtMs": 1792112000000,
           "summary": "已停下，等待用户回复（疑似提问）",
           "detail": {"questionLike": True, "lastAssistantOrdinal": 657,
                      "lastAssistantText": "要我做吗？" * 200}}
    slim = store.slim_event(row)
    before = {k: row.get(k) for k in decide.TRIAGE_FIELDS}
    after = {k: slim.get(k) for k in decide.TRIAGE_FIELDS}
    check("every field the wake card reads survives retention",
          before == after, f"{before} vs {after}")
    check("the quiet rule's signal survives retention",
          slim["detail"].get("questionLike") is True
          and slim["detail"].get("lastAssistantOrdinal") == 657)
    lost = set(row["detail"]) - set(slim["detail"])
    check("the only thing retention drops is the prose",
          lost == {"lastAssistantText"}, f"dropped={sorted(lost)}")
    # The point of the policy is that the stored cost stops depending on how much
    # text the event happened to carry — not an arbitrary ratio.
    small = dict(row, detail=dict(row["detail"], lastAssistantText="x" * 100))
    big = dict(row, detail=dict(row["detail"], lastAssistantText="x" * 4000))
    check("a slimmed row's size no longer depends on the prose it carried",
          store._dumps(store.slim_event(small)) == store._dumps(store.slim_event(big)),
          f"{len(store._dumps(store.slim_event(big)))} B either way, "
          f"was {len(store._dumps(big))} B")

    # Pending events are the card's actual input; retention must never touch them.
    tmp = tempfile.mkdtemp(prefix="pending-keep-")
    p = store.paths(tmp)
    pending = [{"id": "ev_p", "kind": "awaiting_user", "priority": "medium",
                "sessionId": "s1", "detectedAtMs": 1,
                "detail": {"questionLike": True, "lastAssistantText": "x" * 500}}]
    store._write_jsonl(p["pending"], pending)
    conn = store.connect(os.path.join(tmp, "k.db"))
    store.import_from_files(conn, tmp)
    before_row = conn.execute("SELECT row_json FROM events WHERE id='ev_p'").fetchone()[0]
    store.retain(conn, days=0, max_payload_bytes=0, now_ms=1792111449000)
    after_row = conn.execute("SELECT row_json FROM events WHERE id='ev_p'").fetchone()[0]
    conn.close()
    check("retention never touches a pending event", before_row == after_row)


# ------------------------------------------- 10. approvals.py end to end, both stores


def approvals_ledger_end_to_end():
    """A real approval decision through `approvals.py` must reach file *and* database.

    `approval_ledger` has 0 rows in production because no decision has ever been made
    (neither `state/approvals.jsonl` nor `reports/approvals.md` exists). "Wired but
    never exercised" is indistinguishable from "not wired", so this drives the module
    itself — `--dry-run`, so nothing is approved — against a sandbox root and asserts
    the row lands in both places.
    """
    tmp = tempfile.mkdtemp(prefix="appr-")
    root = os.path.join(tmp, "watch")
    for name in ("state", "inbox", "logs", "reports"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    import approvals as ap

    saved = {name: getattr(ap, name) for name in ("LEDGER", "REPORT", "CACHE")}
    saved_store = (store.WATCH, store.DB_PATH, store.MODE_PATH)
    ap.LEDGER = os.path.join(root, "state", "approvals.jsonl")
    ap.REPORT = os.path.join(root, "reports", "approvals.md")
    ap.CACHE = os.path.join(root, "state", "approval-cache.json")
    store.close()
    store.WATCH = root
    store.DB_PATH = os.path.join(root, "state", "supervisor.db")
    store.MODE_PATH = os.path.join(root, "state", "store-mode.json")
    store._mode_cache.update({"path": None, "mtime": None, "value": None})
    real_cfg, real_future = ap.cfg, ap.future
    ap.cfg = lambda: {"auto_approve": True, "auto_approve_per_cycle": 5,
                      "auto_approve_per_hour": 20, "max_approval_checks_per_cycle": 10,
                      "approval_rescan_hours": 2, "exclude_sessions": []}

    class Done:
        def __init__(self, out="", code=0):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def fake_future(args, timeout=45):
        if args[:2] == ["session", "list"]:
            return 0, json.dumps({"sessions": [
                {"id": "s1", "sessionName": "甲", "isStreaming": True,
                 "updatedAtMs": 1792111000000}]}), ""
        if args[:2] == ["session", "approvals"]:
            return 0, json.dumps({"pendingApprovals": [
                {"approval_request_id": "req_1", "tool_name": "bash",
                 "action": {"tool": "bash", "command": "ls"}}]}), ""
        return 0, "{}", ""

    ap.future = fake_future
    try:
        rc = ap.main(["auto", "--dry-run"])
        file_rows = store._read_jsonl(ap.LEDGER)
        conn = store.connect(store.DB_PATH)
        try:
            db_rows = [json.loads(r["row_json"]) for r in conn.execute(
                "SELECT row_json FROM approval_ledger ORDER BY seq")]
            cache_rows = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        finally:
            conn.close()
        check("an approval decision lands in the file", rc == 0 and len(file_rows) == 1
              and file_rows[0]["requestId"] == "req_1",
              f"rc={rc} rows={len(file_rows)}")
        check("…and in the database, row for row",
              len(db_rows) == len(file_rows) and db_rows
              and db_rows[0]["requestId"] == file_rows[0]["requestId"]
              and db_rows[0]["action"] == file_rows[0]["action"],
              f"db={len(db_rows)} file={len(file_rows)}")
        check("…and the approval cache too", cache_rows == 1, f"cache rows={cache_rows}")
    finally:
        ap.LEDGER, ap.REPORT, ap.CACHE = saved["LEDGER"], saved["REPORT"], saved["CACHE"]
        ap.cfg, ap.future = real_cfg, real_future
        store.close()
        store.WATCH, store.DB_PATH, store.MODE_PATH = saved_store
        store._mode_cache.update({"path": None, "mtime": None, "value": None})


# ------------------------------------- 11. dedupe against the recorded history


def dedupe_matches_recorded_history():
    """The dedupe state of the live files must be *exactly* what the store holds.

    Two checks, both strict:
      (a) per session, the ordered keys in `event_keys` equal `state.json`'s `emitted`
          window — not "the same set", the same list, in the same order;
      (b) replaying the recorded `handled.jsonl` stream through the old list logic and
          through `dedupe_admit` admits exactly the same events, in the same order.
    The key *function* is compared to `scan.event_key` separately: this is about the
    window/eviction algorithm, which is the part that could silently differ.
    """
    import scan  # noqa: E402

    live = os.path.join(WATCH, "state", "state.json")
    if os.path.exists(live):
        root = WATCH
        label = "live files"
    else:
        root = os.path.join(tempfile.mkdtemp(prefix="dd-"), "fx")
        store._fixture(root)
        label = "fixture (no live state on this host)"

    db = os.path.join(tempfile.mkdtemp(prefix="dd-db-"), "d.db")
    conn = store.connect(db)
    store.import_from_files(conn, root)
    state = store._load_json(os.path.join(root, "state", "state.json"), {})
    mismatched, checked = [], 0
    for sid, rec in (state.get("sessions") or {}).items():
        want = [str(k) for k in (rec.get("emitted") or [])][-store.EVENT_KEY_WINDOW:]
        if not want:
            continue
        checked += 1
        got = [r["key"] for r in conn.execute(
            "SELECT key FROM event_keys WHERE session_id=? ORDER BY seq", (sid,))]
        if got != want:
            mismatched.append(sid)
    check("the dedupe window equals `emitted`, key for key and in order",
          not mismatched and checked > 0,
          f"{label}: {checked} session(s) compared, {len(mismatched)} mismatched")

    # (b) replay the recorded stream
    rows = store._read_jsonl(os.path.join(root, "inbox", "handled.jsonl"))
    old = {}
    admitted_old, admitted_new = [], []
    for i, row in enumerate(rows):
        sid = row.get("sessionId") or ""
        key = scan.event_key(row.get("kind") or "", row.get("detectedAtMs") or "",
                             row.get("detectedAtMs") or "")
        seen = old.setdefault(sid, [])
        is_new_old = key not in seen
        if is_new_old:
            seen.append(key)
            del seen[:-store.EVENT_KEY_WINDOW]
        admitted_old.append(is_new_old)
        admitted_new.append(store.dedupe_admit(conn, sid, key, row.get("kind")))
    conn.close()
    same = admitted_old == admitted_new
    check("replaying the recorded stream admits exactly the same events",
          same and len(rows) > 0,
          f"{label}: {len(rows)} recorded events, {sum(admitted_old)} admissions, "
          f"{0 if same else sum(a != b for a, b in zip(admitted_old, admitted_new))} "
          f"disagreement(s)")


def main(argv):
    quick = "--quick" in argv
    t0 = time.time()
    print("== 1. dedupe equivalence")
    dedupe_equivalence()
    print("== 2. crash safety")
    crash_safety(rounds=8 if quick else 20)
    print("== 3. concurrency (synthetic writers)")
    concurrency()
    print("== 3b. concurrency (the three real modules)")
    live_concurrency()
    print("== 4. write amplification")
    write_amplification()
    print("== 5. per-module mirror round-trip")
    mirror_actions_export_back()
    print("== 6. the consistency verdict is one-sided")
    consistency_is_one_sided()
    print("== 7. retention keeps identity, drops prose")
    retention_keeps_identity()
    print("== 8. every writer of a mirrored file calls the store")
    mirror_is_wired()
    print("== 9. what the wake card reads survives retention")
    retention_preserves_what_the_card_reads()
    print("== 10. approvals.py end to end (file + database)")
    approvals_ledger_end_to_end()
    print("== 11. dedupe against the recorded history")
    dedupe_matches_recorded_history()
    print("== fixture round-trip (bin/store.py --selftest)")
    if store.selftest() != 0:
        FAILURES.append("fixture round-trip")
    took = time.time() - t0
    print(f"-- selftest_store: {'FAILED ' + ', '.join(FAILURES) if FAILURES else 'all ok'} "
          f"({took:.1f}s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
