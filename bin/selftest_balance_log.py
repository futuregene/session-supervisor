#!/usr/bin/env python3
"""Selftest for balance_log.py — the accounting the daily briefing depends on.

Runs on synthetic records in a temp dir (no platform call, no real log touched).
What is pinned is exactly what the briefing would get wrong: the delta's sign
direction (spending is a *negative* balance move), the recharge case (a rising
balance must not be reported as negative spend), the under-count explanation
(tombstones), and the refusal to invent a number when the ledger is unreadable.

`credits` is a balance (falls as money is spent); `ledger_cost` is a cumulative
sum of session costs (rises as money is spent). Both are read from a record pair.
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load():
    spec = importlib.util.spec_from_file_location("balance_log", ROOT / "bin" / "balance_log.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="balance-log-"))
    mod.LOG = tmp / "balance-log.jsonl"
    return mod


def rec(at, credits, ledger, graves=0):
    return {"at": at, "ts": 0, "credits": credits,
            "ledger_cost": ledger, "tombstones": graves, "sessions": 1}


# A chain of one record per day at 08:30, with the values the assertions below expect.
A = rec("2026-10-20T08:30:00+08:00", 1000.0, 100.0)
B = rec("2026-10-21T08:30:00+08:00", 944.0, 148.0, graves=2)   # spent 56, ledger 48, 2 deleted
C = rec("2026-10-22T08:30:00+08:00", 934.0, 158.0, graves=2)   # spent 10, ledger 10, agree
D = rec("2026-10-23T08:30:00+08:00", 1034.0, 168.0, graves=2)  # recharge: balance rose 100
E = rec("2026-10-24T08:30:00+08:00", 1024.0, None, graves=2)   # spent 10, ledger unreadable


def main() -> int:
    mod = load()

    # 1. spending is a negative balance move and is reported as a positive cost
    line = mod.describe(B, A)
    check("spend is the negative balance move", "余额差 ¥56.00" in line, line)
    check("the ledger column is reported too", "台账累加 ¥48.00" in line, line)
    check("the gap is the difference of the two columns", "｜ 差 ¥8.00" in line, line)
    check("the gap is explained by deleted sessions", "删除 2 个会话" in line, line)

    # 2. agreement needs no explanation — and must not invent one
    line = mod.describe(C, B)
    check("agreement reports no gap", "｜ 差 ¥" not in line, line)
    check("agreement still shows both columns",
          "余额差 ¥10.00" in line and "台账累加 ¥10.00" in line, line)

    # 3. a rising balance is a recharge, not negative spend
    line = mod.describe(D, C)
    check("a recharge is named", "余额上升" in line, line)
    check("a recharge is not reported as spend", "余额差 ¥" not in line, line)
    check("a recharge says the period's spend is unknowable", "无法由余额差得出" in line, line)

    # 4. an unreadable ledger is stated, never filled in with a guess
    line = mod.describe(E, D)
    check("an unreadable ledger is stated", "无法读取" in line, line)
    check("an unreadable ledger produces no fabricated gap", "｜ 差 ¥" not in line, line)
    check("an unreadable ledger still shows the balance delta", "余额差 ¥10.00" in line, line)

    # 5. a short window gets an hourly rate: the first briefing after the baseline
    #    covers a partial day, and "¥56 in 0.3 days" invites a wrong read
    short_a = rec("2026-10-21T01:51:00+08:00", 1000.0, 100.0)
    short_b = rec("2026-10-21T08:30:00+08:00", 980.0, 120.0)
    line = mod.describe(short_b, short_a)
    check("a short window shows a per-hour rate", "均 3.0" in line and "/h" in line, line)

    # 6. a full day does not print a rate (it is just "yesterday")
    check("a full day omits the per-hour rate", "/h" not in mod.describe(C, B))

    # 7. reading the log tolerates a torn line instead of dropping the history
    mod.LOG.write_text('{"at":"x"}\nnot json\n{"at":"y"}\n', encoding="utf-8")
    check("a malformed line is skipped, not fatal", len(mod.load()) == 2, f"{mod.load()}")

    # 8. the source is the one that gets installed, and installing is idempotent
    src = (ROOT / "bin" / "balance_log.py").read_bytes()
    check("install subcommand exists", hasattr(mod, "cmd_install"))
    check("source hashes deterministically",
          hashlib.sha256(src).hexdigest() == hashlib.sha256(src).hexdigest())
    target = pathlib.Path(tempfile.mkdtemp(prefix="balance-install-")) / "bin" / "balance-log"
    ns = type("A", (), {"to": str(target)})()
    mod.cmd_install(ns)
    mod.__file__ = str(ROOT / "bin" / "balance_log.py")  # install always copies *this* file
    check("install writes an executable copy", target.exists() and (target.stat().st_mode & 0o111),
          f"{target} exists={target.exists()}")
    check("the installed copy is byte-identical", target.read_bytes() == src)
    before = target.stat().st_mtime_ns
    mod.cmd_install(ns)
    check("installing again is a no-op (mtime unchanged)", target.stat().st_mtime_ns == before)

    print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
