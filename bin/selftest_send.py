#!/usr/bin/env python3
"""Lock the timing rule for relayed messages: never send into a mid-run session.

Why this test exists
--------------------
The default `busy_policy` is `enqueue_if_busy`, so a relay aimed at a session
that is still running is only *read* after that run finishes. The message was
composed from what the scanner saw before that progress, so the target ends up
acting on a superseded instruction — measured once at 31 minutes late
(`docs/gotcha-send.md` A3). The user's rule (2026-10-16) is therefore about
timing: if the target is still mid-run, do not send now; send on the next wake,
when the new progress is actually in hand.

This test pins the *decision*, and that a refusal has no side effects: no
`relay-out.txt`, no guard `record`, and no `future run`. A refusal that still
records would burn the target's quota for a message never delivered.

Usage: python3 bin/selftest_send.py   (exit 0 = all checks passed)
"""
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPERVISOR_SID = "20261016-supervisor-session-for-tests"
FAILED = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "bin", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


send = load("send")


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


class Recorder:
    """Stand-in for `send.run`/`send.run_py` that answers the three call shapes."""

    def __init__(self, streaming, status_rc=0, info_missing=False, new_rc=0):
        self.streaming = streaming
        self.status_rc = status_rc
        self.info_missing = info_missing
        self.new_rc = new_rc
        self.calls = []
        self.recorded = False
        self.launched = []

    goal = False

    def future(self, args, timeout=60):
        self.calls.append(("future", args))
        if args[:2] == ["session", "info"]:
            if self.info_missing:
                # A session made by `session new` is not in the list until its
                # first run — exactly the `--new` case, and the reason for the
                # `session status` fallback in `resolve_cwd`.
                return 1, "", "Session not found: x"
            body = {"cwd": tempfile.gettempdir(), "sessionName": "target"}
            return 0, json.dumps(body), ""
        if args[:2] == ["session", "status"]:
            if self.status_rc != 0:
                return self.status_rc, "", "agent down"
            state = {"isStreaming": self.streaming, "queryCount": 3,
                     "cwd": tempfile.gettempdir()}
            if self.goal:
                state["appendSystemPrompt"] = ("You are an executor working under a "
                                               "deterministic control plane.")
            return 0, json.dumps(state), ""
        if args[:2] == ["session", "new"]:
            if self.new_rc != 0:
                return self.new_rc, "", "cannot create"
            return 0, json.dumps({"sessionId": "NEW-SID"}), ""
        return 0, "", ""

    def py(self, args, timeout=120):
        self.calls.append(("py", args))
        if "record" in args:
            self.recorded = True
        return 0, "OK (4h 0, 24h 0)", ""

    def shell_run(self, *args, **kwargs):
        """A relay is launched through bash; never let the test actually send."""
        self.launched.append(("run", args))
        return type("R", (), {"returncode": 0})()

    def shell_popen(self, *args, **kwargs):
        self.launched.append(("popen", args))
        return type("P", (), {"returncode": 0})()


class FakeSubprocess:
    """Only the two launchers are faked, but `DEVNULL` must still exist."""

    DEVNULL = os.devnull

    def __init__(self, rec):
        self._rec = rec

    def run(self, *args, **kwargs):
        return self._rec.shell_run(*args, **kwargs)

    def Popen(self, *args, **kwargs):  # noqa: N802 - mirrors the stdlib name
        return self._rec.shell_popen(*args, **kwargs)


# The queue `defer()` writes is a live runtime file (`state/deferred-sends.jsonl`)
# holding real relays waiting to be retried. A selftest that appended to it would
# inject test paths into the next wake's card, so every case runs against this
# throwaway file instead and `deferred_rows()` reads that same file back.
QUEUE_DIR = tempfile.mkdtemp(prefix="selftest-send-queue-")
QUEUE = os.path.join(QUEUE_DIR, "deferred-sends.jsonl")


def deferred_rows():
    if not os.path.exists(QUEUE):
        return []
    with open(QUEUE, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def with_recorder(streaming, status_rc=0, info_missing=False, new_rc=0):
    rec = Recorder(streaming, status_rc, info_missing, new_rc)
    send.run = rec.future
    send.run_py = rec.py
    send.subprocess = FakeSubprocess(rec)
    # The carve-out must be testable without a real state/monitor_session.txt.
    send.supervisor_session = lambda: SUPERVISOR_SID
    send.DEFERRED_PATH = QUEUE
    if os.path.exists(QUEUE):
        os.unlink(QUEUE)  # each case starts from an empty queue
    return rec


def run_main(argv):
    """Call main() with output swallowed; return its exit code."""
    saved = sys.stdout
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    try:
        return send.main(argv)
    finally:
        sys.stdout.close()
        sys.stdout = saved


print("read_state() + busy(): the three states")
with_recorder(True)
state, ok = send.read_state("sid")
check("state is readable", ok, True)
check("still running → True", send.busy(state), True)
with_recorder(False)
state, _ = send.read_state("sid")
check("idle → False", send.busy(state), False)
with_recorder(False, status_rc=1)
state, ok = send.read_state("sid")
check("unreadable state → ok=False", ok, False)
check("unreadable state → None (not False)", send.busy(state), None)

print("goal_owned(): a goal worker is never relayed into")
goal_state = {"sessionName": "求解 16x16 网格瓷砖铺放问题", "isStreaming": False,
              "appendSystemPrompt": "You are an executor working under a deterministic "
                                    "control plane.\n\nGOAL: ship it"}
owned, why = send.goal_owned("sid", goal_state)
check("boundary in the system prompt → owned", owned, True)
check("and it says why", "goal boundary" in why, True)

# The fallback for workers created before the append was recorded: the goal plane
# names a worker `truncate(objective, 60)`, and the objective comes from the
# registry under a configured root.
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, ".future", "goal"))
    with open(os.path.join(root, ".future", "goal", "registry.json"), "w", encoding="utf-8") as fh:
        json.dump([{"goal_id": "g1", "objective": "求解 16x16 网格瓷砖铺放问题：每行每列恰有一个单位格不被覆盖"}], fh)
    plain = {"sessionName": "求解 16x16 网格瓷砖铺放问题：每行每列恰有一", "isStreaming": False}
    owned, why = send.goal_owned("sid", plain, roots=[root])
    check("name matches a goal objective → owned", owned, True)
    check("and it says why (name)", "goal objective" in why, True)
    check("a real objective is discovered", len(send.goal_objectives([root])), 1)

print("goal_owned(): ordinary sessions are untouched")
mine = {"sessionName": "本地重启脚本与移动端重连", "isStreaming": False,
        "appendSystemPrompt": "一些项目规则"}
owned, _ = send.goal_owned("sid", mine, roots=[])
check("no marker, no name match → not owned", owned, False)
check("a short name never matches an objective",
      send.goal_owned("s", {"sessionName": "短"}, roots=["./nope"])[0], False)
check("unreadable state → not owned (warn-and-proceed)",
      send.goal_owned("s", None)[0], False)

print("goal rule + supervisor carve-out in main()")
with tempfile.TemporaryDirectory() as d:
    body = os.path.join(d, "m.txt")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("请看看这个 goal worker\n")
    rec = with_recorder(False)   # idle, so the goal guard is what fires
    rec.goal = True
    code = run_main(["target-sid", body])
    check("a goal worker is refused (exit 4)", code, 4)
    check("refusal has no side effects (no record)", rec.recorded, False)
    check("refusal launches nothing", rec.launched, [])
    # The carve-out: whatever the state says, the supervisor session must pass the
    # goal guard (it runs under a goal of its own).
    rec2 = with_recorder(False)
    rec2.goal = True
    code = run_main([SUPERVISOR_SID, body])
    check("the supervisor session is carved out", code, 0)
    check("and it is recorded", rec2.recorded, True)

print("main(): a mid-run target is deferred, with no side effects")
with tempfile.TemporaryDirectory() as d:
    body = os.path.join(d, "m.txt")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("please do the thing\n")
    rec = with_recorder(True)
    relay = os.path.join(ROOT, "state", "relay-out.txt")
    before = os.path.getmtime(relay) if os.path.exists(relay) else None
    code = run_main([ "target-sid", body, "--note", "probe" ])
    check("exit code is the documented 'deferred' one", code, 3)
    check("no guard record (quota not burned)", rec.recorded, False)
    check("no `future run` was launched",
          any(c[0] == "future" and c[1][:1] == ["run"] for c in rec.calls), False)
    after = os.path.getmtime(relay) if os.path.exists(relay) else None
    check("relay-out.txt not rewritten", before == after, True)
    # A refusal that leaves nothing behind is a message that never arrives
    # (docs/gotcha-send.md A21), so a timing refusal must queue the body.
    rows = deferred_rows()
    check("the refused relay is queued for the next wake", len(rows), 1)
    check("queued under its own session", rows and rows[0]["session"], "target-sid")
    check("queued with an absolute body path", rows and rows[0]["file"], os.path.abspath(body))
    check("the reason names the timing rule", rows and "mid-run" in rows[0]["reason"], True)
    check("the topic is carried, so the retry keeps its heading",
          rows and rows[0]["note"], "probe")

print("resolve_deferral(): a row written with a relative path still clears")
# Rows queued before 2026-10-18 stored `state/msg-*.md` relative to the watch root
# (the operator appended them by hand), while `defer()` stores an absolute path.
# Comparing raw strings left the old row behind after delivery, so the next card
# kept offering work that was already done.
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    body = os.path.join(root, "state", "m.md")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("body\n")
    send.DEFERRED_PATH = QUEUE
    with open(QUEUE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session": "target-sid", "file": "state/m.md"}) + "\n")
    saved_watch = send.WATCH
    send.WATCH = root  # so `state/m.md` resolves to the file that was queued
    try:
        send.resolve_deferral("target-sid", body)
    finally:
        send.WATCH = saved_watch
    check("the relative-path row is matched and removed", deferred_rows(), [])
    send.resolve_deferral("target-sid", body)  # must not raise on an empty queue
    check("clearing twice is harmless", deferred_rows(), [])

print("main(): a guard refusal queues instead of vanishing, and --force does not")
with tempfile.TemporaryDirectory() as d:
    body = os.path.join(d, "m.txt")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("correct this\n")
    rec = with_recorder(False)
    rec.py = lambda args, timeout=120: (1, "BLOCKED session hit 4h limit (3/3)", "")
    original_py = send.run_py
    send.run_py = rec.py
    try:
        code = run_main(["target-sid", body, "--note", "probe"])
    finally:
        send.run_py = original_py
    check("a guard refusal exits 1", code, 1)
    rows = deferred_rows()
    check("and it is queued rather than dropped", len(rows), 1)
    check("the reason names the guard", rows and "guard" in rows[0]["reason"], True)
    check("nothing was launched", rec.launched, [])

with tempfile.TemporaryDirectory() as d:
    body = os.path.join(d, "m.txt")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("STOP NOW\n")
    rec = with_recorder(False)
    original_py = send.run_py
    send.run_py = lambda args, timeout=120: (1, "BLOCKED session hit 4h limit (3/3)", "")
    try:
        code = run_main(["target-sid", body, "--force"])
    finally:
        send.run_py = original_py
    check("--force still overrides the guard", code, 0)
    check("and a forced send queues nothing", deferred_rows(), [])

print("main(): an idle target proceeds normally")
with tempfile.TemporaryDirectory() as d:
    body = os.path.join(d, "m.txt")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("go ahead\n")
    rec = with_recorder(False)
    code = run_main(["target-sid", body])
    check("idle target is not deferred", code, 0)
    check("idle target is recorded", rec.recorded, True)
    check("idle target was launched exactly once", len(rec.launched), 1)
    check("and a delivered relay leaves no queue row", deferred_rows(), [])

print("main(): --force still overrides (a real stop order must get through)")
with tempfile.TemporaryDirectory() as d:
    body = os.path.join(d, "m.txt")
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("STOP\n")
    rec = with_recorder(True)
    code = run_main(["target-sid", body, "--force", "--wait"])
    check("forced send is not deferred", code, 0)
    check("forced send is recorded", rec.recorded, True)
    check("forced send was launched exactly once", len(rec.launched), 1)

print("frame(): one topic line, never two")


def topics(framed):
    return [ln for ln in framed.splitlines() if "主题" in ln]


one = send.frame("**主题** 从正文里带来的主题\n\n正文第一段\n", "sid")
check("body topic appears exactly once", len(topics(one)), 1)
check("body topic is promoted into the header",
      topics(one)[0].startswith("**主题** 从正文里带来的主题"), True)
check("the body keeps its content", "正文第一段" in one, True)

both = send.frame("**主题** 从正文里带来的主题\n\n正文\n", "sid", note="显式的主题")
check("explicit --note wins and the body line is dropped", topics(both), ["**主题** 显式的主题"])
check("the dropped line is not left behind", "从正文里带来的主题" in both, False)

plain = send.frame("主题：中文冒号写法\n\n正文\n", "sid")
check("plain `主题：x` spelling is folded too", len(topics(plain)), 1)

# Note: the probe text must not itself contain the word 主题, or this helper
# would count the body as a topic line.
none = send.frame("普通正文，无人命名\n", "sid")
check("no topic line when neither body nor note has one", topics(none), [])
check("a note with no body topic still frames",
      topics(send.frame("正文\n", "sid", note="N")), ["**主题** N"])

# A heading inside the body must not be mistaken for the topic line.
kept = send.frame("## 小结\n正文\n", "sid")
check("a non-topic first line is left alone", topics(kept), [])
check("a non-topic first line stays in the body", "## 小结" in kept, True)

# Idempotence: an already-framed message is never framed (or de-topic'd) twice.
framed = send.frame("正文\n", "sid", note="T")
check("reframing is a no-op", send.frame(framed, "sid", note="OTHER"), framed)

print("quiet_intent(): a message that asks for nothing is refused, not delivered")
# The case the user flagged (2026-10-16): the relay only said "keep it visible,
# don't reopen it". Both halves needed answering, and neither asked the target to
# act — so the right disposition was a journal entry, not a run in its context.
noop = send.frame("**那 483 字节保持可见，不要隐藏——这条到此收尾，不要再为它花一轮。**\n\n理由…",
                  "sid", note="决定：保持可见、不隐藏")
check("a don't-do-it decision is quiet", send.quiet_intent(noop), True)
check("an already-expired question is quiet",
      send.quiet_intent(send.frame("那个 PR 已经合并了，不必重开。", "sid")), True)
check("保持现状 is quiet", send.quiet_intent(send.frame("保持现状即可。", "sid")), True)
check("只知悉 is quiet", send.quiet_intent(send.frame("只知悉：我改了日志路径。", "sid")), True)
# The gate must not swallow real work: a message that asks for an action stays a
# message, even when it contains a forbidding phrase further down.
check("an authorization is not quiet",
      send.quiet_intent(send.frame("可以开 PR 并合并。", "sid", note="授权")), False)
check("a stop order is not quiet by default",
      send.quiet_intent(send.frame("请立刻停下，别再改那个文件。", "sid")), False)
# A directive that also says "stop asking" is still a directive: the quiet marker
# governs the conversation, not the work. Measured 2026-10-19 on a real go-ahead
# ("往下推…不用再问") that the gate refused before the wording was changed.
check("a go-ahead phrased as 'don't ask again' is not quiet",
      send.quiet_intent(send.frame("往下推，按你排的顺序，不用再问。", "sid")), False)
check("still quiet when the target has nothing to do",
      send.quiet_intent(send.frame("不必再问了，保持现状。", "sid")), True)
# A directive that also says "stop asking" is still a directive: the quiet marker
# governs the conversation, not the work. Measured 2026-10-19 on a real go-ahead
# ("往下推…不用再问") that the gate refused before the wording was changed.
check("a go-ahead phrased as 'don't ask again' is not quiet",
      send.quiet_intent(send.frame("往下推，按你排的顺序，不用再问。", "sid")), False)
check("still quiet when the target has nothing to do",
      send.quiet_intent(send.frame("不必再问了，保持现状。", "sid")), True)
check("plain prose is not quiet",
      send.quiet_intent(send.frame("普通正文，无人命名\n", "sid")), False)
# Only the *opening* is probed: a quiet phrase met later is not a quiet message.
late = send.frame("请把这份报告补上 SHA 校验。\n\n另外，测试先保持现状。", "sid")
check("a quiet phrase only later in the body is ignored", send.quiet_intent(late), False)

print("main(): the awareness gate refuses before any session lookup")
# Exit code 5, and crucially *zero side effects*: no state read, no guard record,
# no send budget used. A refusal that still touched those would defeat the point.
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
    fh.write("**保持可见，不要隐藏。**\n\n理由…")
    quiet_file = fh.name
rec = with_recorder(False)
got = run_main(["20261016-000000-aaaa", quiet_file])
check("a no-action relay exits 5", got, 5)
check("and it reads no session", rec.calls, [])
os.unlink(quiet_file)

with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
    fh.write("请合并 PR #1132。")
    action_file = fh.name
with_recorder(False)
check("an action relay still passes the gate", run_main(["sid", action_file]), 0)
os.unlink(action_file)

print("resolve_cwd(): a fresh session is not in the list, so status must answer")
with_recorder(False, info_missing=True)
check("info fails → cwd comes from status", send.resolve_cwd("sid"), (tempfile.gettempdir(), ""))
with_recorder(False)
check("info works → cwd comes from info", send.resolve_cwd("sid"), (tempfile.gettempdir(), ""))
with_recorder(False, info_missing=True, status_rc=1)
cwd, why = send.resolve_cwd("sid")
check("neither reader works → no cwd (never the caller's)", cwd, None)
check("and it says why", "cannot read" in why, True)

print("--new: a new feature gets its own session in its own repo")
# The user's rule (2026-10-16): reusing a session carries its history into the new
# work, so a new feature is dispatched into a fresh session created in that repo.
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
    fh.write("实现一个导出 PDF 的新功能。")
    feature_file = fh.name
rec = with_recorder(False, info_missing=True)
rc = run_main(["--new", "--cwd", tempfile.gettempdir(), "--name", "导出 PDF",
               "--note", "新功能", feature_file])
check("exit code is 0", rc, 0)
created = [c for c in rec.calls if c[1][:2] == ["session", "new"]]
check("it created a session", len(created), 1)
check("in the repo it was given", created[0][1][created[0][1].index("--cwd") + 1],
      tempfile.gettempdir())
check("named after the feature", created[0][1][created[0][1].index("--name") + 1], "导出 PDF")
launched = rec.launched[0][1][0][2]  # ("popen", (["bash", "-c", script],)) → script
check("and the work order went to the NEW session", "NEW-SID" in launched, True)
check("never to the reused one", "--session sid " in launched, False)
check("--cwd of the run is the repo, not the caller", tempfile.gettempdir() in launched, True)
check("the topic survived", "--note" not in launched and "新功能" in open(
    os.path.join(send.WATCH, "state", "relay-out.txt"), encoding="utf-8").read(), True)

rec = with_recorder(False, new_rc=1)
check("a failed creation is reported, not sent",
      run_main(["--new", "--cwd", tempfile.gettempdir(), feature_file]), 1)
check("and nothing is launched", rec.launched, [])

rec = with_recorder(False)
check("a missing --cwd is refused",
      run_main(["--new", "--cwd", "/nope/does/not/exist", feature_file]), 1)
check("and nothing is launched", rec.launched, [])
os.unlink(feature_file)

if FAILED:
    print("selftest_send: FAILED")
    for line in FAILED:
        print("  " + line)
    sys.exit(1)
print("selftest_send: all checks passed")
