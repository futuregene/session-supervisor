#!/usr/bin/env python3
"""Auto-approve pending tool-permission requests (the user's "yolo" authorization).

Deterministic, no model calls. Every decision is written to state/approvals.jsonl
and appended to reports/approvals.md so the user can audit or reverse it.

  approvals.py auto [--limit N] [--dry-run]
  approvals.py list

One `session approvals` call per session per cycle does not scale: this facility
runs a cycle every 30 s with a few hundred sessions, so the old shape spawned a
few hundred CLI processes (each leaving a `~/.future/cli/logs/cli.<pid>.log`) to
discover that almost every session had nothing pending. The cycle now asks only
about the sessions that can have changed (see `candidates()`), caches the answer
in state/approval-cache.json, and keeps a hard per-cycle cap plus a staleness
backstop so nothing is *dropped* — only deferred to a later cycle.
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
# Session ids the supervisor must keep its hands off, local and expiring
# (`{"session": "…", "until": "<local ISO>"}` per line). A user may be measuring
# what auto-approval does in a session they are driving live; approving there
# would answer the very question under test and override a click they just made.
# Lives under state/ on purpose: it names one machine's sessions, so it must not
# enter git (`bin/config.json: exclude_sessions` stays for permanent exclusions).
EXCLUDE = os.path.join(WATCH, "state", "approval-exclude.jsonl")
# What each session looked like the last time we actually asked it about pending
# approvals: `{session: {"updatedAtMs": n, "checkedAt": epoch}}`. Runtime state
# (it names this machine's sessions), so it lives under state/ and not in git.
CACHE = os.path.join(WATCH, "state", "approval-cache.json")
# Mirrored into the SQLite store before the file write (see the note in bin/scan.py).
# A mirror failure is logged and swallowed: approval decisions are the one thing in
# this repo that must never be lost, and the file write stays the authoritative one.
import store  # noqa: E402


def kept_away(now=None):
    now = time.time() if now is None else now
    ids = set()
    try:
        with open(EXCLUDE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    ids.add(line)
                    continue
                if isinstance(row, str):
                    ids.add(row)
                    continue
                until = row.get("until")
                if until:
                    try:
                        if time.mktime(time.strptime(until[:19], "%Y-%m-%dT%H:%M:%S")) <= now:
                            continue
                    except Exception:
                        pass
                if row.get("session"):
                    ids.add(row["session"])
    except FileNotFoundError:
        pass
    return ids


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


def load_cache():
    """Per-session record of the last real observation, or {} when unreadable.

    A corrupted or missing file must never suppress a check: `{}` means "nothing
    has been looked at", which makes every session a candidate (bounded by the
    per-cycle cap). Losing the cache costs process spawns, not approvals.
    """
    try:
        with open(CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_cache(cache):
    """Write atomically: a half-written cache must not survive a crash."""
    try:
        store.mirror_at("approvals", store.write_approval_cache, CACHE, "approval_cache", cache)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, CACHE)
    except OSError:
        pass  # a cache that cannot be written only costs spawns next cycle


# Why a session earns an approval check this cycle, most urgent first. `streaming`
# is first because that is exactly what a *parked* run looks like: an approval is
# a decision the run is blocked on, so `isStreaming` is still true (measured on a
# real park, 2026-11-03: 8 samples over 42 s, all `isStreaming=true`, `updatedAtMs`
# frozen). `changed` is the second signal — the request itself bumps `updatedAtMs`
# (same measurement: …631622 → …632879 the moment the request appeared). The last
# two are cold start and the staleness backstop, which is what keeps this safe if
# either signal ever stops holding: nothing is dropped, only deferred.
CANDIDATE_RANK = {"streaming": 0, "changed": 1, "unseen": 2, "stale": 3}


def candidates(sessions, cache, c=None, now=None):
    """The sessions worth one `session approvals` call this cycle, and why.

    Returns `[(reason, session)]` in the session list's own order — the cap
    decides *whether* a session is asked about, never the order its pending
    requests are decided in (the deny list, the cycle/hour budgets and the
    `kept_away()` skips apply to that order exactly as before).

    Ordering *within* the selection: a parked run sorts by the oldest
    `updatedAtMs` (frozen when it parked, so the longest wait comes first), while
    everything else sorts freshest-first (a brand-new run is the likeliest place
    for a request to appear).
    """
    c = cfg() if c is None else c
    now = time.time() if now is None else now
    cap = int(c.get("max_approval_checks_per_cycle", 40))
    stale_after = float(c.get("approval_rescan_hours", 2)) * 3600
    ranked = []
    for i, s in enumerate(sessions):
        sid = s.get("id")
        entry = cache.get(sid)
        entry = entry if isinstance(entry, dict) else None
        if s.get("isStreaming"):
            reason = "streaming"
        elif entry is None:
            reason = "unseen"
        elif entry.get("updatedAtMs") != s.get("updatedAtMs"):
            reason = "changed"
        elif now - float(entry.get("checkedAt") or 0) >= stale_after:
            reason = "stale"
        else:
            continue
        stamp = s.get("updatedAtMs") or 0
        tiebreak = stamp if reason == "streaming" else -stamp
        ranked.append((CANDIDATE_RANK[reason], tiebreak, i, reason, s))
    ranked.sort(key=lambda t: t[:3])
    picked = [(i, reason, s) for _, _, i, reason, s in ranked[:max(0, cap)]]
    picked.sort(key=lambda t: t[0])
    return [(reason, s) for _, reason, s in picked]


def request_id(req):
    """The id `future session approve` needs for one parked request.

    The agent's payload spells it `approval_request_id` (snake_case), which is not
    what this script used to look for: `req.get("requestId") or req.get("id")` is
    None for every real request, so the approve argv carried the string "None"
    and the CLI answered "approval request `None` is not pending" (rc=1). The
    ledger then recorded `"action": "failed"` — seen on a real park 2026-11-03.
    All three spellings are accepted so a payload that does carry `requestId`
    keeps working.
    """
    return req.get("approval_request_id") or req.get("requestId") or req.get("id")


def tool_of(req):
    """The tool name, from whichever key the payload uses (for the report line)."""
    action = req.get("action")
    nested = action.get("tool") if isinstance(action, dict) else None
    return req.get("tool_name") or req.get("toolName") or req.get("tool") or nested or "?"


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

APPROVE_NOTE = "auto-approved by session supervisor (user yolo authorization)"


def approve_argv(session_id, request_id):
    """The argv that lets one parked request run.

    Kept in one function because this is where auto-approval silently died for a
    long time: `"--note" "auto-approved …"` is one Python string (adjacent literals
    concatenate), so the CLI was handed `--noteauto-approved …` and answered
    "unknown option", rc=1, for every request. The caller counted the request as
    approved anyway, so the ledger said "→ approve" while nothing ever ran.
    `bin/selftest_deny.py` now pins this shape.
    """
    return ["session", "approve", str(session_id), str(request_id), "--note", APPROVE_NOTE]

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
                print(f"{s['id']} {request_id(req)} {tool_of(req)} "
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
    sessions = json.loads(o).get("sessions", [])
    # One `session approvals` call per session per cycle is what made this cycle
    # spawn a few hundred CLI processes. Ask only about the sessions that can have
    # changed (`candidates()`), remember the answer, and keep every decision rule
    # below untouched.
    fresh = {sid: row for sid, row in load_cache().items()
             if sid in {s.get("id") for s in sessions} and isinstance(row, dict)}
    checked, why = 0, {}
    approved, seen = 0, 0
    away = kept_away()
    for reason, s in candidates(sessions, fresh, c):
        if approved >= budget:
            break
        if s.get("id") in (c.get("exclude_sessions") or []) or s.get("id") in away:
            continue
        rc, a, _ = future(["session", "approvals", s["id"], "--json"])
        if rc != 0:
            # Unreadable answer: drop what we remembered, so this session stays a
            # candidate next cycle instead of looking settled. A failed read must
            # never be cached as "nothing pending".
            fresh.pop(s.get("id"), None)
            continue
        fresh[s.get("id")] = {"updatedAtMs": s.get("updatedAtMs"), "checkedAt": time.time()}
        checked += 1
        why[reason] = why.get(reason, 0) + 1
        for req in json.loads(a).get("pendingApprovals", []):
            if approved >= budget:
                break
            seen += 1
            rid = request_id(req)
            tool = tool_of(req)
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
                store.mirror_at("approvals", store.append_approval_ledger, LEDGER,
                                "approval_ledger", rec)
                with open(LEDGER, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"escalated (not approved): {s['id']} {rid} {tool} [{bounce.group(0)}]")
                continue
            rec = {"ts": time.time(), "at": now_s(), "sessionId": s["id"],
                   "sessionName": (s.get("sessionName") or "")[:80], "requestId": rid,
                   "tool": tool, "detail": json.dumps(req, ensure_ascii=False)[:600],
                   "action": "approve" if not dry else "dry-run"}
            if not dry:
                rc2, o2, e2 = future(approve_argv(s["id"], rid))
                rec["rc"] = rc2
                rec["result"] = (o2 or e2).strip()[:200]
                if rc2 == 0:
                    approved += 1
                else:
                    rec["action"] = "failed"
            os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
            store.mirror_at("approvals", store.append_approval_ledger, LEDGER,
                            "approval_ledger", rec)
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(REPORT, "a", encoding="utf-8") as fh:
                fh.write(f"- {rec['at']} `{s['id']}` 请求 `{rid}` 工具 **{tool}** → {rec['action']}\n")
            print(f"{rec['action']}: {s['id']} {rid} {tool}")
    save_cache(fresh)
    if seen == 0:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(why.items())) or "none"
        print(f"nothing pending ({checked} checked: {breakdown})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
