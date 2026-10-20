#!/usr/bin/env python3
"""Roll the supervisor onto a fresh session when the current one gets long.

Why: every wake appends a whole turn (this card + payload + the model's tool
calls and their results) to ONE session forever. Measured 2026-10-15 11:00:
47 queries, 755 messages, 354 tool calls, contextTokens=309,427 / 1M = 30.9%,
input tokens replayed 67.7M. Compaction does not solve this — it rewrites the
middle but the session keeps growing, and the user explicitly said compaction
"帮助不大". So: start a NEW monitor session at a threshold and archive the old
one. The durable memory is not the chat log, it is the repo (`journal/`,
`reports/`, `docs/`, `POLICY.md`) — so a fresh session loses nothing that
matters, and turn.md/AGENTS.md tell it to read those first.

**In-place reset instead of a new session (user, 2026-10-16).** The user asked
whether the supervisor really opens a new session every so often, and told us to
stop doing that: 「如果是的话，不要这么做了，用这个 reset」. So a trigger now drops
THIS session's own history with `future session reset` (CLI PR #1139): the id, its
name, its cwd, its model, its permissions and both prompt segments survive — only
entries go — and the agent writes a lossless backup first. Rotating onto a new
session stays as the FALLBACK for when the reset cannot be used (a `future` or an
agent process older than #1139, or a reset that fails), because the alternative is
the wedged session of 2026-10-15/16, which is worse than a new id. The CLI and the
agent upgrade separately, so the capability probe asks the **agent** too (a dry
run): on 2026-10-16 17:10 the CLI was new while the agent was still the 16:30 build.

**Context percent alone is not a sufficient trigger (incident 2026-10-15).** That
day the monitor died at **58.1% / 103 wakes**, before the 60% threshold: FutureOS
never drops *user* originals when compacting but caps the compacted history at
`MAX_EXPANDED_HISTORY = 128_000` (`agent/src/compaction/budget.rs`), and every wake
appends a ~1.5K-token **user** card (`prompts/turn.md` + `state/turn_payload.json`).
Once the preserved user cards plus the retained tail outgrow 128K, every run fails
with `context compaction budget exceeded … use a new session` — and each failed
attempt *still* appends its card, so `required` only grows
(129 639 → 131 186 → 132 733 → 134 991 over four wakes) and the session never
recovers by itself. So roll on three triggers: context percent, **awake count**,
and **a wedged last wake**.

  monitor_roll.py [--force] [--threshold-percent N]
        Prints the session id to wake (the old one unchanged, or a new one).
        Records the roll in journal/ so a roll is never silent.

Idempotent and safe: never rolls while the monitor is streaming (that would
abandon a live run), and keeps the old id if creating a new session fails.
"""
import json
import os
import re
import subprocess
import sys
import time

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUTURE = future_exe()
MONITOR_FILE = os.path.join(WATCH, "state", "monitor_session.txt")
SESSION_ID_FILE = os.path.join(WATCH, "state", "session_id.txt")
CFG_PATH = config_path()


def monitor_name():
    """What the supervisor session is called (`config.monitor_session_name`).

    One source of truth for the roll's create + archive names, and the same key
    `bin/decide.py` reads when it heals a name the desktop app's auto-title overwrote
    (`name_heal_needed`).
    """
    try:
        cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return cfg.get("monitor_session_name") or "会话总管"
JOURNAL = os.path.join(WATCH, "journal", time.strftime("%Y-%m-%d") + ".md")
DEFAULT_PERCENT = 60
# Wakes per session before rolling. Each wake costs ~1.5K tokens of user originals
# that compaction must preserve verbatim, and that budget is 128K (measured wedge:
# 103 wakes). 60 leaves ~1.5x headroom for the user's own, larger messages.
DEFAULT_MAX_QUERIES = 60
WEDGED_MARKERS = (
    # 2026-10-15: compacted history hit MAX_EXPANDED_HISTORY and every run failed
    # with the model's own "use a new session".
    "context compaction budget exceeded",
    # 2026-10-16: `future session compact` finished and left the session
    # unappendable ("run_snapshots" existed for its settled runs), so every later
    # turn — wake or the user typing in the desktop app — died with
    # `cannot append to a compacted settled run`. Nothing compacts away that
    # refusal, so this is a roll trigger too (and why compact-first is off).
    "cannot append to a compacted settled run",
)
# Compact in place before abandoning a long session (user, 2026-10-16: 「能不能不要开启
# 新会话，而是删除总管会话的历史部分，然后进行压缩」). Compaction is asynchronous, so the
# effect lands on the NEXT request; this is how long we wait before concluding it
# did not free enough and the session must roll after all.
COMPACT_SETTLE_SECONDS = 300
DEFAULT_COMPACT_FIRST = True
COMPACT_MARKER = os.path.join(WATCH, "state", "monitor_compacted.json")

# `future session reset` (2026-10-16). Preferred over rotating: the session keeps
# its identity, so the sidebar entry, the settings and the naming stay put.
RESET_MARKER = os.path.join(WATCH, "state", "monitor_reset.json")
DEFAULT_RESET_IN_PLACE = True
# User turns to keep. Every wake appends one ~1.8 KB **user** card, and user
# originals are exactly what compaction must preserve verbatim until the 128K
# budget overflows — so the point of the reset is to drop the accumulated cards.
# Two turns keep the thread of the conversation actually in progress.
DEFAULT_RESET_KEEP_LAST = 2
RESET_DROP_KINDS = "thinking,tool-calls,tool-results,attachments"
# The reset reports synchronously, but `contextTokens` only reflects it on a later
# request — the same "acknowledgement, not completion" gap as compaction. A reset
# recorded inside this window is not requested a second time.
RESET_SETTLE_SECONDS = 300
# A reset that genuinely cut the history shows up as a much lower reading the first
# time we measure afterwards. Without that evidence a settled marker means "the
# history did not shrink", i.e. resetting again would be pointless — that is the case
# the roll fallback is for. Measured 2026-10-16: the 17:37 reset took the session from
# ~30 % to 9.4 % (17:44 snapshot) and the context then climbed back over the next
# 3.8 h / 15 wakes of real work. The old rule read that regrowth as "the reset did not
# help" and replaced the session — which the user then asked about.
RESET_EFFECTIVE_RATIO = 0.75
# How long after a reset the "did it shrink?" reading is still meaningful (2026-10-18).
#
# The observation used to be taken whenever the caller happened to call
# `observe_reset_after()`, which was only on a threshold hit — i.e. by construction
# after the context had grown back. On 2026-10-18 a reset that demonstrably worked
# (11:05, 30.0%) was later judged by the 00:06 reading (32.5%, 13 hours of new work)
# and the session was rolled: the exact failure the note above says was fixed.
# So the reading is now taken on every wake, and only counts inside this window;
# outside it the verdict is "unknown" rather than "did not shrink".
RESET_AFTER_WINDOW_SECONDS = 1800
# Consecutive "unknown" verdicts allowed before a roll. One retry is cheap and
# preserves the identity the user asked for; a second means resetting is not
# fixing anything we can see, and rolling is the only move left.
RESET_UNKNOWN_RETRIES = 1
# The agent and the CLI are separate builds and are upgraded separately. This is
# the agent's answer when it predates the `reset_history` RPC (CLI PR #1139): the
# CLI parses the command, the agent has never heard of it.
AGENT_NOT_UPGRADED = "unknown command: reset_session_history"
# The agent refuses a reset while the session is busy, and marks it retryable
# (`agent/src/rpc/commands/session_reset.rs`: `session_busy`, `retryable: true`).
# That is a *positive* signal — it proves the agent implements the command — so the
# capability probe must read it as "supported", and the trigger must retry on the
# next wake rather than fall back to a new session.
BUSY_MARKERS = (
    "finish or stop the active run before resetting session history",
    "session context compaction is in progress",
    "session_busy",
)
# On-demand reset: ask for one without waiting for a threshold. A one-shot file
# naming the session is what makes this reachable from the wake path, where nothing
# can pass arguments.
RESET_REQUEST = os.path.join(WATCH, "state", "reset_requested.json")


def is_busy(err):
    return any(marker in (err or "") for marker in BUSY_MARKERS)


def read_reset_request(current):
    """The on-demand request naming `current`, or None (also for another session)."""
    try:
        with open(RESET_REQUEST, encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    wanted = raw
    try:  # a JSON body carries the id, so a caller can add fields later
        data = json.loads(raw)
        if isinstance(data, dict):
            wanted = str(data.get("session") or "")
    except ValueError:
        pass
    return raw if wanted == current else None


def clear_reset_request():
    try:
        os.remove(RESET_REQUEST)
    except OSError:
        pass


def log(msg):
    path = os.path.join(WATCH, "logs", "monitor_roll.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def future(args, timeout=60):
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run([FUTURE] + args, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def read_monitor():
    try:
        return open(MONITOR_FILE, encoding="utf-8").read().strip()
    except FileNotFoundError:
        return ""


def last_wake_wedged(current):
    """True when the most recent wake **of this same session** came back wedged.

    A refusal *before* the context-percent threshold (see the module docstring),
    so it has to be a roll trigger itself. Matching the session matters: the log
    of a wedged session we already rolled away from stays on disk, and it must not
    roll the *fresh* session every heartbeat afterwards. `wake.sh` writes
    `monitor=<id>` into every log.
    """
    logdir = os.path.join(WATCH, "logs")
    try:
        names = [n for n in os.listdir(logdir)
                 if n.startswith("wake-") and n.endswith(".log")]
    except OSError:
        return False
    if not names:
        return False
    newest = max(names, key=lambda n: os.path.getmtime(os.path.join(logdir, n)))
    path = os.path.join(logdir, newest)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    if not any(marker in text for marker in WEDGED_MARKERS):
        return False
    match = re.search(r"monitor=(\S+)", text)
    if match:
        return match.group(1) == current
    try:  # no `monitor=` line (log written by something else): only trust a fresh one
        return (time.time() - os.path.getmtime(path)) < 1800
    except OSError:
        return False


def metrics(session_id):
    rc, out, _ = future(["session", "status", session_id, "--json", "--metrics"])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


def append_journal(line):
    try:
        with open(JOURNAL, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def apply_manual(session_id):
    """Hand the operating manual to the session we just rolled onto.

    A new session has an empty system-prompt append, while `bin/turn_prompt.py`
    keeps sending the *minimal* card (its record matches the file on disk), so
    the manual would silently vanish for every wake after the roll — found
    2026-10-16, when a roll had already moved the id file. The apply script does
    the end-to-end canary and, on failure, clears the append *and* its record so
    the next card is full again: a failure costs time, not correctness. So this is
    best-effort by design — a roll must never fail because of it.
    """
    script = os.path.join(WATCH, "bin", "apply_prompt.py")
    if not os.path.exists(script):
        return
    try:
        p = subprocess.run([sys.executable, script, "--apply", "--session", session_id],
                           capture_output=True, text=True, timeout=600)
    except Exception as exc:  # noqa: BLE001
        log(f"append apply after roll could not run: {exc}")
        return
    log(f"append apply after roll -> rc={p.returncode} {(p.stdout or '').strip()[:200]}")


def read_compact_marker():
    try:
        with open(COMPACT_MARKER, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a missing/corrupt marker just means "not yet"
        return {}


def write_compact_marker(session_id):
    try:
        os.makedirs(os.path.dirname(COMPACT_MARKER), exist_ok=True)
        with open(COMPACT_MARKER, "w", encoding="utf-8") as fh:
            json.dump({"session": session_id, "at": time.time()}, fh)
    except OSError:
        pass


def compact_or_roll(current, pct, threshold):
    """Keep the session and compact its history, or admit that rolling is needed.

    Returns `("keep", "")` when the in-place compaction was requested, or
    `("roll", reason)` when the session must still be replaced.

    The id, its settings and its place in the sidebar all survive compaction —
    that is the whole point of not opening a new session. Because the request is
    asynchronous ("acknowledgement, not completion"), the effect can only be
    observed on a later wake: compact once per session, and if the context is
    *still* over the threshold after `COMPACT_SETTLE_SECONDS`, conclude that
    compaction did not buy enough and roll — so the fallback is exactly today's
    behaviour and never a wedged session. A failed request also rolls: silencing
    the trigger must not be the outcome of an error.
    """
    marker = read_compact_marker()
    same_session = marker.get("session") == current
    try:
        age = time.time() - float(marker.get("at") or 0)
    except (TypeError, ValueError):
        age = 0.0
    if same_session:
        # Compact once per session. Until the effect can be observed there is
        # nothing useful to do — and re-requesting it every wake would burn a
        # model call each time for the same non-answer.
        if age >= COMPACT_SETTLE_SECONDS:
            return "roll", (f"上下文 {pct:.1f}% ≥ {threshold:.0f}%"
                            f"（{int(age // 60)} 分钟前已原地压缩，未降下来）")
        return "keep", ""
    rc, _out, err = future(["session", "compact", "--session", current])
    if rc != 0:
        return "roll", (f"上下文 {pct:.1f}% ≥ {threshold:.0f}%"
                        f"（原地压缩请求失败 rc={rc}: {err.strip()[:120]}）")
    write_compact_marker(current)
    log(f"compacted {current} in place (context {pct:.1f}%)")
    return "keep", ""


def reset_marker(session_id):
    """The record of a reset performed here, or `{}` for a session we never reset.

    Carries the wake count at reset time so the wake-count trigger re-arms from
    the reset instead of from the session's birth: `queryCount` lives in the
    session *record*, which a reset never touches.
    """
    try:
        with open(RESET_MARKER, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 - missing/corrupt means "never reset here"
        return {}
    if not isinstance(data, dict) or data.get("session") != session_id:
        return {}
    return data


def write_reset_marker(session_id, queries, pct_before=None, unknown_tries=0):
    """Record a reset we just applied, and the reading that triggered it.

    `pct_before` is what makes the *next* threshold hit decidable: with it, and with
    the first reading measured inside the window afterwards (`after`, `after_at`),
    `reset_effective()` can tell a history that shrank (and later regrew through real
    work) from one that ignored the reset.

    `unknown_tries` carries the count of consecutive resets whose effect could not be
    judged, so a session that never yields a readable observation stops being reset
    forever and rolls once the allowance (`RESET_UNKNOWN_RETRIES`) is spent.
    """
    data = {"session": session_id, "at": time.time(), "queries": queries}
    if pct_before is not None:
        data["pct"] = float(pct_before)
    if unknown_tries:
        data["unknown_tries"] = int(unknown_tries)
    try:
        os.makedirs(os.path.dirname(RESET_MARKER), exist_ok=True)
        with open(RESET_MARKER, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def observe_reset_after(marker, pct_now, now=None):
    """Record the first reading taken while the reset's effect is still observable.

    Only a reading inside [`at + RESET_SETTLE_SECONDS`, `at +
    RESET_AFTER_WINDOW_SECONDS`] describes the reset: earlier and the agent has not
    re-run yet (the reading still shows the old history), later and it already
    includes new work. Outside that window we record nothing and say why, so the
    verdict becomes `unknown` instead of a false `ineffective`.

    Callers must invoke this on **every** wake, not only when a threshold fires
    (2026-10-18: see `RESET_AFTER_WINDOW_SECONDS`).
    """
    if not marker or "after" in marker or "pct" not in marker:
        return marker
    now = time.time() if now is None else float(now)
    try:
        at = float(marker.get("at") or 0)
    except (TypeError, ValueError):
        return marker
    age = now - at
    marker = dict(marker)
    if age < RESET_SETTLE_SECONDS:
        return marker
    if age > RESET_AFTER_WINDOW_SECONDS:
        if not marker.get("after_expired"):
            marker["after_expired"] = True
            save_reset_marker(marker)
        return marker
    marker["after"] = float(pct_now)
    marker["after_at"] = now
    save_reset_marker(marker)
    return marker


def save_reset_marker(marker):
    try:
        with open(RESET_MARKER, "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
    except OSError:
        pass


def reset_effective(marker):
    """`(verdict, why)` — did the recorded reset actually shrink the history?

    `verdict` is `"effective"` (the first reading inside the window is at least a
    quarter below the one that triggered the reset — so the history really did drop,
    and a later threshold hit is new accumulation that deserves another reset),
    `"ineffective"` (a reading inside the window shows the history did not shrink:
    the reset dropped nothing, because a floor of entries survived the selectors),
    or `"unknown"` when no trustworthy reading exists — no sample, a sample taken
    after the window (new work has been mixed in), or a legacy marker with no
    timestamp at all. `ineffective` and `unknown` are deliberately different: the
    first is evidence against resetting again, the second is only absence of
    evidence, and treating absence as evidence rolled a session that had been reset
    successfully (2026-10-18).
    """
    try:
        before = float(marker.get("pct"))
    except (TypeError, ValueError):
        return "unknown", "那次重置没有留下可比较的读数（旧版本写的标记）"
    if before <= 0:
        return "unknown", "触发重置时的读数不可信（0）"
    try:
        after = float(marker.get("after"))
        at = float(marker.get("at") or 0)
        after_at = float(marker.get("after_at"))
    except (TypeError, ValueError):
        if "after" in marker:
            return "unknown", ("重置后的那次读数没有时间戳，无法判断它是不是刷新后的读数"
                               "（旧版本标记；可能就是回涨之后采到的）")
        if marker.get("after_expired"):
            return "unknown", ("重置后没在观测窗口内取到读数，之后的变化已不可归因于重置")
        return "unknown", "还没有取到重置之后的读数"
    if after_at < at + RESET_SETTLE_SECONDS:
        return "unknown", "重置后立刻取的读数不算数（重置尚未生效）"
    if after_at > at + RESET_AFTER_WINDOW_SECONDS:
        minutes = int((after_at - at) // 60)
        return "unknown", (f"重置后 {minutes} 分钟才取到读数，已混入新的增长，"
                           "不能据此判断重置是否有效")
    if after < before * RESET_EFFECTIVE_RATIO:
        return "effective", f"重置确实把上下文从 {before:.1f}% 降到 {after:.1f}%"
    return "ineffective", f"重置后读数仍在 {after:.1f}%（触发时 {before:.1f}%），历史没有真正变小"


def reset_supported(session):
    """Whether `future session reset` can be applied to `session` RIGHT NOW.

    Two layers have to agree and they upgrade **separately**: the CLI (the binary
    you type) and the agent (the process holding the database). Probing only
    `--help` was a near-miss on 2026-10-16 17:10: the CLI was refreshed to a build
    with #1139 while the running agent was still the 16:30 build, so `--help` said
    "supported" and the apply would have failed with
    `unknown command: reset_session_history` — one wasted call per trigger, and a
    journal line blaming the reset instead of the stale agent.

    So: `--help` first (a CLI older than #1139 has no such subcommand at all), then
    a **dry run** against the real session — the default is "change nothing", which
    makes it a read-only way to ask the agent whether it implements `reset_history`.

    Returns `(supported, why-not)`; `why-not` is journal-shaped text.
    """
    rc, _out, _err = future(["session", "reset", "--help"], timeout=30)
    if rc != 0:
        return False, "本机 `future` 还没有 `session reset` 子命令（要重新安装二进制）"
    rc, _out, err = future(["session", "reset", session, "--keep-last", "1"], timeout=120)
    if rc == 0 or is_busy(err):
        # A busy refusal is the agent saying "I know this command, but not now".
        return True, ""
    if AGENT_NOT_UPGRADED in err:
        return False, ("`future` 已是新版，但 **agent 进程还是旧构建**"
                       "（重启 agent 后 `session reset` 才能用）")
    return False, f"重置能力探测失败 rc={rc}: {err.strip()[:120]}"


def reset_failure_reason(rc, err):
    """Why an applying reset failed, in words that name the fix."""
    if AGENT_NOT_UPGRADED in err:
        return ("`future` 已是新版，但 **agent 进程还是旧构建**"
                "（重启 agent 后 `session reset` 才能用）")
    if is_busy(err):
        return "会话正忙（有 run 或压缩在跑），下次唤醒再试"
    return f"原地重置失败 rc={rc}: {err.strip()[:140]}"


def reset_or_roll(current, reason, queries, marker, force_all=False, keep_last=0,
                  fresh_episode=False, pct=None):
    """Drop this session's history in place, or admit it has to be replaced.

    Returns `(verdict, why, state)` where verdict is `"keep"` after a successful
    reset (or while an earlier one is still settling) or `"roll"` when the caller
    must open a new session. `state` is `"applied"`, `"settling"`, `"busy"` or
    `"rolled"` — the caller needs it to decide whether an on-demand request has
    been served (a busy session must keep its request for the next wake).

    `fresh_episode` says the trigger is a new accumulation rather than a re-check
    of the one an earlier reset was meant to fix: only then does a settled marker
    allow another reset (the wake-count trigger re-arms that way). For a re-check —
    high context, or a session still wedged — a settled marker means "that did not
    help", so the caller rolls instead of resetting in a loop.
    """
    next_unknown_tries = 0
    if marker and not fresh_episode:
        try:
            age = time.time() - float(marker.get("at") or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age < RESET_SETTLE_SECONDS:
            # Requested moments ago; the reading cannot have moved yet.
            return "keep", "", "settling"
        verdict, why = reset_effective(marker)
        if verdict == "effective":
            # The history really did shrink; this threshold hit is new accumulation,
            # which is exactly what an in-place reset is for. Resetting again keeps the
            # identity — nothing is renamed, and nothing has to be re-found in the
            # sidebar (the 21:27 roll cost the user exactly that).
            fresh_episode = True
        elif verdict == "ineffective":
            return "roll", (f"{reason}（{int(age // 60)} 分钟前已原地重置历史，"
                             f"{why}）"), "rolled"
        else:
            # No trustworthy reading. Resetting costs no model call and keeps the
            # identity the user asked to keep (「用这个 reset」), while rolling is the
            # outcome they complained about — so retry once, then stop guessing.
            tries = int(marker.get("unknown_tries") or 0)
            if tries >= RESET_UNKNOWN_RETRIES:
                return "roll", (f"{reason}（{int(age // 60)} 分钟前已原地重置历史，{why}；"
                                 f"连续 {tries + 1} 次都无法判定，改为换新会话）"), "rolled"
            next_unknown_tries = tries + 1
            fresh_episode = True
    supported, why_not = reset_supported(current)
    if not supported:
        return "roll", f"{reason}（{why_not}）", "rolled"
    selectors = [["--keep-last", str(keep_last), "--drop", RESET_DROP_KINDS]]
    if force_all:
        # A wedged session may refuse to be written even after a slimming reset
        # (incident-6's `cannot append to a compacted settled run`). Dropping every
        # entry is still in place — the identity and its settings are untouched.
        selectors.append(["--all"])
    rc, _out, err = 0, "", ""
    busy = False
    for selector in selectors:
        rc, _out, err = future(["session", "reset", current] + selector
                               + ["--yes", "--json"], timeout=600)
        if rc == 0:
            write_reset_marker(current, queries, pct_before=pct,
                               unknown_tries=(next_unknown_tries
                                              if marker.get("session") == current else 0))
            log(f"reset {current} in place ({reason}) selectors={' '.join(selector)}")
            return "keep", "", "applied"
        if is_busy(err):
            # The agent says retryable. Retrying costs one call next wake; rolling
            # would throw away the session identity the reset exists to keep.
            busy = True
    if busy:
        log(f"reset of {current} postponed: session busy ({reason})")
        return "keep", "", "busy"
    return "roll", f"{reason}（{reset_failure_reason(rc, err)}）", "rolled"


def main(argv):
    cfg = {}
    try:
        cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    threshold = float(cfg.get("monitor_roll_context_percent", DEFAULT_PERCENT))
    if "--threshold-percent" in argv:
        threshold = float(argv[argv.index("--threshold-percent") + 1])
    max_queries = int(cfg.get("monitor_roll_max_queries", DEFAULT_MAX_QUERIES))
    compact_first = bool(cfg.get("monitor_compact_first", DEFAULT_COMPACT_FIRST))
    force = "--force" in argv

    current = read_monitor()
    if not current:
        log("no monitor session registered; nothing to roll")
        print("")
        return 1

    m = metrics(current)
    if m is None:
        log(f"cannot read metrics for {current}; keeping it")
        print(current)
        return 0

    pct = float(m.get("contextPercent") or 0)
    streaming = bool(m.get("isStreaming"))
    queries = int(m.get("queryCount") or 0)
    reset_in_place = bool(cfg.get("monitor_reset_in_place", DEFAULT_RESET_IN_PLACE))
    keep_last = int(cfg.get("monitor_reset_keep_last", DEFAULT_RESET_KEEP_LAST))
    marker = reset_marker(current)
    reason = ""
    wedged = False
    fresh_episode = bool(force)
    requested = None if force else read_reset_request(current)
    if not force:
        if streaming:
            log(f"monitor {current} streaming; not rolling (live run)")
            print(current)
            return 0
        # Sample the post-reset reading on EVERY wake inside the window, not only
        # when a threshold fires (2026-10-18): sampling only at a threshold hit
        # guarantees the sample is taken after the context has grown back, which
        # scored a reset that worked as "did not shrink" and rolled the session.
        if marker:
            marker = observe_reset_after(marker, pct)
        if requested is not None:
            # On-demand: act on it before any threshold, and treat it as a fresh
            # episode (a settled marker from an earlier reset must not veto it).
            reason = "按请求原地重置（state/reset_requested.json）"
            fresh_episode = True
        elif pct >= threshold:
            reason = f"上下文 {pct:.1f}% ≥ {threshold:.0f}%"
        elif queries >= max_queries and queries >= int(marker.get("queries") or 0) + max_queries:
            # Counted from the last in-place reset: the trigger is about the user
            # originals accumulated in the *history*, and a reset just dropped them.
            reason = (f"已唤醒 {queries} 次（自上次原地重置起又满 {max_queries} 次；"
                      f"用户原文累计逼近压缩预算 128K）")
            fresh_episode = True
        elif last_wake_wedged(current):
            wedged = True
            reason = "上一次唤醒返回「会话已卡死」（无法再追加事件，不能自行恢复）"
        if not reason:
            print(current)
            return 0

    if reset_in_place:
        # Reset beats compaction here: compaction must keep user originals
        # verbatim, and those originals are exactly what overflows the budget.
        # The first reading after a reset is the observation that decides whether a
        # later threshold hit means "the reset failed" (roll) or "new work" (reset
        # again). It is recorded on every non-streaming wake above, so by the time a
        # threshold fires the marker already carries a reading taken while the reset
        # was still observable — or none, which `reset_effective()` reports as
        # unknown instead of misreading regrowth as failure.
        marker = observe_reset_after(marker, pct)
        verdict, why, state = reset_or_roll(current, reason or "强制", queries, marker,
                                            force_all=(wedged or force),
                                            keep_last=keep_last,
                                            fresh_episode=fresh_episode,
                                            pct=pct)
        if requested is not None and state != "busy":
            # Served (applied), or moot (already settling / rolled away). Only a
            # busy refusal keeps the request, so the next wake retries it.
            clear_reset_request()
        if verdict == "keep":
            if reason:
                append_journal(
                    f"- {time.strftime('%H:%M')} **原地重置总管会话历史**（{reason}）："
                    f"`future session reset {current} --keep-last {keep_last} "
                    f"--drop {RESET_DROP_KINDS} --yes`；id／名字／设置／侧栏位置不变，"
                    f"历史里的卡片与工具输出已丢弃（Agent 侧无损备份在 `~/.future/agent/session-backups/`）。\n")
            print(current)
            return 0
        reason = why
    elif compact_first and pct >= threshold:
        # Legacy path, kept for `monitor_reset_in_place: false`: a long history is
        # the one trigger compaction can address in place.
        verdict, why = compact_or_roll(current, pct, threshold)
        if verdict == "keep":
            append_journal(
                f"- {time.strftime('%H:%M')} **原地压缩总管会话**（上下文 {pct:.1f}% ≥ "
                f"{threshold:.0f}%）：不新建会话，对 `{current}` 请求 `future session compact`；"
                f"下一次唤醒看是否降到阈值以下。\n")
            print(current)
            return 0
        reason = why

    rc, out, err = future(["session", "new", "--cwd", WATCH, "--name",
                           monitor_name(), "--json"])
    if rc != 0:
        log(f"failed to create replacement session rc={rc} err={err.strip()[:200]}; keeping {current}")
        print(current)
        return 0
    try:
        new_id = json.loads(out)["sessionId"]
    except Exception:  # noqa: BLE001
        log(f"replacement session reply unparseable: {out[:200]}; keeping {current}")
        print(current)
        return 0

    stamp = time.strftime("%Y-%m-%d %H:%M")
    # Archive the old one so the sidebar does not show two "会话总管".
    future(["session", "set", current, "--title", f"{monitor_name()} · 已归档 {stamp}"])

    with open(MONITOR_FILE, "w", encoding="utf-8") as fh:
        fh.write(new_id + "\n")
    try:
        with open(SESSION_ID_FILE, "w", encoding="utf-8") as fh:
            fh.write(new_id + "\n")
    except OSError:
        pass
    apply_manual(new_id)

    line = (f"- {time.strftime('%H:%M')} **轮换总管会话**（{reason or '强制'}）："
            f"旧 `{current}` → 新 `{new_id}`；旧会话改名为「会话总管 · 已归档 {stamp}」保留。"
            f"持久记忆在磁盘（journal/ reports/ .future/memory/）+ 仓库的 docs/，新会话按 turn.md 先读那些。\n")
    append_journal(line)
    log(f"rolled {current} (context {pct:.1f}%, wakes {queries}) "
        f"reason={reason or 'forced'} -> {new_id}")
    print(new_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))