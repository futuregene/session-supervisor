#!/usr/bin/env python3
"""FutureOS session scanner — deterministic, NO model calls.

One scan: diff every session's live state against the last scan, classify whether
a session needs the supervisor's attention, and append events to inbox/pending.jsonl.
Cheap enough to run every 30 seconds.

  scan.py [--json]
"""
import json
import os
import random
import string
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# "Is this a `future goal` session?" lives in one place, shared with bin/send.py:
# the user asked (2026-10-16) that goal-launched workers neither receive relay
# messages nor wake the supervisor at all.
import goal_sessions  # noqa: E402
# Scheduled-task runs of tasks that delete their session: the only channel that can
# turn "that hourly task ran again" into a review, instead of a memory (bin/taskruns.py).
import taskruns  # noqa: E402

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path  # noqa: E402
CFG_PATH = config_path()
STATE_PATH = os.path.join(WATCH, "state", "state.json")
HEALTH_PATH = os.path.join(WATCH, "state", "health.json")
PENDING_PATH = os.path.join(WATCH, "inbox", "pending.jsonl")

Q_MARKERS = ("请确认", "请提供", "需要你", "要不要", "是否要", "你希望",
             "请告知", "等你决定", "由你决定", "请选择", "需要我", "可以吗",
             "麻烦你", "问一下你", "想确认", "你倾向", "按哪个")


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [scan] {msg}", flush=True)


def _expand_watch(path):
    """`${WATCH}` -> this checkout, so config never has to name a home directory.

    The supervisor must not scan its own sessions, and that exclusion used to be
    spelled as one machine's absolute path in bin/config.json. The token keeps the
    intent (exclude myself) while the machine stays out of the file.
    """
    return str(path).replace("${WATCH}", WATCH).replace("$WATCH", WATCH)


def cmp_path(path):
    """One comparable spelling of a directory, for prefix checks.

    The same checkout comes back as `D:\\repo` from `session list` and `D:/repo`
    from `session new` (and one command expands `~` where another does not), so a
    raw `startswith` against a config prefix misses exactly the sessions the
    prefix was meant to exclude — on this host it missed the supervisor's own
    session, which is the one loop the exclusion exists to prevent.
    """
    return os.path.normcase(os.path.normpath(str(path or ""))) if path else ""


def cwd_excluded(cwd, prefixes):
    """True when `cwd` is at or under one of the excluded prefixes."""
    if not cwd:
        return False
    here = cmp_path(cwd)
    for p in prefixes:
        pre = cmp_path(p)
        if pre and (here == pre or here.startswith(pre + os.sep)):
            return True
    return False


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def event_key(kind, key_extra, stamp, stable=False):
    """The dedupe key of one emitted event.

    `stamp` is the session's *last activity*, so it moves every time the session
    appends anything. A key built on it therefore re-fires for an event that has
    not changed at all. That is only right for events whose identity IS "the
    session moved on" (`awaiting_user`, `new_session`, ...). When `key_extra` is
    itself the identity of the event — a run id, where one run is one report —
    pass `stable=True`, or the same failure is reported again for every later
    record in that session (2026-10-20: one 502 was reported four times in
    eleven minutes, each a high-priority wake).
    """
    return f"{kind}|{'stable' if stable else stamp}|{key_extra}"


def future(cfg, args, timeout=120):
    exe = future_exe(cfg.get("future_bin"))
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run([exe] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def new_event_id():
    return "ev_" + time.strftime("%Y%m%d-%H%M%S") + "_" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=4))


def tail_entries(cfg, sid, total, n=4):
    cursor = max(0, int(total) - n)
    rc, out, _ = future(cfg, ["session", "transcript", "--session", sid,
                              "--cursor", str(cursor), "--limit", str(n), "--json"])
    if rc != 0:
        return []
    try:
        return json.loads(out).get("entries", []) or []
    except Exception:
        return []


def last_assistant_text(entries):
    for e in reversed(entries):
        if e.get("kind") == "assistant":
            txt = " ".join(b.get("text", "") for b in e.get("blocks", []) if b.get("kind") == "text")
            return e.get("ordinal"), txt.strip()
    return None, ""


def looks_like_question(text):
    """Only the trailing part counts: a session that ends by asking something has the
    question at the end. A bare '?'/'？' anywhere would also match table headers and
    headings, which produced false positives."""
    tail = text.strip()[-400:]
    if not tail:
        return False
    if not any(m in tail for m in Q_MARKERS):
        return False
    # require it to actually end question-like: a marker near the end, or the last
    # line ends with a question mark
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    return last.endswith(("?", "？")) or any(m in last for m in Q_MARKERS)


def skip_goal_session(sid, name, objectives, supervisor, workers=()):
    """True when this session belongs to the `future goal` control plane.

    Kept pure and cheap on purpose. The scanner sees every session every ~30 s, so
    it uses the signals that cost no per-session call:

    1. `workers` — session ids the plane recorded as bound workers
       (`goal_sessions.worker_sessions()`). Exact, and the only one of the three
       that survives a worker being renamed: measured 2026-10-16, the name signal
       covered 12 of 18 sessions of a live goal and the boundary marker none, so
       named-only gating kept waking the supervisor for six workers;
    2. the name — the plane names a worker `truncate(objective, 60)`, and the
       objectives come from the registries under `goal_sessions.STATE_ROOTS`.

    The supervisor is never skipped: it runs under a goal of its own, and skipping
    it would mean the facility stops waking itself.
    """
    if sid and supervisor and sid == supervisor:
        return False
    if sid and sid in set(workers or ()):
        return True
    return goal_sessions.name_matches(name, objectives or [])
    if sid and supervisor and sid == supervisor:
        return False
    return goal_sessions.name_matches(name, objectives or [])


def list_shrank(prev_total, total, threshold=0.4):
    """True when this scan sees far fewer sessions than the previous one.

    Incident 2026-10-15 17:25: `future session list --json` started returning 60
    sessions (always the ones touched since 02:04), immediately after one
    `Timeout expired`, and never recovered. Nothing in this repo could have
    noticed: the scan simply stopped watching ~200 sessions and kept reporting
    `ok=true`. So treat a sharp drop as a known-degraded scan rather than news.

    An unknown total on either side (0/None/empty) is never a drop: a missing
    count must not read as "everything disappeared".

    `threshold` is the fraction that may disappear before it counts as a drop.
    """
    try:
        prev_total = int(prev_total or 0)
        total = int(total or 0)
    except (TypeError, ValueError):
        return False
    if prev_total <= 0 or total <= 0 or total >= prev_total:
        return False
    return total < prev_total * (1.0 - threshold)


def peak_degraded(peak, total, threshold=0.4):
    """True while the list stays far below the highest count ever seen.

    `list_shrank()` alone only fires on the transition scan, and the very next
    scan compares 60 against 60 and reports a healthy `ok=true` again — which is
    exactly how the 2026-10-15 17:25 outage could have stayed invisible. The peak
    is remembered in `state/state.json`, so the warning persists until the list
    recovers (or the peak is explicitly adopted with `--adopt-peak`).

    Sessions are not deleted in normal operation, so a long-term level far below
    the peak means the agent is still returning less than it has.
    """
    try:
        peak = int(peak or 0)
        total = int(total or 0)
    except (TypeError, ValueError):
        return False
    if peak <= 0 or total <= 0 or total >= peak:
        return False
    return total < peak * (1.0 - threshold)


def approvals(cfg, sid):
    rc, out, _ = future(cfg, ["session", "approvals", sid, "--json"], timeout=30)
    if rc != 0:
        return []
    try:
        return json.loads(out).get("pendingApprovals", []) or []
    except Exception:
        return []


def main():
    cfg = load_json(CFG_PATH, {})
    state = load_json(STATE_PATH, {"sessions": {}})
    sessions_state = state.setdefault("sessions", {})
    now = time.time()
    now_ms = int(now * 1000)
    events = []

    rc, out, err = future(cfg, ["session", "list", "--json"])
    if rc != 0:
        log(f"session list failed rc={rc}: {err.strip()[:200]}")
        save_json(HEALTH_PATH, {"ts": now, "ok": False, "error": f"session list rc={rc}", "stderr": err[:500]})
        return 1
    try:
        all_sessions = json.loads(out).get("sessions", [])
    except Exception as exc:  # noqa: BLE001
        log(f"session list parse failed: {exc}")
        return 1

    # A shrunken list means the agent returned less than it has. Say so loudly and
    # keep saying it (see `peak_degraded`) until the list recovers, and keep the
    # previous dedup state meanwhile, so a gap in the list is never later read as
    # "this session reappeared" (which would fire a false `new_session`).
    prev_health = load_json(HEALTH_PATH, {}) or {}
    peak = int(state.get("peakSessions") or 0)
    if len(all_sessions) >= peak:
        # a count at or above the remembered peak is the new normal
        state["peakSessions"] = len(all_sessions)
        state["peakSessionsTs"] = now
        peak = len(all_sessions)
    shrank_now = list_shrank(prev_health.get("sessionsTotal"), len(all_sessions))
    degraded = ""
    if shrank_now:
        degraded = (f"session list shrank {prev_health.get('sessionsTotal')} -> "
                    f"{len(all_sessions)} (agent returned fewer sessions than it has)")
    elif peak_degraded(peak, len(all_sessions)):
        since = state.get("peakSessionsTs")
        degraded = (f"session list still far below the peak seen ({len(all_sessions)} < "
                    f"{peak}" + (f" since {time.strftime('%F %T', time.localtime(since))}"
                                 if since else "") + ")")
    if degraded:
        log(f"degraded: {degraded}")

    if "--adopt-peak" in sys.argv:
        state["peakSessions"] = len(all_sessions)
        state["peakSessionsTs"] = now
        save_json(STATE_PATH, state)
        log(f"adopted peak: peakSessions={len(all_sessions)} (explicit)")
        return 0

    exclude = set(cfg.get("exclude_sessions") or [])
    exclude_cwd = [_expand_watch(p) for p in (cfg.get("exclude_cwd_prefix") or [])]
    lookback_ms = int(cfg.get("lookback_hours", 72) * 3600 * 1000)
    stall_ms = int(cfg.get("stall_minutes", 25) * 60 * 1000)
    max_deep = int(cfg.get("max_deep_checks_per_scan", 40))
    fresh_ms = int(cfg.get("fresh_window_hours", 24) * 3600 * 1000)
    max_events = int(cfg.get("max_events_per_scan", 25))
    resync_gap = float(cfg.get("resync_gap_seconds", 300))

    baseline = "--baseline" in sys.argv
    # A long gap (daemon restart, sleep, agent outage) makes every session look
    # changed at once. Treat it as a resync: silently re-baseline and only report
    # genuinely urgent kinds, so a restart cannot flood the turn with history.
    last_scan = float(state.get("lastScanTs") or 0)
    # An event whose own freshness stamp predates the last scan we ran can only
    # mean the baseline was rewound (measured 2026-10-15: state.json was tracked
    # then, and a concurrent `git reset --hard` in the main checkout restored a
    # morning-era baseline whose `lastScanTs` was still recent, so the file-based
    # `resync` below stayed false and ~30 historical events re-fired). The
    # per-event check in emit() catches that case.
    resync = (not baseline) and last_scan > 0 and (now - last_scan) > resync_gap
    if resync:
        log(f"resync scan (gap {now - last_scan:.0f}s > {resync_gap:.0f}s): only urgent events")
        # 2026-10-15: this fired with a ~30 minute gap while scan.log showed a tick
        # every ~37s. Either the state write silently did not land for a while, or
        # the daemon was frozen. Log wall-clock both sides so the next occurrence is
        # diagnosable from the log alone (the resync itself is harmless — it only
        # re-baselines and reports urgent events).
        log(f"resync detail: lastScanTs={time.strftime('%F %T', time.localtime(last_scan))} "
            f"now={time.strftime('%F %T', time.localtime(now))} "
            f"state_mtime={time.strftime('%F %T', time.localtime(os.path.getmtime(STATE_PATH))) if os.path.exists(STATE_PATH) else 'missing'}")
    HIGH_KINDS = {"failed_run", "run_error", "approval_pending", "stalled"}
    # Sessions the previous scan deferred (deep-check budget). Filled in below,
    # before the per-session loop; emit() needs it to avoid suppressing them.
    deferred_prev_set = {x for x in (state.get("deferred") or [])}

    def emit(rec, base, kind, priority, summary, detail, key_extra="", stamp=None,
             fresh=None, stable=False):
        key = event_key(kind, key_extra,
                        stamp if stamp is not None else base["updatedAtMs"], stable)
        seen = rec.setdefault("emitted", [])
        if key in seen:
            return
        seen.append(key)
        del seen[:-25]
        if baseline:
            return
        # Stale-baseline guard: an event whose own freshness stamp predates our
        # last scan is a rewind, not news (see the note in main()). BUT a session
        # deferred by the previous scan's deep-check budget legitimately carries a
        # stamp older than lastScanTs — this scan is its turn — so never suppress
        # for those, or deferral would silently drop events.
        if ((stamp is not None) and last_scan > 0 and stamp < last_scan * 1000 - 60_000
                and base["sessionId"] not in deferred_prev_set):
            rec.setdefault("rewound", []).append(kind)
            return
        if resync and kind not in HIGH_KINDS:
            return
        if bulk and not base["isStreaming"]:
            return  # not news: every session was touched by a bulk refresh
        if fresh is not None and (now_ms - int(fresh)) > fresh_ms:
            return  # the session's real last activity is stale: not news
        if len(events) >= max_events:
            return  # per-scan cap
        ev = dict(base)
        ev.update({"id": new_event_id(), "kind": kind, "priority": priority,
                   "summary": summary, "detail": detail or {}})
        events.append(ev)
        log(f"event {kind}/{priority} {base['sessionId']} :: {summary[:80]}")

    skip_goal = goal_sessions.config_flag(cfg, "scan_skip_goal_sessions", True)
    # A goal's registry lives in the goal's state root, and *that is usually the cwd
    # of the sessions it drives* — including worktrees, where the fixed STATE_ROOTS
    # look in the wrong place. Measured 2026-10-16: ten workers of
    # a goal whose workers lived in a worktree) were invisible to
    # this gate, so the user's "唤醒也免除" ask was not in force for exactly the
    # sessions it was written for. Reading each candidate's own
    # `<cwd>/.future/goal/registry.json` costs one `open` per distinct cwd and no
    # subprocess, so the scan can afford it where it cannot afford the strong
    # signal (the boundary recorded in each session's system prompt).
    goal_objectives = goal_sessions.objectives(extra_roots=[s.get("cwd") for s in all_sessions]) if skip_goal else []
    # The exact signal: the plane's own agent -> session binding. Reading it costs
    # one substring-filtered pass per goal's events file (~1 ms per 760 KB), which
    # a scan every 30 s can afford, and unlike the name it survives a rename.
    goal_workers = (goal_sessions.worker_sessions(extra_roots=[s.get("cwd") for s in all_sessions])
                    if skip_goal else set())
    if skip_goal:
        log(f"goal gate: {len(goal_objectives)} objective(s), {len(goal_workers)} bound worker session(s)")
    supervisor = goal_sessions.supervisor_session() if skip_goal else ""
    if skip_goal and not supervisor:
        # The carve-out cannot be applied without it, and the supervisor runs under
        # a goal of its own — a supervisor named after that objective would skip
        # itself. Say so instead of skipping in the dark.
        log("warning: no supervisor session id (state/monitor_session.txt unreadable);"
            " the goal-skip carve-out is not in force this scan")
    goal_skipped = []

    active, deep = [], 0
    for s in all_sessions:
        sid = s.get("id")
        if not sid or sid in exclude:
            continue
        if cwd_excluded(s.get("cwd"), exclude_cwd):
            continue
        if skip_goal and skip_goal_session(
                sid, s.get("sessionName") or s.get("firstMessage") or "",
                goal_objectives, supervisor, goal_workers):
            # Not just "no relay": the user asked that these sessions not wake the
            # supervisor either, because the goal plane already drives them (one
            # todo per turn) and every worker turn would otherwise cost a wake.
            goal_skipped.append(sid)
            continue
        if s.get("isStreaming") or (now_ms - int(s.get("updatedAtMs") or 0)) <= lookback_ms:
            active.append(s)
    if goal_skipped:
        # Visible, not silent: a skipped session must never look like a healthy one.
        log(f"skipped {len(goal_skipped)} goal session(s) "
            f"({', '.join(sorted(goal_skipped)[:3])}{'…' if len(goal_skipped) > 3 else ''})")

    # fairness: sessions deferred by last scan's budget go first, else newest first
    # A DB-wide refresh of updated_at (agent/app restart, maintenance) makes every
    # session look changed at once. Detect that breadth and treat it as a
    # re-baseline rather than news; only a streaming session is still interesting.
    changed_pre = sum(
        1 for _s in active
        if (sessions_state.get(_s["id"]) or {}).get("sig")
        != f"{int(_s.get('updatedAtMs') or 0)}|{int(bool(_s.get('isStreaming')))}")
    bulk = len(active) > 20 and changed_pre > 0.5 * len(active)
    if bulk:
        log(f"bulk timestamp refresh suspected ({changed_pre}/{len(active)} changed): "
            f"only streaming-session events")

    deferred_prev = [x for x in (state.get("deferred") or []) if x in {s["id"] for s in active}]
    deferred_now = []
    prev_set = set(deferred_prev)
    # Order of work, cheapest information first:
    #   1. a session we have never seen (its `new_session` announcement is owed and
    #      its first read is the most informative);
    #   2. a session the last scan deferred — it was changed and never examined, and
    #      it consumes a deep check even when unchanged (`prev_set` below), so it
    #      must not sit behind an unbounded queue of untouched sessions;
    #   3. everything else, newest first.
    def first_sighting(s):
        return "sig" not in (sessions_state.get(s["id"]) or {})
    by_newest = lambda s: -int(s.get("updatedAtMs") or 0)  # noqa: E731
    ordered = (sorted([s for s in active if first_sighting(s)], key=by_newest)
               + sorted([s for s in active if s["id"] in prev_set and not first_sighting(s)],
                        key=by_newest)
               + sorted([s for s in active if s["id"] not in prev_set and not first_sighting(s)],
                        key=by_newest))

    for s in ordered:
        sid = s["id"]
        upd = int(s.get("updatedAtMs") or 0)
        streaming = bool(s.get("isStreaming"))
        rec = sessions_state.setdefault(sid, {})
        sig = f"{upd}|{int(streaming)}"
        changed = rec.get("sig") != sig
        is_new = "sig" not in rec
        base = {
            "sessionId": sid,
            "sessionName": (s.get("sessionName") or s.get("firstMessage") or "")[:120],
            "cwd": s.get("cwd"),
            "model": s.get("model"),
            "queryCount": s.get("queryCount"),
            "isStreaming": streaming,
            "updatedAtMs": upd,
            "detectedAtMs": now_ms,
        }

        # stall detection must run even when nothing changed
        if streaming:
            if rec.get("lastUpdMs") != upd or not rec.get("streamingSinceMs"):
                rec["streamingSinceMs"] = now_ms
            elif now_ms - int(rec["streamingSinceMs"]) > stall_ms:
                emit(rec, base, "stalled", "medium",
                     "streaming 中但长时间没有新记录，可能卡住",
                     {"stalledMs": now_ms - int(rec["streamingSinceMs"])}, key_extra=str(upd),
                     stamp=rec.get("lastUpdMs") or upd)
        else:
            rec.pop("streamingSinceMs", None)
        rec["lastUpdMs"] = upd

        if not changed and sid not in prev_set:
            # Nothing about this session moved. Deferred sessions are the
            # exception: last scan gave them no turn, so this scan owes them one
            # even when their signature is unchanged (that is what `deferred` is
            # for).
            continue
        if deep >= max_deep:
            # Out of budget. Record the signature we just observed anyway: `sig`
            # means "we have seen this session", not "we deep-checked it", and
            # without it a session first seen while deferred looked brand new on
            # the scan that finally reached it — up to `max_events_per_scan` false
            # `new_session` events at a time (measured 2026-10-15 with 257 sessions
            # and a deep budget of 5: 5 per scan, marching through the deferred
            # list). A session that really is new keeps its announcement, deferred
            # until the scan that can read it (`pendingNew`).
            rec["sig"] = sig
            if is_new:
                rec["pendingNew"] = True
            deferred_now.append(sid)
            continue
        deep += 1

        total, failed, last_status, last_run_id = 0, 0, None, None
        rc, out, _ = future(cfg, ["session", "transcript", "--session", sid, "--runs", "--json"])
        if rc == 0:
            try:
                d = json.loads(out)
                total = int(d.get("scannedEntries") or 0)
                led = d.get("ledger") or []
                # `failedRuns` in the agent's payload counts `cancelled` too, so a user
                # who stops and re-sends looks like a session that keeps failing
                # (measured: 4 cancelled + 2 completed reported as "累计 4 次").
                # Derive from the ledger and only count real failures.
                REAL_FAILURES = ("failed", "error", "aborted")
                if led:
                    failed = sum(1 for r in led if r.get("status") in REAL_FAILURES)
                    last_status = led[-1].get("status")
                    last_run_id = led[-1].get("runId")
                else:
                    failed = int(d.get("failedRuns") or 0)
            except Exception:
                pass
        entries = tail_entries(cfg, sid, total) if total else []
        last_kind = entries[-1]["kind"] if entries else None
        last_ord = entries[-1]["ordinal"] if entries else None
        a_ord, a_text = last_assistant_text(entries)
        # the session's *real* last activity, not the DB's updated_at (which can be
        # bumped in bulk, e.g. by a restart)
        lastMs = max([int(e.get("createdAtMs") or 0) for e in entries], default=0) or upd

        if (is_new or rec.pop("pendingNew", None)) and not streaming:
            emit(rec, base, "new_session", "low",
                 f"新会话：{base['sessionName'][:60]}",
                 {"lastKind": last_kind}, key_extra=str(upd), stamp=lastMs, fresh=lastMs)

        prev_failed = rec.get("failedRuns")
        if failed and (prev_failed is None or failed > int(prev_failed)):
            emit(rec, base, "failed_run", "high",
                 f"出现失败的运行（累计 {failed} 次）",
                 {"failedRuns": failed, "lastRunStatus": last_status}, key_extra=str(failed),
                 stamp=lastMs, fresh=lastMs)
        if last_status in ("failed", "error", "aborted"):
            # Keyed by the run id, not the session's last ordinal: with the ordinal,
            # appending any user message re-fired the same failure (the same run was
            # reported 4 times in one morning). One run = one report — `stable=True`
            # is what makes that true, since `lastMs` moves with every later record
            # in the same session (2026-10-20: four reports of one 502 in 11 min).
            emit(rec, base, "run_error", "high",
                 f"最近一次运行状态：{last_status}",
                 {"lastRunStatus": last_status, "runId": last_run_id},
                 key_extra=str(last_run_id or last_ord),
                 stamp=lastMs, fresh=lastMs, stable=True)
        rec["failedRuns"] = failed

        appr = approvals(cfg, sid) if (streaming or last_kind == "assistant") else []
        if appr:
            emit(rec, base, "approval_pending", "high",
                 f"有 {len(appr)} 个待批准的工具权限请求",
                 {"approvals": appr}, key_extra=str(last_ord))

        if (not streaming) and last_kind == "assistant" and total:
            q = looks_like_question(a_text)
            emit(rec, base, "awaiting_user", "medium" if q else "low",
                 "已停下，等待用户回复" + ("（疑似提问）" if q else ""),
                 {"lastAssistantOrdinal": a_ord, "questionLike": q,
                  "lastAssistantText": a_text[-1500:]}, key_extra=str(last_ord),
                 stamp=lastMs, fresh=lastMs)

        rec["sig"] = sig
        rec["lastOrdinal"] = last_ord
        rec["lastKind"] = last_kind

    if deferred_now:
        log(f"deep-check budget reached ({max_deep}); deferring {len(deferred_now)} sessions to next scan")
    state["deferred"] = deferred_now

    # --- scheduled-task runs ------------------------------------------------
    # A task with `session retention = delete` settles and its conversation is gone
    # before the next scan (30 s apart), so nothing above can ever see it: measured
    # 2026-10-21, 8 of 13 tasks work that way and the hourly dev-PR task had 9 runs
    # that no event had ever mentioned. Its ledger survives, so this is the one place
    # where a *review* can be triggered rather than hoped for. `taskruns.collect`
    # throttles itself (`task_poll_seconds`), baselines a task on first sight, and
    # never emits twice for one run id.
    task_events = []
    if not baseline and cfg.get("task_watch_enabled", True):
        try:
            task_events, tr_state = taskruns.collect(cfg, now_ms=now_ms)
            taskruns.save_json(taskruns.STATE_PATH, tr_state)
            if tr_state.get("lastError"):
                log(f"task poll: {tr_state['lastError']}")
        except Exception as exc:  # noqa: BLE001
            log(f"task poll skipped ({type(exc).__name__}: {exc})")
        room = max_events - len(events)
        for ev in task_events[:max(room, 0)]:
            events.append(ev)
            log(f"event {ev['kind']}/{ev['priority']} task:{ev.get('sessionName')} :: {ev['summary'][:80]}")
        if len(task_events) > max(room, 0):
            log(f"task events dropped by per-scan cap: {len(task_events) - max(room, 0)}")
    state["lastScanTs"] = now

    # prune very old session state (but never while the list is degraded: a session
    # missing from the list must not be aged out and then re-announced as new)
    cutoff = now_ms - max(lookback_ms * 5, 30 * 24 * 3600 * 1000)
    if not degraded:
        for sid in list(sessions_state):
            if int(sessions_state[sid].get("lastUpdMs") or 0) < cutoff:
                del sessions_state[sid]

    if events:
        os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
        with open(PENDING_PATH, "a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    save_json(STATE_PATH, state)
    health = {"ts": now, "ok": True, "baseline": baseline, "resync": resync, "bulk": bulk,
              "sessionsTotal": len(all_sessions),
              "active": len(active), "deepChecked": deep, "deferred": len(deferred_now),
              "events": len(events), "taskEvents": len(task_events)}
    if degraded:
        health["degraded"] = degraded
    save_json(HEALTH_PATH, health)
    log(f"sessions={len(all_sessions)} active={len(active)} deep={deep} events={len(events)}"
        + (f" DEGRADED" if degraded else ""))
    if "--json" in sys.argv:
        print(json.dumps({"health": health, "events": events}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
