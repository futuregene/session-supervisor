#!/usr/bin/env python3
"""Auto-approve pending tool-permission requests (the user's "yolo" authorization).

Deterministic, no model calls. Every decision is written to state/approvals.jsonl
and appended to reports/approvals.md so the user can audit or reverse it.

  approvals.py auto [--limit N] [--dry-run]
  approvals.py list
"""
import json
import os
import re
import subprocess
import sys
import time

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path  # noqa: E402

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = config_path()
LEDGER = os.path.join(WATCH, "state", "approvals.jsonl")
REPORT = os.path.join(WATCH, "reports", "approvals.md")


def cfg():
    try:
        return json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:
        return {}


def future(args, timeout=45):
    exe = cfg().get("future_bin") or "future"
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run([exe] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def rows():
    out = []
    try:
        with open(LEDGER, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


def now_s():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# "yolo" + B + "合并也自动": push 自己的分支、开 PR、合并 PR 都归自动；
# 这里只拦真正不可逆或对外的部分：强推、直推主干（`main`/`master`；本仓默认分支就是 `main`）、
# 发版、部署、删库。`p2` 保留在拒绝清单里当**防复发护栏**：2026-10-15 曾误把用户说的 "PR"
# 当成分支名建了 `P2`，别再建。
DENY_PATTERNS = [
    r"--force\b", r"--force-with-lease", r"\bpush\b[^&|;]*\s(main|master|p2)\b",
    r"\bpush\s+-f\b", r"git\s+reset\s+--hard", r"git\s+branch\s+-D\b",
    r"gh\s+release", r"npm\s+publish", r"cargo\s+publish", r"docker\s+push",
    r"kubectl\s+apply", r"terraform\s+apply", r"\bdeploy\b", r"\bsystemctl\b",
    r"pm2\s+(delete|stop|restart)", r"launchctl\s+(bootout|disable)",
    r"rm\s+-rf?\s+/", r"\bmkfs\b", r"\bdd\s+if=",
    r"DROP\s+TABLE", r"TRUNCATE\s+TABLE",
]
DENY_RE = re.compile("|".join(DENY_PATTERNS), re.IGNORECASE)

def main(argv):
    cmd = argv[0] if argv else "list"
    c = cfg()
    if cmd == "list":
        rc, o, _ = future(["session", "list", "--json"])
        if rc != 0:
            print("agent unreachable")
            return 1
        found = 0
        for s in json.loads(o).get("sessions", []):
            rc, a, _ = future(["session", "approvals", s["id"], "--json"])
            if rc != 0:
                continue
            for req in json.loads(a).get("pendingApprovals", []):
                found += 1
                print(f"{s['id']} {req.get('requestId')} {req.get('toolName') or req.get('tool')} "
                      f"{json.dumps(req, ensure_ascii=False)[:160]}")
        print(f"-- {found} pending")
        return 0

    if cmd != "auto":
        print(__doc__)
        return 2
    if os.path.exists(os.path.join(WATCH, "state", "PAUSE")):
        print("PAUSE present; not auto-approving")
        return 0
    if not c.get("auto_approve"):
        return 0

    dry = "--dry-run" in argv
    limit = int(c.get("auto_approve_per_cycle", 5))
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    per_hour = int(c.get("auto_approve_per_hour", 20))
    done_last_hour = sum(1 for r in rows() if time.time() - float(r.get("ts") or 0) < 3600)
    budget = max(0, min(limit, per_hour - done_last_hour))
    if budget <= 0:
        print(f"hourly auto-approve budget spent ({done_last_hour}/{per_hour})")
        return 0

    rc, o, _ = future(["session", "list", "--json"])
    if rc != 0:
        print("agent unreachable; nothing approved")
        return 1
    approved, seen = 0, 0
    for s in json.loads(o).get("sessions", []):
        if approved >= budget:
            break
        if s.get("id") in (c.get("exclude_sessions") or []):
            continue
        rc, a, _ = future(["session", "approvals", s["id"], "--json"])
        if rc != 0:
            continue
        for req in json.loads(a).get("pendingApprovals", []):
            if approved >= budget:
                break
            seen += 1
            rid = req.get("requestId") or req.get("id")
            tool = req.get("toolName") or req.get("tool") or "?"
            blob = json.dumps(req, ensure_ascii=False)
            bounce = DENY_RE.search(blob)
            if bounce:
                rec = {"ts": time.time(), "at": now_s(), "sessionId": s["id"],
                       "sessionName": (s.get("sessionName") or "")[:80], "requestId": rid,
                       "tool": tool, "detail": blob[:600],
                       "action": "escalated", "reason": f"deny-pattern: {bounce.group(0)}"}
                with open(REPORT, "a", encoding="utf-8") as fh:
                    fh.write(f"- {rec['at']} `{s['id']}` 请求 `{rid}` 工具 **{tool}** "
                             f"→ 未自动批准（命中 `{bounce.group(0)}`），待你决定\n")
                with open(os.path.join(WATCH, "reports", "needs-you.md"), "a",
                          encoding="utf-8") as fh:
                    fh.write(f"\n- {rec['at']} 会话 `{s['id']}`（{rec['sessionName']}）的权限请求 "
                             f"`{rid}`（工具 {tool}）命中拒绝清单 `{bounce.group(0)}`：{blob[:300]}\n")
                os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
                with open(LEDGER, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"escalated (not approved): {s['id']} {rid} {tool} [{bounce.group(0)}]")
                continue
            rec = {"ts": time.time(), "at": now_s(), "sessionId": s["id"],
                   "sessionName": (s.get("sessionName") or "")[:80], "requestId": rid,
                   "tool": tool, "detail": json.dumps(req, ensure_ascii=False)[:600],
                   "action": "approve" if not dry else "dry-run"}
            if not dry:
                rc2, o2, e2 = future(["session", "approve", s["id"], str(rid),
                                      "--note" "auto-approved by session supervisor (user yolo authorization)"])
                rec["rc"] = rc2
                rec["result"] = (o2 or e2).strip()[:200]
                approved += 1
            os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(REPORT, "a", encoding="utf-8") as fh:
                fh.write(f"- {rec['at']} `{s['id']}` 请求 `{rid}` 工具 **{tool}** → {rec['action']}\n")
            print(f"{rec['action']}: {s['id']} {rid} {tool}")
    if seen == 0:
        print("nothing pending")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
