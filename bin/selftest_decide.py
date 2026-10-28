#!/usr/bin/env python3
"""Self-test for the wake gate's "only new news wakes us" rule (bin/decide.py).

Run: python3 bin/selftest_decide.py
Wired into bin/selfcheck.py so CI runs it with the rest.

Why this exists: the heartbeat was clock-driven, so any low-priority event that
stayed pending (the normal case: a session waiting for the *user*) woke a turn
every five minutes for as long as it sat there — 115 of 128 wakes in the
measured 24 h, ≈¥0.24 each. `has_new_pending` is what stops that; regressing it
silently restores a ~¥28/day bill, so pin the boundary cases here.

Also pins `name_heal_needed`: the desktop app's first-turn auto-title overwrote the
newly rolled supervisor session's name, and the user could not find a new `会话总管`
in the sidebar (2026-10-16 21:32) — so the wake path renames it back, once, inside
that first-run window only.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("decide", os.path.join(ROOT, "bin", "decide.py"))
decide = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decide)

FAILED = []


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


# The desktop app's first-turn auto-title renamed the freshly rolled supervisor
# session away from `会话总管` (measured 2026-10-16 21:32), so no `会话总管` appeared in
# the sidebar. The heal must be bounded to that first run: later on, a different name
# is the user's own choice and must be left alone.
NAME_CASES = [
    ("a generated title after the first run",
     {"sessionName": "磁盘清理与事件处置", "queryCount": 1}, True),
    ("the name we set", {"sessionName": "会话总管", "queryCount": 1}, False),
    ("a rename the user made later", {"sessionName": "用户改的名", "queryCount": 5}, False),
    ("no runs yet (the roll just created it)",
     {"sessionName": "别的", "queryCount": 0}, False),
    ("unreadable status", {}, False),
]

for label, status, want in NAME_CASES:
    check(f"name heal: {label}", decide.name_heal_needed(status, "会话总管"), want)


# ── has_new_pending ─────────────────────────────────────────────────────────
print("has_new_pending")
check("first ever wake (no state file)", decide.has_new_pending(["a"], None), True)
check("first ever wake (empty state)", decide.has_new_pending(["a"], []), True)
check("identical set → no wake", decide.has_new_pending(["a", "b"], ["a", "b"]), False)
check("identical set, different order", decide.has_new_pending(["b", "a"], ["a", "b"]), False)
check("one new id → wake", decide.has_new_pending(["a", "b", "c"], ["a", "b"]), True)
call = "an event we acked disappears → not news"
check(call, decide.has_new_pending(["b"], ["a", "b"]), False)
call = "everything acked → no wake (the idle path handles it)"
check(call, decide.has_new_pending([], ["a", "b"]), False)
check("both empty", decide.has_new_pending([], []), False)
check("nothing pending but stale seen ids", decide.has_new_pending(None, ["a"]), False)
check("None ids dropped", decide.has_new_pending([None, "a"], ["a"]), False)
# The seen file is JSON, so ids come back as str; a numeric-looking id must not
# count as new just because of its Python type.
check("str coercion", decide.has_new_pending(["7"], ["7"]), False)
check("str coercion, new", decide.has_new_pending(["7", "8"], ["7"]), True)
# A wake that is deferred (monitor busy) must not consume the news — decide.py
# only writes the file once a wake really happens; this is the function's side
# of that contract: the same set is still "fresh" until it is recorded.
check("news survives a deferred wake", decide.has_new_pending(["a"], None), True)

# A wake that is deferred (monitor busy) must not consume the news — decide.py
# only writes the file once a wake really happens; this is the function's side
# of that contract: the same set is still "fresh" until it is recorded.

# ── the wake path itself ────────────────────────────────────────────────────
# Regression (2026-10-20): `main()` reached its spawn as
# `subprocess.Popen([BASH, ...wake.sh])` and there is no `BASH` in this module, so
# every automatic wake died with `NameError: name 'BASH' is not defined` — after
# `state/pending_seen.json` had already been written. The supervisor was therefore
# never woken between 19:17 and 21:11 while the ledger claimed two wakes.
# A pure unit test of the gate cannot see that; this one runs `main()` to the spawn.
print("wake path (spawn is real, everything around it is stubbed)")
import json
import subprocess as _subprocess
import tempfile
import types


def run_main(tmp, spawn, pending_rows=(), future_fn=None, cfg_extra=None):
    """Drive decide.main() in `tmp`, with `spawn` standing in for subprocess.Popen.

    `future_fn` replaces the CLI answers (default: "the monitor is idle"), and
    `cfg_extra` is merged into the throwaway config, so a case can say what the
    sessions look like without inventing a second harness.
    """
    calls = []

    def fake_popen(argv, **kw):
        calls.append((argv, kw))
        return spawn(argv, **kw)

    fake_sub = types.SimpleNamespace(
        Popen=fake_popen,
        DEVNULL=_subprocess.DEVNULL,
        CREATE_NEW_PROCESS_GROUP=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        DETACHED_PROCESS=getattr(_subprocess, "DETACHED_PROCESS", 0),
        # High/medium events reach `guard.py check` and `inbox.py ack` through
        # subprocess.run. Answer "allowed, nothing happened" instead of running them:
        # a case about the wake rule must never read or write the live repo's inbox.
        run=lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    saved = {}
    overrides = {
        "PENDING": os.path.join(tmp, "inbox", "pending.jsonl"),
        "HANDLED": os.path.join(tmp, "inbox", "handled.jsonl"),
        "WAKES": os.path.join(tmp, "state", "wakes.jsonl"),
        "PENDING_SEEN": os.path.join(tmp, "state", "pending_seen.json"),
        "PAYLOAD": os.path.join(tmp, "state", "turn_payload.json"),
        "MONITOR_FILE": os.path.join(tmp, "state", "monitor_session.txt"),
        "LOG": os.path.join(tmp, "logs", "decide.log"),
        # No lock file in `tmp`: every case here is "no turn is running". The
        # fresh-lock case has its own test.
        "LOCK_FILE": os.path.join(tmp, "state", "agent.lock"),
        "CFG_PATH": os.path.join(tmp, "config.json"),
        "subprocess": fake_sub,
    }
    for k, v in overrides.items():
        saved[k] = getattr(decide, k)
        setattr(decide, k, v)
    # `future()` asks the CLI whether the monitor is streaming; answer "idle".
    saved_future = decide.future
    decide.future = future_fn or (
        lambda cfg, args, timeout=30: (
            0, json.dumps({"isStreaming": False, "sessionName": "会话总管", "queryCount": 9}), ""))
    saved_bash = decide.bash_exe
    decide.bash_exe = lambda: "bash-that-exists"
    try:
        for sub in ("inbox", "state", "logs"):
            os.makedirs(os.path.join(tmp, sub), exist_ok=True)
        cfg = {"heartbeat_minutes": 0, "min_wake_gap_seconds": 0}
        cfg.update(cfg_extra or {})
        open(os.path.join(tmp, "config.json"), "w").write(json.dumps(cfg))
        open(os.path.join(tmp, "state", "monitor_session.txt"), "w").write("sess-monitor")
        with open(os.path.join(tmp, "inbox", "pending.jsonl"), "w", encoding="utf-8") as fh:
            for row in pending_rows:
                fh.write(json.dumps(row) + "\n")
        rc = decide.main()
    finally:
        for k, v in saved.items():
            setattr(decide, k, v)
        decide.future = saved_future
        decide.bash_exe = saved_bash
    return rc, calls


with tempfile.TemporaryDirectory() as tmp:
    rc, calls = run_main(tmp, lambda argv, **kw: None)
    check("a wake reaches the spawn", len(calls), 1)
    if calls:
        check("it spawns an existing executable", calls[0][0][0], "bash-that-exists")
        check("with wake.sh and the reason after it",
              (os.path.basename(calls[0][0][1]), calls[0][0][2]), ("wake.sh", "heartbeat"))
    check("the wake is recorded", os.path.exists(os.path.join(tmp, "state", "wakes.jsonl")), True)
    check("and the events are marked weighed",
          os.path.exists(os.path.join(tmp, "state", "pending_seen.json")), True)

# The other half of the regression: a spawn that fails must NOT consume the news.
with tempfile.TemporaryDirectory() as tmp:
    def boom(argv, **kw):
        raise OSError("no such file")
    rc, calls = run_main(tmp, boom)
    check("a failed spawn reports failure", rc, 1)
    check("and records nothing (the news survives)",
          os.path.exists(os.path.join(tmp, "state", "pending_seen.json")), False)
    check("no wake is claimed in the ledger",
          os.path.exists(os.path.join(tmp, "state", "wakes.jsonl")), False)

# The overlap regression (2026-10-25): a wake while another turn still holds the
# lock buys nothing — the woken turn's first instruction is `lock.py acquire ||
# exit 0`, so it exits before it can act. Measured: one turn held the lock 63 min
# with two `high` events pending and the gate woke 15 more times, every one of
# them exiting at the acquire step. Fresh lock → no spawn, and the news stays;
# expired lock → the normal path.
with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    import time as _time
    with open(os.path.join(tmp, "state", "agent.lock"), "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "ts": _time.time(), "note": "a long turn"}, fh)
    rc, calls = run_main(tmp, lambda argv, **kw: None, pending_rows=(
        {"id": "ev_1", "kind": "run_error", "priority": "high", "sessionId": "s1",
         "sessionName": "n", "summary": "s"},))
    check("a held lock means no wake at all", len(calls), 0)
    check("a held lock returns cleanly", rc, 0)
    check("and the event is still pending (not weighed)",
          os.path.exists(os.path.join(tmp, "state", "pending_seen.json")), False)

with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    import time as _time
    # Older than the 900 s default TTL: a crashed turn's lock must not wedge the gate.
    with open(os.path.join(tmp, "state", "agent.lock"), "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "ts": _time.time() - 1000, "note": "a crashed turn"}, fh)
    rc, calls = run_main(tmp, lambda argv, **kw: None)
    check("an expired lock still wakes", len(calls), 1)

# ── the user is talking in that session right now ───────────────────────────
# Measured 2026-10-28: a session in ordinary back-and-forth (19 completed runs,
# 5–10 min apart) produced a fresh `awaiting_user` id after every turn, so
# `has_new_pending` saw news each time and the gate woke 5 turns in 75 minutes that
# could only conclude "nothing to answer" and ack. `active_user_suppressed` retires
# those instead — but only for a *non-question* stop in a session with several recent
# runs, because the two failure modes are opposite: waking too often costs a run,
# and staying quiet about a session that really asked him something costs the reply.
import time as _t

print("count_completed_runs (distinct runs inside the window, not entries)")
NOW = 1793200000.0


def at(minutes_ago, now=None):
    return int(((NOW if now is None else now) - minutes_ago * 60) * 1000)


def run_entries(spec, now=None):
    """[(runId, minutes_ago, status)] → entries shaped like `transcript --json`."""
    out = []
    for i, (rid, ago, status) in enumerate(spec):
        for j in range(2):                       # one run leaves several entries
            out.append({"ordinal": i * 2 + j, "kind": "assistant", "runId": rid,
                        "createdAtMs": at(ago, now) + j * 1000, "run": {"status": status}})
    return out


check("two runs inside the window",
      decide.count_completed_runs(run_entries([("r1", 20, "completed"),
                                               ("r2", 3, "completed")]), at(30)), 2)
check("entries are not runs (one turn, two entries)",
      len(run_entries([("r1", 3, "completed")])), 2)
check("one of them older than the window",
      decide.count_completed_runs(run_entries([("r1", 45, "completed"),
                                               ("r2", 3, "completed")]), at(30)), 1)
check("a run still in flight does not count",
      decide.count_completed_runs(run_entries([("r1", 5, "completed"),
                                               ("r2", 1, None)]), at(30)), 1)
check("a failed run does not count",
      decide.count_completed_runs(run_entries([("r1", 5, "failed")]), at(30)), 0)
check("an empty transcript", decide.count_completed_runs([], at(30)), 0)


print("active_user_suppressed (the rule, and its safety valve)")
def transcript_cli(entries_by_sid, streaming=()):
    """Stand-in for `future`: session status, and the two transcript calls."""
    def _call(cfg, args, timeout=30):
        if args[:2] == ["session", "status"]:
            sid = args[2]
            return 0, json.dumps({"isStreaming": sid in streaming,
                                  "sessionName": "会话总管", "queryCount": 9}), ""
        if args[:2] == ["session", "transcript"]:
            evs = entries_by_sid.get(args[args.index("--session") + 1], [])
            if "--runs" in args:
                ledger = [{"runId": e["runId"], "status": (e.get("run") or {}).get("status")}
                          for e in evs]
                return 0, json.dumps({"scannedEntries": len(evs), "ledger": ledger}), ""
            cur = int(args[args.index("--cursor") + 1])
            lim = int(args[args.index("--limit") + 1])
            return 0, json.dumps({"entries": evs[cur:cur + lim]}), ""
        return 127, "", f"unexpected call: {args}"
    return _call


def au(eid, sid="s1", question=False, prio="low"):
    """An `awaiting_user` event as scan.py emits it (questionLike is a real bool).

    `question=None` omits the field entirely — the "unknown" case, which must never
    be read as "not a question".
    """
    detail = {"lastAssistantOrdinal": 1, "lastAssistantText": "已处理完这一轮"}
    if question is not None:
        detail["questionLike"] = question
    return {"id": eid, "kind": "awaiting_user", "sessionId": sid, "priority": prio,
            "sessionName": "n", "summary": "已停下，等待用户回复",
            "detectedAtMs": int(NOW * 1000), "detail": detail}


def supp(rows, entries=None, cfg_extra=None, stub=None):
    cfg = {"active_user_quiet_minutes": 30, "active_user_min_run": 2,
           "max_status_checks_per_cycle": 8}
    cfg.update(cfg_extra or {})
    saved = decide.future
    decide.future = stub or transcript_cli(entries or {})
    try:
        return decide.active_user_suppressed(cfg, rows, now=NOW)
    finally:
        decide.future = saved


CONVERSATION = {"s1": run_entries([("r1", 20, "completed"), ("r2", 3, "completed")])}

with tempfile.TemporaryDirectory() as tmp:
    saved_log = decide.LOG
    decide.LOG = os.path.join(tmp, "decide.log")
    try:
        ids, detail = supp([au("e1")], entries=CONVERSATION)
        check("an active conversation retires the stop", ids, ["e1"])
        check("the reason names how many runs it saw", "2 completed run" in detail, True)

        ids, _ = supp([au("e1", question=True)], entries={"s1": run_entries(
            [("r1", 2, "completed"), ("r2", 3, "completed"), ("r3", 4, "completed")])})
        check("a stop that asks him something is never suppressed", ids, [])
        ids, _ = supp([au("e1", question=None)], entries={"s1": run_entries(
            [("r1", 2, "completed"), ("r2", 3, "completed"), ("r3", 4, "completed")])})
        check("a missing questionLike is unknown, not 'no question'", ids, [])

        ids, _ = supp([au("e1")], entries={"s1": run_entries([("r1", 3, "completed")])})
        check("one run is a session waiting for him, not a conversation", ids, [])
        ids, _ = supp([au("e1")], entries={"s1": run_entries(
            [("r1", 45, "completed"), ("r2", 40, "completed")])})
        check("two runs, both older than the window", ids, [])
        ids, _ = supp([au("e1")], entries={"s1": run_entries(
            [("r1", 31, "completed"), ("r2", 30.5, "completed")])})
        check("just outside the window is outside", ids, [])

        ids, _ = supp([au("e1")], cfg_extra={"active_user_quiet_minutes": 0},
                      entries=CONVERSATION)
        check("quiet_minutes 0 is the documented off switch", ids, [])
        ids, _ = supp([au("e1")], cfg_extra={"active_user_min_run": 0}, entries=CONVERSATION)
        check("min_run 0 is the other off switch", ids, [])

        def unreachable(cfg, args, timeout=30):
            return 127, "", "no agent"
        ids, _ = supp([au("e1")], stub=unreachable)
        check("an unreadable transcript wakes as before", ids, [])

        ids, _ = supp([au("e1"), au("e2", "s2")],
                      cfg_extra={"max_status_checks_per_cycle": 1},
                      entries={"s1": CONVERSATION["s1"], "s2": CONVERSATION["s1"]})
        check("the status-check cap bounds how many sessions are read", ids, ["e1"])
        check("and the truncation is logged, not silent",
              "only the first 1 were checked" in open(decide.LOG, encoding="utf-8").read(),
              True)

        ids, _ = supp([au("e1"),
                       {"id": "e2", "kind": "run_error", "priority": "high",
                        "sessionId": "s1", "detail": {"lastRunStatus": "failed"}}],
                      entries=CONVERSATION)
        check("a failure in the same session is left alone", ids, ["e1"])
    finally:
        decide.LOG = saved_log


print("wake path: the user is mid-conversation")
E2E_CFG = {"heartbeat_minutes": 0, "min_wake_gap_seconds": 0, "skip_idle_heartbeat": True}


def pending_ids(tmp):
    path = os.path.join(tmp, "inbox", "pending.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(ln)["id"] for ln in open(path, encoding="utf-8") if ln.strip()]


def log_text(tmp):
    path = os.path.join(tmp, "logs", "decide.log")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


with tempfile.TemporaryDirectory() as tmp:
    rc, calls = run_main(tmp, lambda argv, **kw: None, pending_rows=(au("ev_1"),),
                         future_fn=transcript_cli(
                             {"s1": run_entries([("r1", 20, "completed"),
                                                 ("r2", 3, "completed")], now=_t.time())}),
                         cfg_extra=E2E_CFG)
    check("no wake while he is answering", len(calls), 0)
    check("the stop is retired from pending, not left to ride the next beat",
          pending_ids(tmp), [])
    handled = [json.loads(ln) for ln in open(os.path.join(tmp, "inbox", "handled.jsonl"),
                                             encoding="utf-8") if ln.strip()]
    check("it is recorded as handled", [e.get("id") for e in handled], ["ev_1"])
    check("with its own timestamp", bool(handled and handled[0].get("handledAtMs")), True)
    check("the log says why, and how many runs it saw",
          ("active-user-suppressed" in log_text(tmp)
           and "2 completed run" in log_text(tmp)), True)
    check("and no wake is claimed", os.path.exists(os.path.join(tmp, "state", "wakes.jsonl")),
          False)

# The safety valve end-to-end: it really asks him something, so it must still wake —
# and its event must stay pending until a turn acts on it.
with tempfile.TemporaryDirectory() as tmp:
    rc, calls = run_main(tmp, lambda argv, **kw: None,
                         pending_rows=(au("ev_1", question=True, prio="medium"),),
                         future_fn=transcript_cli(
                             {"s1": run_entries([("r1", 2, "completed"),
                                                 ("r2", 3, "completed")], now=_t.time())}),
                         cfg_extra=E2E_CFG)
    check("a question in a live conversation still wakes", len(calls), 1)
    check("and its event stays pending for the turn", pending_ids(tmp), ["ev_1"])

for label, spec in (
        ("a single recent run", [("r1", 3, "completed")]),
        ("two runs older than the window", [("r1", 45, "completed"), ("r2", 40, "completed")]),
):
    with tempfile.TemporaryDirectory() as tmp:
        rc, calls = run_main(tmp, lambda argv, **kw: None, pending_rows=(au("ev_1"),),
                             future_fn=transcript_cli({"s1": run_entries(spec, now=_t.time())}),
                             cfg_extra=E2E_CFG)
        check(f"{label} still wakes", len(calls), 1)
        check(f"{label}: the event stays pending", pending_ids(tmp), ["ev_1"])

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}):")
    for f in FAILED:
        print("  " + f)
    sys.exit(1)
print("selftest_decide: all checks passed")
