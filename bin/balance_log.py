#!/usr/bin/env python3
"""余额对账：把 `future account balance` 追加成一条时间序列，用相邻两点相减算花费。

**这份文件是源，`~/.future/bin/balance-log` 是装好的副本。** 改这里之后跑一次
`python3 bin/balance_log.py install` 覆盖副本；`bin/selfcheck.py` 会比对两者的 sha256，
不一致就红——否则每天依赖它的 08:30 简报会抱着一个没进版本库、也没人测的脚本跑下去。

用法:
  balance-log record [--json]    读一次余额、追加一行，并打印与上一行的差
  balance-log report [--days 1]  只读已记录的点，不追加（不调用平台）
  balance-log tail [--n 10]      看最近几行
  balance-log install            把本文件装成 ~/.future/bin/balance-log（改完源码后必做）

记录格式（~/.future/balance-log.jsonl，一行一个 JSON）:
  {"at":"2026-10-21T01:45:00+08:00","ts":1792517...,
   "credits":76521.46869708,"balance_credits":765214686970800,
   "ledger_cost":700.33,"sessions":272,"tombstones":55}

为什么记三样东西：
  credits       平台余额（权威，唯一不需要估算的消耗口径）
  ledger_cost   本机会话累计 total_cost 之和（会少算：history reset 删 runs 行、
                已删会话用量级联消失、并发会话之间也无法区分）
  tombstones    已删会话计数（用它的增量解释上面那笔差额）

本脚本只读：不写平台、不改任何计费/额度设置，`future account balance` 本身零花费。
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import datetime

LOG = os.path.expanduser("~/.future/balance-log.jsonl")
AGENT_DB = os.path.expanduser("~/.future/agent/agent.db")


def now():
    return datetime.datetime.now().astimezone()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def find_future():
    """`future` CLI 的路径，两条来源都试。**

    在仓库里跑时，发现交给 `bin/toolpath.py`（本仓唯一获准写候选前缀的地方）；
    但这份脚本还有一份**装好的副本**在 `~/.future/bin/`，那里没有 `toolpath`
    ——所以再退到 PATH 与 `~/.future/bin/future`（本机装它就是装在这里）。
    两个候选都不是按机器写死的绝对路径，`selfcheck` 的 "toolchain not hardcoded"
    因此保持绿。
    """
    cands = []
    try:
        import toolpath  # only present when running from the repo checkout
        cands.append(toolpath.future_exe())
    except ImportError:
        pass
    cands.append(shutil.which("future"))
    cands.append(os.path.expanduser("~/.future/bin/future"))
    for cand in cands:
        if cand and os.path.exists(cand):
            return cand
    raise SystemExit("找不到 future 可执行文件（PATH 里没有，~/.future/bin 里也没有）")


def read_balance():
    proc = subprocess.run([find_future(), "account", "balance", "--json"],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SystemExit("future account balance 失败: " + (msg[0][:300] if msg else "无输出"))
    data = json.loads(proc.stdout)
    raw = data.get("balance_credits")
    if isinstance(raw, (int, float)):
        credits = raw / 1e10
    else:
        credits = float(data["credits"])
    return credits, raw if isinstance(raw, (int, float)) else None


def read_ledger():
    """本机会话累计花费 + 已删会话数；读不到就返回 None（不猜）。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % AGENT_DB, uri=True, timeout=15)
        try:
            rows, cost = con.execute(
                "select count(*), sum(cast(json_extract(current_metadata_json,'$.total_cost') as real))"
                " from sessions").fetchone()
            graves = con.execute("select count(*) from session_tombstones").fetchone()[0]
        finally:
            con.close()
        return (rows or 0), float(cost or 0.0), int(graves or 0)
    except Exception:
        return None


def load():
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def fmt(y):
    return "¥%.2f" % y


def describe(cur, prev):
    """把两点之间的差写成一行中文。cur/prev 都是记录字典。"""
    t0 = datetime.datetime.fromisoformat(prev["at"])
    t1 = datetime.datetime.fromisoformat(cur["at"])
    hours = (t1 - t0).total_seconds() / 3600.0
    d_bal = cur["credits"] - prev["credits"]          # 负数 = 花掉
    spend = -d_bal
    parts = []
    when = "%s → %s，%.1fh" % (t0.strftime("%m-%d %H:%M"), t1.strftime("%m-%d %H:%M"), hours)

    recharged = d_bal > 0.005
    if recharged:
        parts.append("余额上升 %s（%s）：有充值或退还，期间实际消耗无法由余额差得出"
                     % (fmt(d_bal), when))
    else:
        line = "余额差 %s（%s" % (fmt(spend), when)
        if hours >= 3 and abs(hours - 24.0) >= 2.0:
            line += "，均 %.2f/h" % (spend / hours)
        line += "）"
        parts.append(line)

    if cur.get("ledger_cost") is None or prev.get("ledger_cost") is None:
        parts.append("台账累加 无法读取（agent.db 不可读）")
        return " ｜ ".join(parts)

    d_led = cur["ledger_cost"] - prev["ledger_cost"]
    parts.append("台账累加 %s" % fmt(d_led))
    if recharged:
        return " ｜ ".join(parts)

    gap = spend - d_led
    if abs(gap) < 0.05:
        return " ｜ ".join(parts)
    why = []
    d_gr = (cur.get("tombstones") or 0) - (prev.get("tombstones") or 0)
    if d_gr > 0:
        why.append("窗口内删除 %d 个会话，其用量随之消失" % d_gr)
    if gap > 0:
        why.append("其余是从未入账的调用或窗口结束时尚未结算的用量")
    else:
        why.append("台账高于余额差：充值到账前已扣费，或上一次记录之后才结算的用量")
    parts.append("差 %s（%s）" % (fmt(gap), "；".join(why)))
    return " ｜ ".join(parts)


def cmd_record(args):
    credits, raw = read_balance()
    ledger = read_ledger()
    entry = {
        "at": iso(now()),
        "ts": int(now().timestamp()),
        "credits": round(credits, 8),
        "balance_credits": raw,
        "ledger_cost": None if ledger is None else round(ledger[1], 6),
        "sessions": None if ledger is None else ledger[0],
        "tombstones": None if ledger is None else ledger[2],
    }
    prev = load()
    prev = prev[-1] if prev else None
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if prev is None:
        text = "首次记录（基线）：余额 %s，台账累加 %s。下一次记录起才有余额差。" % (
            fmt(credits),
            "—" if ledger is None else fmt(ledger[1]))
    else:
        text = describe(entry, prev)
    if args.json:
        print(json.dumps({"entry": entry, "previous": prev, "summary": text},
                         ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


def cmd_report(args):
    rows = load()
    if len(rows) < 2:
        print("已记录 %d 个点，不足以算差（至少要 2 个）。" % len(rows))
        return 0
    cur = rows[-1]
    cutoff = cur["ts"] - int(args.days * 86400)
    prev = None
    for r in reversed(rows[:-1]):
        if r["ts"] <= cutoff:
            prev = r
            break
    if prev is None:
        prev = rows[0]
    print(describe(cur, prev))
    return 0


def cmd_tail(args):
    rows = load()[-args.n:]
    for r in rows:
        t = datetime.datetime.fromisoformat(r["at"]).strftime("%m-%d %H:%M")
        print("%s  余额 %12.4f  台账 %8.2f  会话 %s  墓碑 %s" % (
            t, r["credits"],
            -1 if r.get("ledger_cost") is None else r["ledger_cost"],
            r.get("sessions"), r.get("tombstones")))
    return 0


def cmd_install(args):
    """把仓库里的这份装成命令本身（同一条路径被 08:30 的简报调用）。

    装而不是「就地编辑」的理由：一个每天被依赖的脚本不在版本库里，就既没人看 diff、也没人测它，
    坏了只会表现为简报里少了一节。sha256 一致就不重写，避免无意义的 mtime 变动。
    """
    src = os.path.abspath(__file__)
    dst = os.path.expanduser(args.to)
    with open(src, "rb") as fh:
        data = fh.read()
    try:
        with open(dst, "rb") as fh:
            if fh.read() == data:
                print("已经是最新：%s" % dst)
                return 0
    except OSError:
        pass
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(data)
    os.chmod(dst, 0o755)
    print("已装到 %s（sha256 %s）" % (dst, hashlib.sha256(data).hexdigest()[:12]))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="余额对账（只读）")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("record", help="读一次余额并追加一行")
    r.add_argument("--json", action="store_true")
    rep = sub.add_parser("report", help="由已记录的点算差，不调用平台")
    rep.add_argument("--days", type=float, default=1.0)
    tl = sub.add_parser("tail", help="看最近的记录")
    tl.add_argument("--n", type=int, default=10)
    ins = sub.add_parser("install", help="把本文件装成 ~/.future/bin/balance-log")
    ins.add_argument("--to", default="~/.future/bin/balance-log")
    args = ap.parse_args(argv)
    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "tail":
        return cmd_tail(args)
    if args.cmd == "install":
        return cmd_install(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
