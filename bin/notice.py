#!/usr/bin/env python3
"""notice.py — 即时告知制（POLICY §A.11）的留痕工具。

自主执行后**不追加消息打扰用户**，而是写一行 TSV 到 state/notices.jsonl；
08:30 简报任务（prompts/daily.md 步骤 3）汇总后归档到 reports/notices/<日期>.md 并截断。

用法：
  python3 bin/notice.py add --kind ruleset --text "修正 Protect Main 必需检查名 (loop→goal)；回退：gh api -X PUT repos/.../rulesets/19533179 ..." 
  python3 bin/notice.py list [--since 2026-10-16]
  python3 bin/notice.py _trim           # 保留最近 7 天（watchd 每轮调用）

kind 约定：ruleset | boundary | breach | mistake | other
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTICES = ROOT / "state" / "notices.jsonl"
RETENTION_DAYS = 7

KINDS = {"ruleset", "boundary", "breach", "mistake", "other"}


def _load() -> list[dict]:
    if not NOTICES.exists():
        return []
    rows = []
    for line in NOTICES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"ts": "", "kind": "other", "text": line})
    return rows


def _save(rows: list[dict]) -> None:
    NOTICES.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    NOTICES.write_text(body + ("\n" if body else ""), encoding="utf-8")


def cmd_add(args: argparse.Namespace) -> int:
    kind = args.kind if args.kind in KINDS else "other"
    rows = _load()
    # 同一 kind+text 只保留最新一条（幂等，避免重复上报）
    rows = [r for r in rows if not (r.get("kind") == kind and r.get("text") == args.text)]
    rows.append({
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "text": args.text,
    })
    _save(rows)
    print(f"notices.jsonl +1 ({kind})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = _load()
    if args.since:
        rows = [r for r in rows if (r.get("ts") or "") >= args.since]
    for r in rows:
        print(f"{r.get('ts','')}\t{r.get('kind','')}\t{r.get('text','')}")
    if not rows:
        print("(none)")
    return 0


def cmd_trim(_args: argparse.Namespace) -> int:
    cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    rows = _load()
    kept = [r for r in rows if (r.get("ts") or "") >= cutoff]
    if len(kept) != len(rows):
        _save(kept)
        print(f"trimmed {len(rows) - len(kept)} row(s), kept {len(kept)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--kind", default="other", help="|".join(sorted(KINDS)))
    p.add_argument("--text", required=True)
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list")
    p.add_argument("--since", default="")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("_trim")
    p.set_defaults(fn=cmd_trim)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
