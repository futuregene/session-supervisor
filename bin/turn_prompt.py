#!/usr/bin/env python3
"""Compose the wake message: minimal card, or full manual + card.

Why this exists
---------------
The wake card is a **user** message, and FutureOS never discards user text when
it compacts history. So every card stays in the context at full size forever:
measured 2026-10-16, this supervisor's 110k context was ~57 accumulated cards
(~1.9k tokens each), and a run's uncached input ≈ its context size (~¥1/M), i.e.
the card was the single biggest driver of the ≈¥58/day bill.

The stable part of the card does not have to be in history at all: the agent
rebuilds the system prompt at every run start, so text appended to the session's
system prompt (`session set --append-system-prompt`, applied per run by
`agent/src/rpc/session_prompt.rs`) is re-sent by the provider's prompt cache but
never accumulates as history.

The switch is gated on evidence, not on intent: the manual is only dropped from
the message once `bin/apply_prompt.py` has *canary-verified* that the installed
binary really puts the append in front of the model, and the file it verified is
byte-identical to `prompts/append.md` today. Otherwise this composes the full
message, exactly as before — the fallback is the old behaviour, not a degraded
one.

Usage: python3 bin/turn_prompt.py [--mode]   (prints the message; --mode prints
just `minimal`/`full` for scripts and tests)
"""
import hashlib
import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import utf8_stdout  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPEND = os.path.join(WATCH, "prompts", "append.md")
CARD = os.path.join(WATCH, "prompts", "turn.md")
APPLIED = os.path.join(WATCH, "state", "append_applied.json")


def _active_directives(watch=WATCH):
    """One-shot directives not yet expired (POLICY §A.12); rendered into the card tail."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # this script lives in bin/
        import directives as _d
        rows = _d.active()
    except Exception:
        return []
    # The scope MUST be stated, not implied by a bare id (2026-10-17): a directive
    # the user gave *in one session* was rendered as a flat bullet, so it read as a
    # standing rule and was applied to a different session's work — the coverage
    # campaign's PR was flagged as a violation of an instruction that never covered
    # it. Say "仅限该会话" or "全局" in words; the id alone is not a scope.
    out = []
    for r in rows:
        sid = r.get("session") or ""
        if sid and sid not in ("-", "global", "*"):
            scope = f"**作用域：仅限会话 `{sid}`**（不约束别的会话）"
        else:
            scope = "**作用域：全局**"
        out.append(f"- {scope}｜{r.get('text','')}（{r.get('expires','?')[:16]} 到期自动失效）")
    return out


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manual_digest(watch=WATCH):
    """Digest of `prompts/append.md`, computed exactly as `bin/apply_prompt.py` records it.

    Both sides must hash the *same* text: apply_prompt records
    `sha256_text(fh.read().strip())`. This used to hash the raw bytes instead,
    which can never match a file ending in a newline — so `mode` stayed `full`
    forever while `apply_prompt --check` cheerfully reported `mode=minimal`.
    A switch that can never flip is worse than no switch: it silently keeps the
    cost it was built to remove (2026-10-16).
    """
    try:
        with open(os.path.join(watch, "prompts", "append.md"), encoding="utf-8") as fh:
            return sha256_text(fh.read().strip())
    except OSError:
        return ""


def mode(watch=WATCH):
    """`minimal` only when a canary-verified append matches the file on disk."""
    try:
        with open(os.path.join(watch, "state", "append_applied.json"), "r", encoding="utf-8") as fh:
            applied = json.load(fh)
    except (OSError, ValueError):
        return "full", "no state/append_applied.json"
    if not applied.get("canary_ok"):
        return "full", "append was never canary-verified"
    want = manual_digest(watch)
    if not want:
        return "full", "prompts/append.md is missing"
    if applied.get("sha256") != want:
        return "full", "prompts/append.md changed since it was applied"
    return "minimal", f"verified {applied.get('verifiedAt', '?')} on {applied.get('binary', '?')}"


def one_line_per_session(events):
    """Pair every triage event with the ids it supersedes: one line per (session, kind).

    `bin/decide.py` already folds superseded events (`inbox.fold_superseded`), so on a
    normal wake every group has exactly one member; the rule is repeated here because
    the renderer is the last place that can guarantee the reader never sees one session
    three times (measured 2026-10-16 21:47: 6 events, 3 sessions, 「蓝色空提问气泡排查」
    appearing at 21:17 / 21:36 / 21:45). The superseded ids are returned *with* the
    survivor rather than dropped: they are ack handles, and an event whose id never
    appears again would never be acked — it would keep counting in `pendingCount` and
    keep riding the heartbeat forever.
    """
    groups = {}
    for i, ev in enumerate(events):
        sid, kind = ev.get("sessionId") or "", ev.get("kind") or ""
        key = (sid, kind) if (sid and kind) else ("", f"#{i}")
        groups.setdefault(key, []).append(ev)
    pairs = []
    for group in groups.values():
        # `detectedAtMs` is not in TRIAGE_FIELDS, so in a real payload every member is
        # 0 and the last one listed (append order = chronological) wins.
        newest = max(range(len(group)), key=lambda j: (int(group[j].get("detectedAtMs") or 0), j))
        older = [e.get("id") for j, e in enumerate(group) if j != newest and e.get("id")]
        pairs.append((group[newest], older))
    return pairs


def triage(watch=WATCH):
    """Render `state/turn_payload.json` as short markdown, in the message itself.

    Why not attach the file: `wake.sh` used to pass `@state/turn_payload.json`, which
    the target renders as a raw `<file name=…>{…}</file>` JSON blob — unreadable in
    Markdown and full of fields nobody acts on (monitor session id, epoch ms,
    baseline/resync/bulk, per-event `at`). The user asked for it to be readable and
    trimmed (2026-10-16). The event id is the one field that must survive: every
    `bin/` tool takes it.
    """
    try:
        with open(os.path.join(watch, "state", "turn_payload.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return ""  # no payload this turn: the card alone must still make sense

    events = payload.get("pendingEvents") or []
    health = payload.get("scannerHealth") or {}
    total = payload.get("pendingCount", len(events))
    hot = payload.get("pendingHighMedium", 0)
    out = [f"## 待处理事件（{total} 条 · 高/中 {hot}）", ""]
    if not events:
        out.append("无——空闲心跳，按手册收尾即可。")
    for event, older in one_line_per_session(events):
        prio = event.get("priority") or "low"
        mark = "" if prio == "low" else f" · **{prio}**"
        line = (f"- `{event.get('id', '?')}` **{event.get('kind', '?')}**{mark}"
                f" · {event.get('sessionName', '?')} · `{event.get('sessionId', '')}`"
                f" — {event.get('summary', '')}")
        if older:
            line += (f"（同会话更早 {len(older)} 条同类已折叠："
                     + "、".join(f"`{i}`" for i in older) + "）")
        out.append(line)
    out.append("")
    wakes = (f"**唤醒** {payload.get('wakeReason', '?')}"
             f" ｜ 1h {payload.get('wakesLastHour', '?')} 次 · 24h {payload.get('wakesLastDay', '?')} 次")
    bits = [f"**扫描器** {'ok' if health.get('ok') else '**异常**'}"]
    if health.get("sessionsTotal") is not None:
        bits.append(f"会话 {health.get('sessionsTotal')}（活跃 {health.get('active', '?')}）")
    if health.get("degraded"):
        bits.append(f"降级：{health['degraded']}")
    out.append("> " + wakes)
    out.append("> " + " · ".join(bits))
    return "\n".join(out).rstrip() + "\n"


def _pending_sends(watch=WATCH):
    """Relays that were refused because the target was mid-run (2026-10-17).

    `send.py` exits without side effects when the target is streaming; the message
    then lives only in a file nobody reads, so it silently never arrives. This
    renders those queued rows into the card so the next wake actually retries
    them. Row shape: {"ts","session","file","note","expires"}.

    A row whose body file is **gone** is reported, not skipped. Skipping it used to
    look harmless ("nothing to render") but it is the same silent loss A21 is about:
    the row survives, no card ever shows it, and the message is lost with the file.
    It happened (2026-10-18): a queued refusal's body was cleaned up at turn end, so
    "keep PR #1175 open until the gate is green" never reached the session, which
    then merged. The card now says the body is lost so the operator re-composes it.
    """
    path = os.path.join(watch, "state", "deferred-sends.jsonl")
    if not os.path.exists(path):
        return []
    now = datetime.datetime.now().astimezone()
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except Exception:
        return []
    for r in rows:
        exp = r.get("expires") or ""
        try:
            if exp and datetime.datetime.fromisoformat(exp) < now:
                continue
        except Exception:
            pass
        f = r.get("file") or ""
        resolved = f if os.path.isabs(f) else os.path.join(watch, f)
        if not f or not os.path.exists(resolved):
            # Loud, not silent: see the docstring. The row is real work whose body
            # was lost, so the card must ask for it back instead of hiding it.
            out.append(f"- ⚠️ **正文已丢失** `{r.get('session','?')}` ← `{f or '(空)'}`："
                       f"{r.get('note','') or '(无主题)'} —— 重新写一份正文再发；"
                       "这一行对应的消息**从未送达**（不是「已发出」）")
            continue
        out.append(f"- `{r.get('session','?')}` ← `{f}`：{r.get('note','')}")
    return out


def compose(watch=WATCH):
    kind, _why = mode(watch)
    with open(os.path.join(watch, "prompts", "turn.md"), "r", encoding="utf-8") as fh:
        card = fh.read().rstrip()
    pending = _pending_sends(watch)
    if pending:
        card += ("\n\n## 待补发（上次因对方 mid-run 被拒，**本次先试发**；"
                 "`python3 bin/send.py <session> <file> --note \"…\"` 后删掉对应行）\n"
                 + "\n".join(pending))
    directives = _active_directives(watch)
    if directives:
        card += "\n\n## 用户一次性指令（48h 后自动失效）\n" + "\n".join(directives)
    header = f"<!-- supervisor-card mode={kind} -->\n"
    if kind == "minimal":
        body = header + card
    else:
        try:
            with open(os.path.join(watch, "prompts", "append.md"), "r", encoding="utf-8") as fh:
                manual = fh.read()
        except OSError:
            return header + card  # nothing to prepend: never send a card-less message
        body = header + manual.rstrip() + "\n\n---\n\n" + card
    extra = triage(watch)
    return body + ("\n\n" + extra if extra else "")


def main(argv):
    # stdout must be UTF-8 bytes: `wake.sh` captures them with `$(...)` and passes
    # them as an argument, where Git Bash decodes them as UTF-8. Without this the
    # whole card is delivered as mojibake on Windows (2026-10-20).
    utf8_stdout()
    kind, why = mode()
    if "--mode" in argv:
        print(kind)
        return 0
    if "--why" in argv:
        print(f"{kind}: {why}")
        return 0
    sys.stdout.write(compose())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
