#!/usr/bin/env python3
"""Selftest for bin/monitor_roll.py's decide-in-place-or-roll logic.

Two user requests shape it. 2026-10-16 (morning): a long supervisor conversation
should be *compacted* rather than replaced: 「能不能不要开启新会话，而是删除总管会话的历史部分，然后进行
压缩」. 2026-10-16 (afternoon): not compacted either — **reset**: 「如果是的话，不要这么做了，
用这个 reset」. So the module now drops this session's own history in place and only
opens a new session when that is impossible (a `future` binary without
`session reset`, or a reset that fails). Getting this wrong is expensive in both
directions — rotating anyway ignores the request, and never rotating wedges the
session (the 2026-10-15 incident) — so every branch is pinned here.

`future` is replaced by a scripted stub; no agent, no session, no network.
"""
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import monitor_roll as mr  # noqa: E402

FAILED = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILED.append(name)


class Stub:
    """Records `future` calls and answers with canned JSON per subcommand."""

    def __init__(self, pct=90.0, queries=1, streaming=False, compact_rc=0,
                 reset_supported=True, reset_rc=0, reset_all_rc=0,
                 reset_probe_rc=0, agent_not_upgraded=False, busy=False):
        self.pct = pct
        self.queries = queries
        self.streaming = streaming
        self.compact_rc = compact_rc
        self.reset_supported = reset_supported
        self.reset_rc = reset_rc
        self.reset_all_rc = reset_all_rc
        self.reset_probe_rc = reset_probe_rc
        self.agent_not_upgraded = agent_not_upgraded
        self.busy = busy
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        if args[:2] == ["session", "status"]:
            return 0, json.dumps({"contextPercent": self.pct,
                                  "queryCount": self.queries,
                                  "isStreaming": self.streaming}), ""
        if args[:2] == ["session", "compact"]:
            return self.compact_rc, "", ("boom" if self.compact_rc else "")
        if args[:2] == ["session", "new"]:
            return 0, json.dumps({"sessionId": "NEW-SESSION"}), ""
        if args[:2] == ["session", "reset"]:
            if "--help" in args:
                if self.reset_supported:
                    return 0, "future session reset — delete part of a history", ""
                return 1, "", "Unknown command: reset"
            if self.busy:
                return 1, "", mr.BUSY_MARKERS[0]
            if "--yes" not in args:  # the capability probe: a dry run
                if self.reset_probe_rc:
                    err = mr.AGENT_NOT_UPGRADED if self.agent_not_upgraded else "probe failed"
                    return self.reset_probe_rc, "", err
                return 0, "plan", ""
            rc = self.reset_all_rc if "--all" in args else self.reset_rc
            err = mr.AGENT_NOT_UPGRADED if (rc and self.agent_not_upgraded) else ("reset failed" if rc else "")
            return rc, json.dumps({"applied": rc == 0}), err
        return 0, "{}", ""

    def compacted(self):
        return any(c[:2] == ["session", "compact"] for c in self.calls)

    def created(self):
        return any(c[:2] == ["session", "new"] for c in self.calls)

    def resets(self):
        """The applying `session reset` calls (not the probe, not `--help`)."""
        return [c for c in self.calls
                if c[:2] == ["session", "reset"] and "--help" not in c and "--yes" in c]

    def probes(self):
        """The read-only capability probes (a reset without `--yes`)."""
        return [c for c in self.calls
                if c[:2] == ["session", "reset"] and "--help" not in c and "--yes" not in c]

    def reset_all(self):
        return [c for c in self.resets() if "--all" in c]


def rig(pct=90.0, queries=1, streaming=False, compact_rc=0, marker=None,
        threshold=30, extra_cfg=None, reset_marker=None, request=None, **stub_kw):
    """Point the module at temp files and install the stub; return the stub."""
    tmp = tempfile.mkdtemp()
    monitor_file = os.path.join(tmp, "monitor_session.txt")
    with open(monitor_file, "w", encoding="utf-8") as fh:
        fh.write("CUR\n")
    mr.MONITOR_FILE = monitor_file
    mr.SESSION_ID_FILE = os.path.join(tmp, "session_id.txt")
    mr.JOURNAL = os.path.join(tmp, "journal.md")
    mr.COMPACT_MARKER = os.path.join(tmp, "monitor_compacted.json")
    mr.RESET_MARKER = os.path.join(tmp, "monitor_reset.json")
    mr.RESET_REQUEST = os.path.join(tmp, "reset_requested.json")
    mr.CFG_PATH = os.path.join(tmp, "config.json")
    # `last_wake_wedged` reads WATCH/logs and `apply_manual` looks for
    # WATCH/bin/apply_prompt.py: point both at the rig so a test can control them.
    mr.WATCH = tmp
    cfg = {"monitor_roll_context_percent": threshold}
    cfg.update(extra_cfg or {})
    with open(mr.CFG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    if marker is not None:
        session, age = marker
        with open(mr.COMPACT_MARKER, "w", encoding="utf-8") as fh:
            json.dump({"session": session, "at": time.time() - age}, fh)
    if request is not None:
        with open(mr.RESET_REQUEST, "w", encoding="utf-8") as fh:
            fh.write(request)
    if reset_marker is not None:
        session, age, at_queries = reset_marker[:3]
        extra = reset_marker[3] if len(reset_marker) > 3 else {}
        marker_data = {"session": session, "at": time.time() - age, "queries": at_queries}
        marker_data.update(extra)
        with open(mr.RESET_MARKER, "w", encoding="utf-8") as fh:
            json.dump(marker_data, fh)
    stub = Stub(pct=pct, queries=queries, streaming=streaming, compact_rc=compact_rc,
                **stub_kw)
    stub.tmp = tmp
    mr.future = stub
    return stub


def wedged_wake_log(tmp, text, monitor="CUR"):
    """Leave a wake log that says this session came back wedged."""
    logdir = os.path.join(tmp, "logs")
    os.makedirs(logdir, exist_ok=True)
    with open(os.path.join(logdir, "wake-20261016-150000-heartbeat.log"),
              "w", encoding="utf-8") as fh:
        fh.write(f"=== wake 20261016-150000 reason=heartbeat monitor={monitor} ===\n"
                 f"Error: session persistence is unavailable: {text}\n")


def wake(argv=()):
    """Run one wake; return (exit code, the session id it printed)."""
    saved, cap = sys.stdout, io.StringIO()
    sys.stdout = cap
    try:
        rc = mr.main(list(argv))
    finally:
        sys.stdout = saved
    return rc, cap.getvalue().strip()


print("over the threshold: reset THIS session's history in place (user 2026-10-16)")
stub = rig(pct=90.0)
rc, out = wake()
check("exit code is 0", rc, 0)
check("it reset the current session", len(stub.resets()), 1)
check("the reset targets the current session", stub.resets()[0][2], "CUR")
check("it keeps a tail and drops the bulky kinds",
      ("--keep-last" in stub.resets()[0], "--drop" in stub.resets()[0]),
      (True, True))
check("it applies (not a dry run)", "--yes" in stub.resets()[0], True)
check("it did NOT open a new session", stub.created(), False)
check("and it did not compact instead", stub.compacted(), False)
check("the same session is still the monitor", out.endswith("CUR"), True)
check("the marker now names this session",
      json.load(open(mr.RESET_MARKER, encoding="utf-8"))["session"], "CUR")

print("reset, settled, and the reset DID shrink the history: reset again, do not swap")
stub = rig(pct=90.0, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 120, 1,
                                   {"pct": 90.0, "after": 20.0,
                                    "after_at": time.time() - 60}))
_rc, out = wake()
check("it reset a second time", len(stub.resets()), 1)
check("and did NOT open a new session", stub.created(), False)
check("the same session is still the monitor", out.endswith("CUR"), True)

print("reset, settled, and the reset bought nothing: replace the session")
stub = rig(pct=90.0, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 120, 1,
                                   {"pct": 90.0, "after": 89.0,
                                    "after_at": time.time() - 60}))
_rc, out = wake()
check("it opened a new session", stub.created(), True)
check("it did not reset a second time", stub.resets(), [])
check("the new id is what gets awaited", out.endswith("NEW-SESSION"), True)

print("2026-10-18 regression: the sample was taken 13 h late, after the context regrew")
# The 11:05 reset really did drop the history, but the only reading on file came from
# the 00:06 threshold hit (32.5% against a 30.0% trigger). Scoring that as "did not
# shrink" rolled a session the user had asked to keep. A sample that late (or a legacy
# sample with no timestamp at all) proves nothing and must not justify a roll.
stub = rig(pct=90.0, reset_marker=("CUR", 13 * 3600, 1,
                                   {"pct": 30.0, "after": 32.5}))
_rc, _out = wake()
check("a stale sample is not evidence against resetting", stub.created(), False)
check("so it resets in place instead of rolling", len(stub.resets()), 1)

print("a marker from an older build has no readings: retry the reset once")
stub = rig(pct=90.0, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 60, 1))
_rc, out = wake()
check("it retries in place rather than rolling on a guess", len(stub.resets()), 1)
check("and does not open a new session yet", stub.created(), False)

print("...but a second consecutive unjudgeable reset rolls instead of looping")
stub = rig(pct=90.0, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 60, 1,
                                   {"unknown_tries": mr.RESET_UNKNOWN_RETRIES}))
_rc, out = wake()
check("it stops guessing and opens a new session", stub.created(), True)
check("and does not reset again", stub.resets(), [])

print("a calm wake records the post-reset reading (the 2026-10-18 bug)")
# Sampling only on a threshold hit meant the reading was always taken after the
# history had grown back. Every calm wake inside the window has to record it.
stub = rig(pct=9.0, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 60, 1,
                                  {"pct": 30.0}))
_rc, _out = wake()
with open(mr.RESET_MARKER, encoding="utf-8") as fh:
    recorded = json.load(fh)
check("the calm wake stored the reading", recorded.get("after"), 9.0)
check("and timestamped it", isinstance(recorded.get("after_at"), float), True)
check("without resetting or rolling", (stub.resets(), stub.created()), ([], False))


print("a forced roll is not vetoed by a marker that says the reset worked")
stub = rig(pct=90.0, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 60, 1,
                                   {"pct": 90.0, "after": 20.0}))
mr.main(["--force"])
check("force still resets in place (it keeps the identity)", len(stub.resets()) >= 1, True)

print("reset but not yet settled: keep waiting, do not reset or roll again")
stub = rig(pct=90.0, reset_marker=("CUR", 5, 1))
_rc, out = wake()
check("no new session while the effect is still landing", stub.created(), False)
check("and no second reset request", stub.resets(), [])
check("the same session is awaited again", out.endswith("CUR"), True)

print("a marker from a previous session must not silence the trigger")
stub = rig(pct=90.0, reset_marker=("OLD-SESSION", mr.RESET_SETTLE_SECONDS * 4, 1))
_rc, _out = wake()
check("the fresh session still resets", len(stub.resets()), 1)
check("and is not replaced", stub.created(), False)

print("a failed reset falls back to opening a new session")
stub = rig(pct=90.0, reset_rc=1)
_rc, out = wake()
check("it rolls", stub.created(), True)
check("the new session is what gets awaited", out.endswith("NEW-SESSION"), True)
check("and no marker is written (so a retry is not silenced)",
      os.path.exists(mr.RESET_MARKER), False)

print("a NEW CLI with an OLD agent (upgraded separately) falls back too, and says why")
stub = rig(pct=90.0, agent_not_upgraded=True, reset_probe_rc=1)
_rc, out = wake()
check("it probes the agent before applying", len(stub.probes()), 1)
check("it does not attempt an apply the agent cannot serve", stub.resets(), [])
check("it opens a new session", stub.created(), True)
check("the new session is awaited", out.endswith("NEW-SESSION"), True)
check("the journal names the stale agent, not a generic failure",
      "agent 进程还是旧构建" in open(mr.JOURNAL, encoding="utf-8").read(), True)

print("an apply that fails BECAUSE the agent is old says so too")
stub = rig(pct=90.0, reset_rc=1, reset_probe_rc=0, agent_not_upgraded=True)
_rc, _out = wake()
check("the probe passed (agent answered) so it tried to apply", len(stub.resets()), 1)
check("and the fallback names the stale agent",
      "agent 进程还是旧构建" in open(mr.JOURNAL, encoding="utf-8").read(), True)

print("a busy session is NOT rolled away: the agent says retryable")
stub = rig(pct=90.0, busy=True)
_rc, out = wake()
check("it tried to reset", len(stub.resets()), 1)
check("it did not open a new session", stub.created(), False)
check("the same session is awaited", out.endswith("CUR"), True)
check("no marker is written (nothing was applied)",
      os.path.exists(mr.RESET_MARKER), False)

print("a busy probe still counts as support (the agent knows the command)")
stub = rig(pct=90.0, busy=True)
_rc, _out = wake()
check("the probe accepted the busy refusal as support", stub.created(), False)

print("on demand: a request file resets this session without any threshold")
stub = rig(pct=1.0, queries=1, request="CUR")
_rc, out = wake()
check("the request alone triggered a reset", len(stub.resets()), 1)
check("it did not open a new session", stub.created(), False)
check("and the session is kept", out.endswith("CUR"), True)
check("the request is consumed (one-shot)",
      os.path.exists(mr.RESET_REQUEST), False)
check("the journal says it was on request",
      "按请求原地重置" in open(mr.JOURNAL, encoding="utf-8").read(), True)

print("on demand: a request naming another session is ignored, and left alone")
stub = rig(pct=1.0, request="OTHER-SESSION")
_rc, out = wake()
check("nothing was reset", stub.resets(), [])
check("nothing was created", stub.created(), False)
check("the file is untouched", os.path.exists(mr.RESET_REQUEST), True)
check("and the current session is simply awaited", out.endswith("CUR"), True)

print("on demand: a busy session keeps its request for the next wake")
stub = rig(pct=1.0, request="CUR", busy=True)
_rc, _out = wake()
check("it tried", len(stub.resets()), 1)
check("the request survives a busy refusal",
      os.path.exists(mr.RESET_REQUEST), True)

print("on demand: a settled marker from an earlier reset does not veto a new request")
stub = rig(pct=1.0, request="CUR", reset_marker=("CUR", mr.RESET_SETTLE_SECONDS * 4, 5))
_rc, out = wake()
check("it reset again on request", len(stub.resets()), 1)
check("and kept the same session", out.endswith("CUR"), True)

print("on demand: a JSON body naming the session works too")
stub = rig(pct=1.0, request='{"session": "CUR"}')
_rc, _out = wake()
check("it reset", len(stub.resets()), 1)

print("a binary without `session reset` (pre-#1139) falls back to rolling, loudly")
stub = rig(pct=90.0, reset_supported=False)
_rc, out = wake()
check("it opens a new session", stub.created(), True)
check("it does not attempt to apply an unknown subcommand", stub.resets(), [])
check("the new session is awaited", out.endswith("NEW-SESSION"), True)
check("the journal says the binary has to be refreshed",
      "重新安装二进制" in open(mr.JOURNAL, encoding="utf-8").read(), True)

print("other triggers and safe states")
stub = rig(pct=90.0, streaming=True)
_rc, out = wake()
check("mid-run never touches the session",
      (stub.compacted(), stub.created(), stub.resets()), (False, False, []))
check("and the live session is kept", out.endswith("CUR"), True)
stub = rig(pct=10.0)
_rc, _out = wake()
check("under the threshold: nothing happens",
      (stub.compacted(), stub.created(), stub.resets()), (False, False, []))
stub = rig(pct=10.0, queries=999)
_rc, out = wake()
check("the wake count resets in place too (that trigger is about user originals)",
      (len(stub.resets()), stub.created()), (1, False))
check("and the same session is awaited", out.endswith("CUR"), True)
check("without compacting", stub.compacted(), False)

print("the wake-count trigger re-arms from the last reset, not from birth")
stub = rig(pct=10.0, queries=999, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 60, 980))
_rc, out = wake()
check("60 wakes have not accumulated since that reset",
      (stub.resets(), stub.created()), ([], False))
check("so the same session is simply awaited", out.endswith("CUR"), True)
stub = rig(pct=10.0, queries=1100, reset_marker=("CUR", mr.RESET_SETTLE_SECONDS + 60, 980))
_rc, _out = wake()
check("but 120 wakes later it resets again", len(stub.resets()), 1)

print("a wedged wake log resets in place (both shapes of wedge)")
stub = rig(pct=5.0)
wedged_wake_log(stub.tmp, "context compaction budget exceeded")
_rc, out = wake()
check("the compaction-budget wedge resets", len(stub.resets()), 1)
check("and keeps the same session", out.endswith("CUR"), True)
stub = rig(pct=5.0)
wedged_wake_log(stub.tmp, "cannot append to a compacted settled run")
_rc, out = wake()
check("the in-place-compaction wedge resets (2026-10-16)", len(stub.resets()), 1)
check("and keeps the same session", out.endswith("CUR"), True)
stub = rig(pct=5.0)
wedged_wake_log(stub.tmp, "cannot append to a compacted settled run", monitor="OTHER")
_rc, out = wake()
check("a wedged log of another session does not touch this one",
      (stub.resets(), stub.created()), ([], False))
check("and the current session is kept", out.endswith("CUR"), True)

print("a wedge that survives the slimming reset gets the strongest in-place reset")
stub = rig(pct=5.0, reset_rc=1, reset_all_rc=0)
wedged_wake_log(stub.tmp, "cannot append to a compacted settled run")
_rc, out = wake()
check("it fell back to --all", len(stub.reset_all()), 1)
check("and still did not open a new session", stub.created(), False)
check("the same session stays the monitor", out.endswith("CUR"), True)
stub = rig(pct=5.0, reset_rc=1, reset_all_rc=1)
wedged_wake_log(stub.tmp, "cannot append to a compacted settled run")
_rc, out = wake()
check("only when even --all fails does it roll", stub.created(), True)

print("--force resets in place too (the escape hatch no longer needs a new id)")
stub = rig(pct=0.0, queries=1)
_rc, out = wake(["--force"])
check("forced wake resets", len(stub.resets()), 1)
check("without opening a new session", stub.created(), False)
check("and keeps the session", out.endswith("CUR"), True)

print("a roll still hands the operating manual to the session it just created")
stub = rig(pct=90.0, queries=999, reset_supported=False)
os.makedirs(os.path.join(stub.tmp, "bin"), exist_ok=True)
with open(os.path.join(stub.tmp, "bin", "apply_prompt.py"), "w", encoding="utf-8"):
    pass
applies = []


class RunResult:
    returncode = 0
    stdout = "applied + verified"
    stderr = ""


saved_run = mr.subprocess.run
mr.subprocess.run = lambda argv, **kw: (applies.append(list(argv)), RunResult())[1]
try:
    _rc, out = wake()
finally:
    mr.subprocess.run = saved_run
check("the roll still lands on the new session", out.endswith("NEW-SESSION"), True)
check("apply_prompt is run with an explicit --session target",
      any("--apply" in c and "--session" in c for c in applies), True)
check("and the target is the session just created",
      [c[c.index("--session") + 1] for c in applies if "--session" in c], ["NEW-SESSION"])

print("escape hatch 1: monitor_reset_in_place=false restores plain rolling")
stub = rig(pct=90.0, extra_cfg={"monitor_reset_in_place": False,
                                "monitor_compact_first": False})
_rc, out = wake()
check("it rolls immediately", stub.created(), True)
check("without resetting", stub.resets(), [])
check("without compacting", stub.compacted(), False)
check("the new session is awaited", out.endswith("NEW-SESSION"), True)

print("escape hatch 2: reset off + compact_first on restores the compaction detour")
stub = rig(pct=90.0, extra_cfg={"monitor_reset_in_place": False})
_rc, out = wake()
check("it compacts the same session", stub.compacted(), True)
check("and does not roll while the effect may still land", stub.created(), False)
check("the same session is awaited", out.endswith("CUR"), True)

print("the keep-last count is configurable")
stub = rig(pct=90.0, extra_cfg={"monitor_reset_keep_last": 5})
_rc, _out = wake()
keep = stub.resets()[0]
check("the configured tail is passed to the reset",
      keep[keep.index("--keep-last") + 1], "5")

if FAILED:
    print("selftest_roll: FAILED")
    for line in FAILED:
        print("  " + line)
    sys.exit(1)
print("selftest_roll: all checks passed")
