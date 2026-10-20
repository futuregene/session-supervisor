#!/usr/bin/env python3
"""Selftest for notice.py + directives.py (POLICY §A.11/§A.12) and the turn_prompt injection.

Runs entirely in a temp dir by monkey-patching the module-level paths — no repo state touched.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="notice-selftest-"))
    notice = load("notice", ROOT / "bin" / "notice.py")
    notice.NOTICES = tmp / "notices.jsonl"

    # add: idempotent (same kind+text replaces)
    notice.cmd_add(type("A", (), {"kind": "ruleset", "text": "x"})())
    notice.cmd_add(type("A", (), {"kind": "ruleset", "text": "x"})())
    notice.cmd_add(type("A", (), {"kind": "boundary", "text": "y"})())
    rows = notice._load()
    check("notice: dedup same kind+text", len(rows) == 2, f"rows={len(rows)}")

    # unknown kind falls back to other
    notice.cmd_add(type("A", (), {"kind": "weird", "text": "z"})())
    rows = notice._load()
    check("notice: unknown kind -> other", rows[-1]["kind"] == "other")

    # trim keeps recent
    old = dict(rows[-1])
    old["ts"] = "2020-01-01T00:00:00+08:00"
    rows.append(old)
    notice._save(rows)
    notice.cmd_trim(None)
    check("notice: _trim drops >7d", len(notice._load()) == 3, f"rows={len(notice._load())}")

    directives = load("directives", ROOT / "bin" / "directives.py")
    directives.DIRECTIVES = tmp / "directives.jsonl"

    directives.cmd_add(type("A", (), {"session": "s1", "text": "先不要提交 PR"})())
    directives.cmd_add(type("A", (), {"session": "s1", "text": "先不要提交 PR"})())
    rows = directives._load()
    check("directives: dedup same session+text", len(rows) == 1, f"rows={len(rows)}")
    check("directives: active sees it", len(directives.active()) == 1)

    # expired rows are not active and _expire removes them
    rows[0]["expires"] = "2020-01-01T00:00:00+08:00"
    directives._save(rows)
    check("directives: expired not active", len(directives.active()) == 0)
    directives.cmd_expire(None)
    check("directives: _expire removes", len(directives._load()) == 0)

    # turn_prompt renders active directives into the card tail
    tp = load("turn_prompt", ROOT / "bin" / "turn_prompt.py")
    watch = tmp / "watch"
    (watch / "prompts").mkdir(parents=True)
    (watch / "state").mkdir()
    (watch / "prompts" / "turn.md").write_text("CARD\n", encoding="utf-8")
    (watch / "prompts" / "append.md").write_text("MANUAL\n", encoding="utf-8")
    tp.WATCH = str(watch)
    tp.APPEND = str(watch / "prompts" / "append.md")
    tp.CARD = str(watch / "prompts" / "turn.md")
    tp.APPLIED = str(watch / "state" / "append_applied.json")

    directives.DIRECTIVES = tmp / "directives.jsonl"  # fresh
    if directives.DIRECTIVES.exists():
        directives.DIRECTIVES.unlink()
    directives.cmd_add(type("A", (), {"session": "s2", "text": "等我看"})())
    # turn_prompt does `import directives` — hand it our monkey-patched module
    sys.modules["directives"] = directives
    out = tp.compose(str(watch))
    check("turn_prompt: card carries directive", "等我看" in out and "48h" in out)

    directives.DIRECTIVES.unlink(missing_ok=True)
    out = tp.compose(str(watch))
    check("turn_prompt: no directives -> no section", "一次性指令" not in out)

    print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
