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
STATE = os.path.join(WATCH, "state")
HEALTH = os.path.join(STATE, "health.json")
BLIND_STATE = os.path.join(STATE, "blind.json")
REPORT = os.path.join(WATCH, "reports", "blind.md")
NOTICE = os.path.join(WATCH, "bin", "notice.py")

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


def have_health() -> tuple[bool, str, float]:
    health = load(HEALTH, None)
    if not isinstance(health, dict):
        return False, "state/health.json is missing or unreadable", 0.0
    ts = float(health.get("ts") or 0.0)
    if not health.get("ok"):
        err = (health.get("error") or "ok=false").strip()
        detail = (health.get("stderr") or "").strip().splitlines()
        if detail:
            err = f"{err}: {detail[0][:300]}"
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
        lines += [
            f"当前：**扫描器在失败**，自 {_ts(state.get('since'))} 起连续 {state.get('count', 0)} 次。",
            "",
            f"最后一次错误：`{state.get('error', '')}`",
            "",
            "影响：事件不会被扫描进来，我不会被唤醒——也就是说**我看不见任何会话**，",
            "直到 CLI 恢复。已在 `logs/scan.log` 打了 WARN。",
            "",
            "常见原因（按概率）：`~/.future/bin/future` 被换成新构建，而正在跑的",
            "`future host` 仍是旧镜像（版本握手失败）。修法：重启 host。",
            "",
        ]
    tmp = REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, REPORT)


def _ts(sec) -> str:
    if not sec:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(sec)))


def main() -> int:
    healthy, error, _ = have_health()
    prev = load(BLIND_STATE, {}) or {}
    now = time.time()

    if healthy:
        if prev.get("since"):
            write_report(prev, healthy=True)
            print(f"{time.strftime('%F %T')} [blind] recovered after "
                  f"{prev.get('count', 0)} failing scans ({_ts(prev.get('since'))} → {_ts(now)})")
            # Clear the failure so the recovery line prints exactly once.
            save(BLIND_STATE, {"healthy_since": now})
        return 0

    same = prev.get("error") == error
    state = {
        "since": prev.get("since") if same and prev.get("since") else now,
        "last": now,
        "count": int(prev.get("count", 0)) + 1 if same else 1,
        "error": error,
        "announced": prev.get("announced") if same else False,
        "healthy_since": None,
    }

    if state["count"] >= SPEAK_AFTER and not state["announced"]:
        state["announced"] = True
        notice(
            "⚠️ 我自己瞎了：扫描器连续失败，事件扫不进来、我不会被唤醒。"
            f"错误：{error[:200]}。常见原因是 ~/.future/bin/future 换了新构建但 "
            "`future host` 还在跑旧镜像（版本握手失败）——重启 host 即恢复。"
            "详见 reports/blind.md。"
        )
        print(f"{time.strftime('%F %T')} [blind] WARN scanner failing x{state['count']}: {error[:200]}")

    save(BLIND_STATE, state)
    write_report(state, healthy=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
