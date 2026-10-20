#!/usr/bin/env python3
"""directives.py — 一次性指令登记处（POLICY §A.12）。

用户**当场**的限制性指令（如「先不要提交 PR」）登记到这里，**48 小时后自动失效**；
到期仍需执行的须用户重新确认才转为长期口径。`bin/turn_prompt.py` 注入未过期指令。

用法：
  python3 bin/directives.py add --session 20261015-183548 --text "先不要提交 PR（本次 loop→goal）"
  python3 bin/directives.py list [--all]        # 默认只列未过期
  python3 bin/directives.py _expire             # 物理清掉过期行（watchd 每轮调用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIRECTIVES = ROOT / "state" / "directives.jsonl"
TTL = dt.timedelta(hours=48)


def _load() -> list[dict]:
    if not DIRECTIVES.exists():
        return []
    rows = []
    for line in DIRECTIVES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _save(rows: list[dict]) -> None:
    DIRECTIVES.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    DIRECTIVES.write_text(body + ("\n" if body else ""), encoding="utf-8")


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def active(rows: list[dict] | None = None) -> list[dict]:
    rows = _load() if rows is None else rows
    now = _now().isoformat(timespec="seconds")
    return [r for r in rows if (r.get("expires") or "") > now]


def cmd_add(args: argparse.Namespace) -> int:
    rows = _load()
    now = _now()
    rows = [r for r in rows if not (r.get("session") == args.session and r.get("text") == args.text)]
    rows.append({
        "ts": now.isoformat(timespec="seconds"),
        "session": args.session,
        "text": args.text,
        "expires": (now + TTL).isoformat(timespec="seconds"),
    })
    _save(rows)
    print(f"directives.jsonl +1 (expires {rows[-1]['expires']})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = _load()
    if not args.all:
        rows = active(rows)
    for r in rows:
        flag = "" if (r.get("expires") or "") > _now().isoformat(timespec="seconds") else " [expired]"
        print(f"{r.get('ts','')} → {r.get('expires','')}{flag}\t{r.get('session','')}\t{r.get('text','')}")
    if not rows:
        print("(none)")
    return 0


def cmd_expire(_args: argparse.Namespace) -> int:
    rows = _load()
    kept = active(rows)
    if len(kept) != len(rows):
        _save(kept)
        print(f"expired {len(rows) - len(kept)} directive(s), kept {len(kept)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--session", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("_expire")
    p.set_defaults(fn=cmd_expire)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
