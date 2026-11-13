#!/usr/bin/env python3
"""端点断言：`bin/scan.py` 的 `main()` 必须是**一整个**函数，守护进程必须能看出它没跑完。

为什么需要这一份（`docs/gotcha-scan.md` B15）：一次"降噪"改动把模块级的常量与
`transient_failure()` 以第 0 列缩进插进了 `main()` 体内。Python 对此完全合法——
`main()` 在插入点就结束了，之后约 360 行（`emit()`、逐会话循环、
`state["lastScanTs"] = now`、两次 `save_json`、收尾的 `sessions=…` 日志）成了新函数的
函数体里的死代码。没有 traceback、退出码 0、`logs/scan.log` 每 33 秒照旧写两行，
`selfcheck` 全绿。于是扫描器空转了 18.4 小时，一条事件都没扫出来。

所以这里钉的不是"某个函数返回什么"，而是**结构**：

  1. 解析 `bin/scan.py`，取模块级 `main()` 的 `ast.FunctionDef`；
  2. 断言它体内确实有 `state["lastScanTs"] = now`（状态写入点）；
  3. 断言它的最后一条语句是 `return 0`；
  4. 断言它的语句数不低于真实值的 90%（一次合法截断会砍掉绝大部分）；
  5. 断言它体内有那条收尾日志 `log(... sessions= ...)`。

第 2–5 条都只在"代码真的在 `main()` 里"时成立，所以既不需要跑扫描、也不需要 `future` CLI。

另一半是守护进程的可观测性：`bin/watchd.sh` 每轮扫描后必须能看出"这一轮没有收尾行/
退出码非 0"，并在 `logs/scan.log` 里明说。这里用一个临时 fixture 真跑一遍守护循环
（stub `scan.py`），而不是读 shell 源码猜它会不会工作。
"""
from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN = ROOT / "bin" / "scan.py"
WATCHD = ROOT / "bin" / "watchd.sh"

# 真实值 69 条语句（写这条断言时）。留 10% 余量：一次"合法的函数截断"会砍掉
# 绝大多数语句，而一次正常的局部重构只动几条。
MIN_MAIN_STATEMENTS = 62

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ------------------------------------------------------------------ 结构断言


def _is_last_scan_ts_assign(stmt: ast.stmt) -> bool:
    """`state["lastScanTs"] = now` (plus whatever it is on the right)."""
    targets = getattr(stmt, "targets", None)
    if not isinstance(stmt, ast.Assign) or not targets:
        return False
    tgt = targets[0]
    return (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
            and tgt.value.id == "state"
            and isinstance(tgt.slice, ast.Constant) and tgt.slice.value == "lastScanTs")


def _logs_summary(stmt: ast.stmt) -> bool:
    """Any `log(...)` call inside this statement whose text carries `sessions=`."""
    for node in ast.walk(stmt):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "log"):
            continue
        for part in ast.walk(node):
            if isinstance(part, ast.Constant) and isinstance(part.value, str) \
                    and "sessions=" in part.value:
                return True
    return False


def main_structure_problems(source: str) -> list[str]:
    """Every way this source's `main()` fails to be one whole function ([] = fine)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"does not parse: {exc}"]
    mains = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if len(mains) != 1:
        return [f"expected exactly one module-level main(), found {len(mains)}"]
    body = mains[0].body
    problems: list[str] = []
    if len(body) < MIN_MAIN_STATEMENTS:
        problems.append(f"main() body has {len(body)} statements "
                        f"(< {MIN_MAIN_STATEMENTS}) — it ends early")
    last = body[-1]
    if not (isinstance(last, ast.Return) and isinstance(last.value, ast.Constant)
            and last.value.value == 0):
        problems.append(f"main()'s last statement is {type(last).__name__}, not `return 0`")
    if not any(_is_last_scan_ts_assign(st) for st in body):
        problems.append('main() body has no `state["lastScanTs"] = now` — '
                        "the state write landed outside it")
    if not any(_logs_summary(st) for st in body):
        problems.append("main() body has no `log(... sessions= ...)` summary line — "
                        "the close of a scan landed outside it")
    return problems


def _truncate_main(source: str, inline: bool = False) -> str:
    """Re-create the 事故形状：`HIGH_KINDS = {...}` 之后来一条第 0 列的 def。

    这正是"把模块级常量 + 辅助函数粘进函数体中间"的语法效果：`main()` 在此合法结束，
    后面那些 4 空格缩进的语句变成新函数的函数体。

    `inline=True` 则写成 `def _boom(): pass` 一行——那条会留下一个**语法错误**
    （后面跟着的缩进块无处可去），所以它也会变红，但红的原因不同；两种都钉住。
    """
    body = ["def _boom(): pass\n"] if inline else ["def _boom():\n", "    pass\n"]
    lines = source.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("    HIGH_KINDS = {"):
            return "".join(lines[:i + 1] + body + lines[i + 1:])
    raise AssertionError("no `    HIGH_KINDS = {` line in main() to mutate")


# ------------------------------------------------------- watchd.sh 可观测性


def _fixture(tmp: pathlib.Path, stub: str) -> pathlib.Path:
    """A throwaway checkout: one stub `bin/scan.py`, the real `bin/watchd.sh`."""
    root = tmp / "repo"
    for sub in ("bin", "logs", "state", "inbox", "journal", "reports"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "bin" / "scan.py").write_text(stub, encoding="utf-8")
    shutil.copy(WATCHD, root / "bin" / "watchd.sh")
    (root / "bin" / "config.json").write_text('{"scan_interval_seconds": 1}\n',
                                              encoding="utf-8")
    return root


def _run_watchd(root: pathlib.Path, seconds: float = 2.0) -> tuple[str, bool]:
    """(logs/scan.log, stopped-on-SIGTERM) from a couple of cycles of the real loop.

    The loop has no "run once" mode on purpose (a manual tick would auto-approve in a
    live checkout), so the test stops it like a human would — and that stop is asserted
    too: buffering the round's output added a `trap`, and a `trap … TERM` that only
    cleans up would swallow SIGTERM and leave the daemon running.
    """
    env = dict(os.environ, SESSION_WATCH_INTERVAL="1")
    proc = subprocess.Popen(["bash", str(root / "bin" / "watchd.sh")], cwd=str(root),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env)
    stopped = False
    try:
        time.sleep(seconds)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
            stopped = True
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged daemon
            proc.kill()
            proc.wait(timeout=10)
    return (root / "logs" / "scan.log").read_text(encoding="utf-8", errors="replace"), stopped


SUMMARY_STUB = """\
import time
print(time.strftime('%F %T') + ' [scan] sessions=12 active=3 deep=0 events=0', flush=True)
raise SystemExit({rc})
"""

SILENT_STUB = """\
import time
print(time.strftime('%F %T') + ' [scan] resync scan (gap 900s > 300s): only urgent events', flush=True)
print(time.strftime('%F %T') + ' [scan] resync detail: lastScanTs=2026-11-13 09:50:11', flush=True)
raise SystemExit({rc})
"""


def _main_statements(source: str) -> int:
    tree = ast.parse(source)
    return len([n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main"][0].body)


def main() -> int:
    # 1. 本仓库的 scan.py 现在必须是完整的
    source = SCAN.read_text(encoding="utf-8")
    problems = main_structure_problems(source)
    check("bin/scan.py's main() is one whole function", not problems,
          "; ".join(problems))

    # 2. 变异必须能红：把当年那一行插回去，同一套断言必须失败
    truncated = _truncate_main(source)
    with tempfile.TemporaryDirectory(prefix="scan-structure-") as tmp:
        copy = pathlib.Path(tmp) / "scan.py"
        copy.write_text(truncated, encoding="utf-8")
        mutated_problems = main_structure_problems(copy.read_text(encoding="utf-8"))
        check("a column-0 def inside main() turns the assertion red",
              bool(mutated_problems), "the mutated copy still passed")
        check("the red names the truncated body",
              any("ends early" in p for p in mutated_problems), f"{mutated_problems}")
        check("the red names the missing state write",
              any("lastScanTs" in p for p in mutated_problems), f"{mutated_problems}")
        check("the red names the missing `return 0`",
              any("return 0" in p for p in mutated_problems), f"{mutated_problems}")
        if mutated_problems:
            print(f"      mutation red: {mutated_problems[0]}")
    check("restoring the source turns it green again", not main_structure_problems(source))
    # the same break written as one line (`def _boom(): pass`) leaves no room for the
    # indented block, so it fails one step earlier — a syntax error is still red, and
    # the assertion must not depend on *which* way the edit broke the file
    inline_problems = main_structure_problems(_truncate_main(source, inline=True))
    check("the one-line form of the same break is red too", bool(inline_problems),
          f"{inline_problems}")
    print(f"      mutation red (one line): {inline_problems[0]}")
    print(f"      main() body: {_main_statements(source)} statements "
          f"(floor {MIN_MAIN_STATEMENTS})")

    # 3. watchd 必须能说出"这一轮没有收尾行"
    with tempfile.TemporaryDirectory(prefix="watchd-") as tmp:
        root = _fixture(pathlib.Path(tmp), SILENT_STUB.format(rc=0))
        log, stopped = _run_watchd(root)
        check("watchd still stops on SIGTERM", stopped, "the loop kept running after TERM")
        check("watchd reports a scan that printed no summary",
              "[watchd] scan produced no summary (exit=0)" in log,
              f"log={log[-400:]!r}")
        check("the silent round's own lines are still kept",
              "resync detail" in log, f"log={log[-400:]!r}")

    with tempfile.TemporaryDirectory(prefix="watchd-") as tmp:
        root = _fixture(pathlib.Path(tmp), SUMMARY_STUB.format(rc=0))
        log, _ = _run_watchd(root)
        check("watchd stays quiet about a healthy round",
              "scan produced no summary" not in log, f"log={log[-400:]!r}")
        check("a healthy round's summary line is still appended",
              "[scan] sessions=" in log, f"log={log[-400:]!r}")

    with tempfile.TemporaryDirectory(prefix="watchd-") as tmp:
        root = _fixture(pathlib.Path(tmp), SUMMARY_STUB.format(rc=3))
        log, _ = _run_watchd(root)
        check("watchd reports a non-zero exit even when the summary is there",
              "[watchd] scan produced no summary (exit=3)" in log,
              f"log={log[-400:]!r}")

    print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
