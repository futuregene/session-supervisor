#!/usr/bin/env python3
"""Write state/SNAPSHOT.md — the operational state that must survive a roll.

Why: the supervisor's *chat history* is not a reliable store. It grows without
bound (see docs/runbooks/limits.md) and gets rolled to a fresh session at a
threshold (bin/monitor_roll.py). What must survive that is the operational state.

Where it lives is decided by one test — *would another machine need it?* No: it
records this box's session ids, wake counters and gate values. So it stays **on
disk and out of git** (the whole of `state/` does), and a fresh turn reads the
file on disk rather than a committed copy. Durability across a roll comes from
the file being rewritten every turn, not from version history.

What a turn reads, and who writes each:

    state/SNAPSHOT.md          <- this script: what a fresh turn needs to orient
    state/state.json           <- bin/scan.py: event de-dup + per-session baseline
    journal/ reports/          <- the turn + the record (also on disk, also untracked)
    AGENTS.md docs/ POLICY.md  <- the framework, in git

Run it: at the end of a turn (cheap, no model calls), and before a roll.
Read it: as the FIRST step of a turn — it is ~1-2 KB and answers "where am I".
"""
import json
import os
import subprocess
import sys
import time

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUTURE = future_exe()
SNAP = os.path.join(WATCH, "state", "SNAPSHOT.md")


def read_jsonl(path, n=None):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        pass
    except FileNotFoundError:
        pass
    return rows[-n:] if n else rows


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def metrics(session_id):
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run([FUTURE, "session", "status", session_id, "--json", "--metrics"],
                           capture_output=True, text=True, timeout=45, env=env)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def journal_tail(n=4):
    path = os.path.join(WATCH, "journal", time.strftime("%Y-%m-%d") + ".md")
    try:
        lines = [l.rstrip() for l in open(path, encoding="utf-8") if l.strip()]
        return lines[1:][-n:]  # skip the "# date" header
    except FileNotFoundError:
        return []


def open_needs_you():
    """Count sections in needs-you.md that are not marked as resolved."""
    path = os.path.join(WATCH, "reports", "needs-you.md")
    try:
        txt = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        return 0
    return sum(1 for l in txt.splitlines() if l.startswith("## ") and "已" not in l[:40])


def main():
    mon = ""
    try:
        mon = open(os.path.join(WATCH, "state", "monitor_session.txt"), encoding="utf-8").read().strip()
    except FileNotFoundError:
        pass
    m = metrics(mon) if mon else None
    health = load(os.path.join(WATCH, "state", "health.json"), {}) or {}
    pending = read_jsonl(os.path.join(WATCH, "inbox", "pending.jsonl"))
    wakes = read_jsonl(os.path.join(WATCH, "state", "wakes.jsonl"), 5)
    sent = [r for r in read_jsonl(os.path.join(WATCH, "state", "sent.jsonl"))
            if float(r.get("ts") or 0) > time.time() - 86400]

    out = []
    out.append("# 会话总管 · 状态快照（机器写，勿手改）")
    out.append(f"\n> 由 `bin/state_snapshot.py` 生成于 {time.strftime('%F %T')}；"
               f"任何回合**先读这一份**（约 1–2 KB）再决定要不要深读。")
    out.append("\n## 我是谁")
    out.append(f"- 总管会话：`{mon or '(未注册)'}`")
    if m:
        out.append(f"- 上下文：{m.get('contextTokens')} / {m.get('contextWindow')} "
                   f"= **{m.get('contextPercent'):.1f}%**（超阈值会轮换，见 `bin/monitor_roll.py`）")
        out.append(f"- 模型 `{m.get('model')}` · 权限 `{m.get('permissionLevel')}` · "
                   f"queryCount {m.get('queryCount')} · streaming={m.get('isStreaming')}")
    out.append(f"- 扫描健康：ok={health.get('ok')} ts={time.strftime('%H:%M:%S', time.localtime(health.get('ts', 0)))} "
               f"resync={health.get('resync')} bulk={health.get('bulk')} "
               f"sessions={health.get('sessionsTotal')} events={health.get('events')}")
    if health.get("degraded"):
        out.append(f"- **⚠ 扫描降级**：{health.get('degraded')}")

    out.append("\n## 待处理")
    if pending:
        out.append(f"- **{len(pending)} 条**待处理事件（全文：`python3 bin/inbox.py list --json --limit N`）")
        for e in pending[-8:]:
            out.append(f"  - `{e.get('kind')}`/{e.get('priority')} {e.get('sessionId','')[:22]} "
                       f"{e.get('summary','')[:52]}")
    else:
        out.append("- 无")

    out.append("\n## 最近动作")
    for w in wakes:
        out.append(f"- 唤醒 {time.strftime('%H:%M:%S', time.localtime(w.get('ts', 0)))} "
                   f"{w.get('reason')} pending={w.get('pending')}")
    if sent:
        out.append(f"- 过去 24h 向 {len({r.get('sessionId') for r in sent})} 个会话发过 {len(sent)} 条")
        for r in sent[-5:]:
            out.append(f"  - {time.strftime('%H:%M', time.localtime(float(r.get('ts') or 0)))} "
                       f"{str(r.get('sessionId'))[:22]} — {str(r.get('note') or '')[:60]}")
    else:
        out.append("- 过去 24h 未向任何会话发消息")

    out.append("\n## 等你拍板")
    out.append(f"- `reports/needs-you.md` 里约 **{open_needs_you()}** 条待决（该文件是用户收件箱）")

    tail = journal_tail()
    if tail:
        out.append("\n## 今日日志尾部（完整见 `journal/`）")
        for l in tail:
            out.append(f"- {l.lstrip('- ')[:150]}")

    with open(SNAP, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"wrote {SNAP} ({len(open(SNAP, encoding='utf-8').read().encode())} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())