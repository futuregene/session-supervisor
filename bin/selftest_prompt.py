#!/usr/bin/env python3
"""Self-test for the wake-message composition and the append gate.

Run: python3 bin/selftest_prompt.py   (wired into bin/selfcheck.py / CI)

The rule under test is a *safety* rule as much as a cost one: the wake card is
the only place some operating instructions appear (use peek.py, ≤3-line replies,
where the whitelist lives). If the composition dropped the manual without the
system-prompt append being provably live, the supervisor would start each turn
without its manual — so every "minimal" path here must be earned by evidence,
and every failure mode must fall back to the full message.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "bin", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


turn_prompt = load("turn_prompt")
apply_prompt = load("apply_prompt")


def check(name, got, want):
    if got != want:
        FAILED.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


def fixture():
    d = tempfile.mkdtemp(prefix="selftest-prompt-")
    for sub in ("prompts", "state"):
        os.makedirs(os.path.join(d, sub))
    with open(os.path.join(d, "prompts", "append.md"), "w", encoding="utf-8") as fh:
        fh.write("# manual\n- rule one\n")
    with open(os.path.join(d, "prompts", "turn.md"), "w", encoding="utf-8") as fh:
        fh.write("# card\n- this turn\n")
    return d


def apply_record(d, sha, canary=True):
    with open(os.path.join(d, "state", "append_applied.json"), "w", encoding="utf-8") as fh:
        json.dump({"sha256": sha, "canary_ok": canary, "binary": "0.0.0-test",
                   "verifiedAt": "2026-01-01 00:00:00"}, fh)


print("mode gate")
d = fixture()
check("no record → full", turn_prompt.mode(d)[0], "full")
check("full message carries the manual", "# manual" in turn_prompt.compose(d), True)
check("full message carries the card", "# card" in turn_prompt.compose(d), True)
check("full message is labelled", "mode=full" in turn_prompt.compose(d), True)

sha = turn_prompt.manual_digest(d)
# Regression (2026-10-16): this test used to hash the *raw* file while
# `apply_prompt.py` records the *stripped* text, so it passed while production
# could never flip to `minimal` (a trailing newline always changed the hash).
# Lock the two sides together, and check the real contract explicitly.
check("turn_prompt and apply_prompt hash the same text",
      sha, apply_prompt.sha256_text("# manual\n- rule one"))
apply_record(d, sha, canary=False)
check("recorded but never verified → full", turn_prompt.mode(d)[0], "full")

apply_record(d, sha, canary=True)
check("verified + matching → minimal", turn_prompt.mode(d)[0], "minimal")
check("minimal message drops the manual", "# manual" in turn_prompt.compose(d), False)
check("minimal message keeps the card", "# card" in turn_prompt.compose(d), True)
check("minimal message is labelled", "mode=minimal" in turn_prompt.compose(d), True)

# The append is set on the *session*, so an edit to the manual would otherwise
# keep the old text live while the card stops repeating it.
with open(os.path.join(d, "prompts", "append.md"), "a", encoding="utf-8") as fh:
    fh.write("- rule two\n")
check("manual edited after applying → full", turn_prompt.mode(d)[0], "full")
check("edited manual is sent again", "- rule two" in turn_prompt.compose(d), True)

# Corruption of the record must not be read as "verified".
with open(os.path.join(d, "state", "append_applied.json"), "w", encoding="utf-8") as fh:
    fh.write("{not json")
check("corrupt record → full", turn_prompt.mode(d)[0], "full")

# A missing manual must never produce a manual-less message.
shutil.rmtree(d)
print("degenerate inputs")
d = fixture()
os.remove(os.path.join(d, "prompts", "append.md"))
apply_record(d, "whatever", canary=True)
check("missing manual → full", turn_prompt.mode(d)[0], "full")
check("missing manual still sends the card", "# card" in turn_prompt.compose(d), True)
shutil.rmtree(d)

print("the triage is rendered, not attached as JSON")
none = fixture()
# No payload on disk (a fresh checkout, or a wake that carried none): the card
# must still compose, without an empty triage heading.
check("no payload → composes without a triage heading",
      "待处理事件" in turn_prompt.compose(none), False)
shutil.rmtree(none)

d2 = fixture()
with open(os.path.join(d2, "state", "turn_payload.json"), "w", encoding="utf-8") as fh:
    json.dump({"wakeReason": "events", "pendingCount": 1, "pendingHighMedium": 1,
               "wakesLastHour": 2, "wakesLastDay": 9, "wokenAtMs": 1234567890,
               "monitorSessionId": "MONITOR-SHOULD-NOT-APPEAR",
               "scannerHealth": {"ok": True, "sessionsTotal": 78, "active": 69,
                                 "baseline": False, "ts": 1.5,
                                 "degraded": "list below peak"},
               "pendingEvents": [{"id": "ev_1", "kind": "failed_run",
                                  "priority": "high", "sessionName": "demo",
                                  "sessionId": "sess-1", "summary": "run failed"}]}, fh)
composed = turn_prompt.compose(d2)
check("triage section appears", "待处理事件（1 条 · 高/中 1）" in composed, True)
check("the event id survives (bin tools need it)", "`ev_1`" in composed, True)
check("a high-priority event is marked", "**high**" in composed, True)
check("session name and summary survive", "demo" in composed and "run failed" in composed, True)
check("scanner health is summarised, not dumped", "list below peak" in composed, True)
# The point of the change: no raw JSON blob the reader has to parse.
check("no raw JSON braces in the message", '{"' not in composed, True)
check("no <file> attachment rendering", "<file name=" not in composed, True)
for dropped in ("MONITOR-SHOULD-NOT-APPEAR", "wokenAtMs", "1234567890", "baseline", "ts\":"):
    check(f"field dropped: {dropped}", dropped in composed, False)
shutil.rmtree(d2)

print("one line per (session, kind)")
d3 = fixture()
with open(os.path.join(d3, "state", "turn_payload.json"), "w", encoding="utf-8") as fh:
    json.dump({"wakeReason": "events", "pendingCount": 4, "pendingHighMedium": 1,
               "wakesLastHour": 1, "wakesLastDay": 3,
               "scannerHealth": {"ok": True},
               "pendingEvents": [
                   {"id": "ev_old1", "kind": "awaiting_user", "priority": "low",
                    "sessionName": "蓝气泡", "sessionId": "sess-blue",
                    "summary": "已停下，等待用户回复"},
                   {"id": "ev_old2", "kind": "awaiting_user", "priority": "low",
                    "sessionName": "蓝气泡", "sessionId": "sess-blue",
                    "summary": "已停下，等待用户回复"},
                   {"id": "ev_new", "kind": "awaiting_user", "priority": "low",
                    "sessionName": "蓝气泡", "sessionId": "sess-blue",
                    "summary": "已停下，等待用户回复"},
                   {"id": "ev_err", "kind": "run_error", "priority": "high",
                    "sessionName": "蓝气泡", "sessionId": "sess-blue",
                    "summary": "最近一次运行状态：failed"}]}, fh)
composed3 = turn_prompt.compose(d3)
# `bin/decide.py` folds superseded events before building the payload; the renderer
# repeats the rule because it is the last place that guarantees the reader does not
# see one session three times (measured 2026-10-16 21:47: 6 events, 3 sessions).
check("one bullet per (session, kind), not per event", composed3.count("- `ev_"), 2)
check("the newest event of the group is the one shown", "\n- `ev_new`" in composed3, True)
check("a different kind is not folded into it", "\n- `ev_err`" in composed3, True)
check("superseded ids stay on the line (they are ack handles)",
      "`ev_old1`" in composed3 and "`ev_old2`" in composed3, True)
check("the fold is stated, not silent", "同会话更早 2 条同类已折叠" in composed3, True)
check("the session is named once per surviving kind", composed3.count("`sess-blue`"), 2)
shutil.rmtree(d3)

print("the real prompts")
check("prompts/append.md exists", os.path.exists(os.path.join(ROOT, "prompts", "append.md")), True)
check("prompts/turn.md exists", os.path.exists(os.path.join(ROOT, "prompts", "turn.md")), True)
real = turn_prompt.compose(ROOT)
check("real composition is non-empty", len(real) > 500, True)
manual = open(os.path.join(ROOT, "prompts", "append.md"), encoding="utf-8").read()
check("real manual names the lock step", "lock.py acquire" in manual, True)
check("real manual names peek.py", "peek.py" in manual, True)
card = open(os.path.join(ROOT, "prompts", "turn.md"), encoding="utf-8").read()
check("card stays small (≤1200 B)", len(card.encode()) <= 1200, True)
# The card must not re-state the manual: that duplication is what made a card a
# whole page of rules, and the ≤5 sessions / ≤25 calls limit already lives in
# append.md and AGENTS.md §2 (user, 2026-10-16: it is noise in the card).
check("card does not repeat the per-turn limit", "25 次工具调用" in card, False)
# `state/turn_payload.json` is gitignored, so CI has none: assert the inline
# triage only when this checkout actually has a payload to render.
if os.path.exists(os.path.join(ROOT, "state", "turn_payload.json")):
    check("real composition renders the triage inline", "待处理事件" in real, True)
else:
    print("  skip real triage (no state/turn_payload.json in this checkout)")
check("real composition carries no raw <file> blob", "<file name=" in real, False)
# The saving this whole mechanism exists for.
check("manual is the large part", len(manual.encode()) > len(card.encode()), True)

print("pending sends: a row whose body was lost is REPORTED, never skipped")
# A queue row that survives while its body file is gone used to be skipped in
# silence, which is A21 again through a side door: the row is real work, no card
# ever shows it, and the message never arrives. It happened for real on
# 2026-10-18 -- a queued "keep PR #1175 open" was cleaned up at turn end and the
# session merged. The card must say the body is lost.
import tempfile
with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    queue = os.path.join(d, "state", "deferred-sends.jsonl")
    body = os.path.join(d, "present.md")
    open(body, "w", encoding="utf-8").write("body\n")
    rows = [
        {"ts": "2026-10-18T00:00:00+08:00", "session": "SESS-OK", "file": body,
         "note": "deliverable", "expires": "2099-01-01T00:00:00+08:00"},
        {"ts": "2026-10-18T00:00:00+08:00", "session": "SESS-LOST",
         "file": os.path.join(d, "gone.md"), "note": "never delivered",
         "expires": "2099-01-01T00:00:00+08:00"},
    ]
    with open(queue, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    got = turn_prompt._pending_sends(d)
    check("both rows are rendered (none silently dropped)", len(got), 2)
    check("the lost body is called out", any("正文已丢失" in g for g in got), True)
    check("and it says the message was never delivered",
          any("从未送达" in g for g in got), True)

print("stdout is UTF-8, not the host code page")
# Regression (2026-10-20): with stdout on a pipe and no encoding override, Python on
# Windows encodes with the ANSI code page (cp936 here). `wake.sh` captured those
# bytes with `$(...)`, Git Bash handed them to the CLI as if they were UTF-8, and the
# user saw the whole card as `# һ´λ½ÐѻغÏ` instead of `# 一次唤醒回合`.
# The contract is byte-level, so assert it on a *separate* process whose environment
# carries no encoding override: the composition itself was never wrong, the transport
# was.
_env = {k: v for k, v in os.environ.items()
        if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
_proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "turn_prompt.py")],
                       capture_output=True, env=_env)
try:
    _decoded = _proc.stdout.decode("utf-8")
    _utf8_ok = True
except UnicodeDecodeError:
    _decoded, _utf8_ok = "", False
check("composed bytes are valid UTF-8", _utf8_ok, True)
check("they decode to the composition, byte for byte",
      _decoded.strip(), turn_prompt.compose(ROOT).strip() if _utf8_ok else "")
check("and the Chinese survives the pipe", "唤醒" in _decoded, True)
check("no cp936 mojibake reached the bytes", "һ" in _decoded, False)

print()
if FAILED:
    print(f"FAILED ({len(FAILED)}):")
    for f in FAILED:
        print("  " + f)
    sys.exit(1)
print("selftest_prompt: all checks passed")
