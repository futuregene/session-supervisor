#!/usr/bin/env python3
"""Speak up when the scanner itself has been failing — the supervisor's blind spot.

The wake path is `scan.py` → `decide.py` → wake. `scan.py` needs the `future` CLI.
When the CLI breaks, scanning fails, no events are produced, and **nothing wakes the
supervisor**: it stays up, keeps looping, and goes blind silently. Real case
(2026-10-26): `~/.future/bin/future` was replaced by a newer local build while the
long-running `future host` kept serving the old image, so every `future session list`
died with "Incompatible Future Agent … does not match client …"; 40 minutes of
silence passed before the user noticed and asked why.

`bin/liveness.py` can detect this, but nothing called it from the loop — this does,
right after `scan.py` in `watchd.sh`.

Two shapes of silence are covered, because only one of them is an error:

  1. **连续失败**：`health.json` 报 `ok=false`，`SPEAK_AFTER` 次后说一次；
  2. **在跑但不推进**：`health.json` 仍是 `ok=true`（它不会自己变旧）、`watchd.pid`
     的进程也活着，但 `state/state.json` 的 `lastScanTs` 停在十个扫描周期之前。
     一次“合法截断”的 `main()` 就是这样让扫描器空转 18.4 小时的（docs/gotcha-scan.md B15）。

Behaviour: after `SPEAK_AFTER` consecutive failing scans with the *same* error,
announce once (a notice for the daily briefing + a rewritten `reports/blind.md`),
then stay quiet until either the error changes or health recovers. Prints one line
per transition only, so `watchd.sh` can append it to `logs/scan.log` every cycle
without noise.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# `pid_alive` lives in one place because the obvious POSIX probe (`os.kill(pid, 0)`)
# *terminates* the process on Windows — liveness must never be written twice.
import liveness  # noqa: E402
STATE = os.path.join(WATCH, "state")
HEALTH = os.path.join(STATE, "health.json")
BLIND_STATE = os.path.join(STATE, "blind.json")
REPORT = os.path.join(WATCH, "reports", "blind.md")
NOTICE = os.path.join(WATCH, "bin", "notice.py")
CONFIG = os.path.join(WATCH, "bin", "config.json")

# 扫描器“在跑”与“在推进”是两个事实。`state/watchd.pid` 说循环还活着，
# `state/health.json` 的 `ok` 只说明*最后一次跑完*的扫描成功——它不会自己变旧
# （`have_health()` 从不看 `ts`）。唯一证明“又走完了一轮”的是 `scan.py` 收尾写的
# `state/state.json: lastScanTs`。2026-11 的一次合法函数截断让每轮都在写 `lastScanTs`
# 之前退出、退出码 0，于是这两条既有信号全部判健康，扫描器空转 18.4 小时
# （docs/gotcha-scan.md B15）。所以再加一条：状态在跑、却不再推进。
SCAN_STATE = os.path.join(STATE, "state.json")
PIDFILE = os.path.join(STATE, "watchd.pid")
STALL_FACTOR = 10          # 十个扫描周期没有推进 = 问题，不是抖动
STALL_SIG = "progress-stalled"

# Two failing scans (≈60 s at the 30 s interval): a single miss is a blip, two in a
# row is a broken CLI — and being fast matters, because silence is the failure mode.
SPEAK_AFTER = 2


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _text(path) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 - missing reads as unknown
        return ""


def scan_interval() -> float:
    try:
        return float((load(CONFIG, {}) or {}).get("scan_interval_seconds") or 30)
    except (TypeError, ValueError):
        return 30.0


def progress_stall(now=None) -> tuple[str, str]:
    """(message, stable signature) when the scanner runs but stops advancing.

    Both halves are needed: the message carries the fact (where `lastScanTs` stopped,
    how long ago), the signature must be *stable* across cycles — `main()` dedups on
    it, and a message that re-words itself every 33 s would look like a brand-new fault
    each time and never reach `SPEAK_AFTER`.
    """
    now = now if now is not None else time.time()
    st = load(SCAN_STATE, {}) or {}
    try:
        last = float(st.get("lastScanTs") or 0)
    except (TypeError, ValueError):
        last = 0.0
    pid = (_text(PIDFILE) or "").strip()
    if not last or not pid or not liveness.pid_alive(pid):
        # No baseline / no watcher / a dead watcher is the other check's business:
        # this criterion is only about "the loop is up and it is not progressing".
        return "", ""
    gap = now - last
    if gap <= STALL_FACTOR * scan_interval():
        return "", ""
    return (f"扫描在跑但没有推进（lastScanTs 停在 {_ts(last)}，已 {int(gap // 60)} 分钟）",
            f"{STALL_SIG}@{int(last)}")


def have_health() -> tuple[bool, str, float]:
    health = load(HEALTH, None)
    if not isinstance(health, dict):
        return False, "state/health.json is missing or unreadable", 0.0
    ts = float(health.get("ts") or 0.0)
    if not health.get("ok"):
        err = (health.get("error") or "ok=false").strip()
        # Last line, not first: the CLI's `[dns] plan=…` preamble is on every call,
        # so quoting `detail[0]` reported the preamble as the fault (see
        # `scan.err_line`). `error_line` is the scan side's answer when it has one.
        line = (health.get("error_line") or "").strip()
        if not line:
            detail = [l.strip() for l in (health.get("stderr") or "").splitlines() if l.strip()]
            line = detail[-1] if detail else ""
        if line:
            err = f"{err}: {line[:300]}"
        return False, err, ts
    return True, "", ts


def notice(text: str) -> None:
    """Best effort: `bin/notice.py` is local-only, so it works while the CLI is dead."""
    try:
        subprocess.run(
            [sys.executable, NOTICE, "add", "--kind", "other", "--text", text],
            check=False, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def write_report(state: dict, healthy: bool) -> None:
    lines = [
        "# 扫描器健康（自动写，勿手改）",
        "",
        "这份文件只在扫描器**连续失败**时出现内容，用来回答“总管为什么没动静”。",
        "",
    ]
    if not state.get("since"):
        lines += ["当前：**正常**。" if healthy else "当前：尚无健康记录。", ""]
    elif healthy:
        lines += [
            f"当前：**已恢复**（最后一次失败 {_ts(state.get('last'))}，"
            f"此前连续失败 {state.get('count', 0)} 次）。",
            "",
            f"最近一次故障：`{state.get('error', '')}`",
            "",
        ]
    else:
        what = ("扫描在跑但没有推进" if "没有推进" in (state.get('error') or "")
                else "扫描器在失败")
        lines += [
            f"当前：**{what}**，自 {_ts(state.get('since'))} 起连续 {state.get('count', 0)} 次。",
            "",
            f"最后一次错误：`{state.get('error', '')}`",
            "",
            "影响：事件不会被扫描进来，我不会被唤醒——也就是说**我看不见任何会话**，",
            "直到扫描恢复。已在 `logs/scan.log` 打了 WARN。",
            "",
            f"对症的修法：{_diagnose(state.get('error', ''))}",
            "",
        ]
    tmp = REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, REPORT)


def _diagnose(error: str) -> str:
    """The remedy for *this* failure, not the most common one overall.

    Until 2026-11-05 both the notice and `reports/blind.md` printed one fixed guess
    ("新构建 + host 跑旧镜像 → 重启 host") for every fault. While the scanner was
    down for six hours the real error was `unable to connect to Future Agent
    (unix://…/agent.sock)`, so the advice sent the reader to a host that had not
    restarted in 5 h. Reading the error first costs one `if` and saves that walk.
    """
    low = (error or "").lower()
    if "agent.sock" in low or "unable to connect to future agent" in low:
        return ("agent 连不上（`~/.future/run/agent.sock`）——agent 进程没在跑，"
                "或刚被停掉/重启。启动 FutureOS App（或 agent）后自愈即可；"
                "这与 `future host`（远程桌面）无关，不用重启它。")
    if any(k in low for k in ("version", "handshake", "握手", "mismatch")):
        return ("`~/.future/bin/future` 与正在跑的 host 版本不一致（握手失败）——"
                "重启 host 即恢复。")
    if "dns" in low:
        return ("错误里只剩 CLI 的 DNS 预检行 —— 真正的原因被压在它下面："
                "先看 `logs/scan.log` 该时刻的完整几行。")
    if "没有推进" in (error or ""):
        return ("扫描进程活着但一轮也没跑完：先在 `logs/scan.log` 找上一轮的"
                "`[watchd] scan produced no summary (exit=N)`，再跑一次"
                "`python3 bin/scan.py` 看输出末尾有没有 `sessions=…`。"
                "最常见的原因是 `main()` 被提前结束——`python3 bin/selftest_scan_structure.py`"
                "会直接报出是哪几条断言没了；`state/state.json` 与 `state/health.json` 的 mtime"
                "也必须前进。别用“日志里还有新行”当健康证据。")
    return "原因未知：先看 `logs/scan.log` 该时刻的完整几行，别照抄通用建议。"


def _ts(sec) -> str:
    if not sec:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(sec)))


def main() -> int:
    healthy, error, _ = have_health()
    prev = load(BLIND_STATE, {}) or {}
    now = time.time()

    # “在跑但不推进”是第三种状态：CLI 没报错（health.json 仍是 ok），循环也活着
    # （watchd.pid 里的进程在），但一个扫描周期都没跑完。它比连续失败更难被发现，
    # 因为每一条既有信号都说健康。
    stall, stall_sig = progress_stall(now)
    if stall and healthy:
        healthy, error = False, stall
    sig = stall_sig if stall else error

    if healthy:
        if prev.get("since"):
            write_report(prev, healthy=True)
            print(f"{time.strftime('%F %T')} [blind] recovered after "
                  f"{prev.get('count', 0)} failing scans ({_ts(prev.get('since'))} → {_ts(now)})")
            # Clear the failure so the recovery line prints exactly once.
            save(BLIND_STATE, {"healthy_since": now})
        return 0

    # Old state files carry only `error`; compare against that so an upgrade does not
    # restart the count (and so a stall signature change is noticed as a new fault).
    same = (prev.get("sig") or prev.get("error")) == sig
    state = {
        "since": prev.get("since") if same and prev.get("since") else now,
        "last": now,
        "count": int(prev.get("count", 0)) + 1 if same else 1,
        "sig": sig,
        "error": error,
        "announced": prev.get("announced") if same else False,
        "healthy_since": None,
    }

    # A stall is already ten missed cycles old when it is first seen, so it speaks at
    # once; a failing scan is allowed one blip.
    speak_at = 1 if stall else SPEAK_AFTER
    if state["count"] >= speak_at and not state["announced"]:
        state["announced"] = True
        if stall:
            notice("⚠️ 扫描在跑但没有推进：事件扫不进来、我不会被唤醒。"
                   f"{error}（`logs/scan.log` 里有 `[watchd] scan produced no summary`？）"
                   "详见 reports/blind.md。")
            print(f"{time.strftime('%F %T')} [blind] WARN scan-not-advancing x{state['count']}: {error}")
        else:
            notice(
                "⚠️ 我自己瞎了：扫描器连续失败，事件扫不进来、我不会被唤醒。"
                f"错误：{error[:200]}。{_diagnose(error)}"
                "详见 reports/blind.md。"
            )
            print(f"{time.strftime('%F %T')} [blind] WARN scanner failing x{state['count']}: {error[:200]}")

    save(BLIND_STATE, state)
    write_report(state, healthy=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
