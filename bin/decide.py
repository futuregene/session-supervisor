#!/usr/bin/env python3
"""Wake gate for the session supervisor. No model calls.

Decides whether this scan cycle should wake an agent turn, and if so writes
state/turn_payload.json and spawns bin/wake.sh detached.

Rules: wake on high/medium pending events (respecting gap + hourly/daily caps),
otherwise wake on heartbeat after config.heartbeat_minutes of silence, but only
if the monitor session is idle.
"""
import json
import os
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING = os.path.join(WATCH, "inbox", "pending.jsonl")
WAKES = os.path.join(WATCH, "state", "wakes.jsonl")
PENDING_SEEN = os.path.join(WATCH, "state", "pending_seen.json")
PAYLOAD = os.path.join(WATCH, "state", "turn_payload.json")
MONITOR_FILE = os.path.join(WATCH, "state", "monitor_session.txt")
LOG = os.path.join(WATCH, "logs", "decide.log")
NOTIFY_PRIORITY = {"high", "medium"}
# What the monitor session is called. `monitor_roll.py` creates it with this name; the
# desktop app's first-turn auto-title can overwrite it (see `name_heal_needed`).
MONITOR_NAME = "会话总管"

# Keep only what a turn needs to *triage*. Full event JSON (and especially
# detail.lastAssistantText, which is ~63% of a flood payload) is fetched
# on demand with `bin/inbox.py list --json --limit N`. Measured on the
# 2026-10-15 188-event flood: 246,718 B of payload here, 154,564 B of it
# lastAssistantText, both sent on EVERY wake even when the model never
# opens an event.
TRIAGE_FIELDS = ("id", "kind", "priority", "sessionId", "sessionName", "summary")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# One definition of "which pending events are still saying something true": the fold
# lives with the inbox that stores them, and this gate applies it before building the
# card (`inbox.fold_superseded` has the measurements and the reasoning). `bin/` is not
# a package, so it is imported by path — same pattern as scan.py's goal_sessions.
import inbox as inbox_mod  # noqa: E402

# Locating `future`/`python3`: discovery, not a hardcoded Homebrew path (see toolpath.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path, bash_exe  # noqa: E402
CFG_PATH = config_path()


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [decide] {msg}\n")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return rows


def future(cfg, args, timeout=30):
    exe = cfg.get("future_bin") or "future"
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run([exe] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def streaming_sessions(cfg, sids, cap=8):
    """Subset of `sids` whose session is running a turn right now.

    An `awaiting_user` event means the session stopped and waits for an answer;
    that premise dies the moment the user answers, because the session starts a
    new run. Then there is nothing this facility can do: answering would land
    inside a turn that is already handling the same question, and
    `send.py` would only queue another one behind it. Measured 2026-10-15: after
    the wake for such an event, the user had answered 3 seconds earlier and the
    session was already `isStreaming=true` — the whole turn was spent on a
    premise that no longer held.

    Unreadable status is deliberately *not* treated as busy: waking on an event
    we could not check is the safe direction, and it keeps this from turning a
    transient agent hiccup into silence.
    """
    busy = set()
    for sid in list(sids)[:cap]:
        rc, out, _ = future(cfg, ["session", "status", sid, "--json"], timeout=20)
        if rc != 0:
            continue
        try:
            if json.loads(out).get("isStreaming"):
                busy.add(sid)
        except Exception:  # noqa: BLE001
            continue
    return busy


def has_new_pending(pending_ids, seen_ids):
    """True when a pending event was not weighed by an earlier wake.

    The heartbeat used to be purely clock-driven, so any low-priority event that
    stayed pending woke a turn every `heartbeat_minutes` for as long as it sat
    there. The common case is a session in `awaiting_user` — it waits for the
    *user*, not for this facility, so nothing changes between those wakes.
    Measured 2026-10-15→16: 128 wakes in 24 h, **115 of them heartbeats** like
    that, ≈¥0.24 each → ≈¥28/day spent rediscovering an answer we already had.

    Only a *new* id counts: an event that disappears (we acked it) or an
    unchanged set is not news. `liveness_minutes` is the floor for a long
    silence, so a stale event still gets re-read eventually.
    """
    return bool({str(i) for i in (pending_ids or []) if i}
                - {str(i) for i in (seen_ids or []) if i})


def name_heal_needed(status, want, max_runs=1):
    """True when something renamed the monitor session away from `want`.

    The desktop app titles a session after its **first** run, and its only guard is
    "nobody changed the title while the model was working" — a name we set at creation
    is fair game for it. Measured 2026-10-16 21:27: the roll created the new supervisor
    session as `会话总管`, its first run completed, and the app renamed it to a
    generated topic (`磁盘清理与事件处置`). The user then saw no new `会话总管` in the
    sidebar at all: the only visible session was the archived one.

    Bounded to the first run (`max_runs`) on purpose: that is exactly the window in
    which the app can overwrite a name, so this can never fight a rename the user makes
    later on.
    """
    name = (status or {}).get("sessionName")
    if not name or name == want:
        return False
    try:
        runs = int((status or {}).get("queryCount") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < runs <= int(max_runs)


def main():
    cfg = load_json(CFG_PATH, {})
    if os.path.exists(os.path.join(WATCH, "state", "PAUSE")):
        return 0  # one-switch circuit breaker
    now = time.time()
    monitor = ""
    try:
        monitor = open(MONITOR_FILE, "r", encoding="utf-8").read().strip()
    except FileNotFoundError:
        pass
    if not monitor:
        log("no monitor session registered (state/monitor_session.txt); not waking")
        return 0

    pending = read_jsonl(PENDING)
    # A later event of the same kind supersedes an earlier one for a session: the
    # earlier state cannot still hold, and showing both is what put one session on the
    # card three times (user, 2026-10-16). Folded here, before anything reads the list,
    # so the wake gate, `pendingCount`, and the rendered card all agree on one line per
    # (session, kind); the folded ones are recorded in inbox/handled.jsonl with
    # `foldedInto`. Best-effort: a failure must never stop a wake.
    try:
        kinds = cfg.get("fold_superseded_kinds", list(inbox_mod.FOLD_KINDS))
        pending, folded = inbox_mod.apply_fold(rows=pending, kinds=tuple(kinds or ()))
        if folded:
            log(f"folded {len(folded)} superseded event(s) ({len(pending)} still live): "
                + ", ".join(f"{r.get('id')}->{r.get('foldedInto')}" for r in folded[:6]))
    except Exception as exc:  # noqa: BLE001
        log(f"fold skipped ({type(exc).__name__}: {exc})")
    # An event whose session is busy cannot be acted on (see streaming_sessions).
    # This is *not* a suppression: the event stays in pending.jsonl untouched, so
    # when the session goes idle the next cycle sees it again and we wake. Only
    # the wake itself is skipped, which is what `pending` below drives. Order is
    # pending.jsonl's (oldest first), so the cap below is deterministic.
    sid_order = list(dict.fromkeys(e.get("sessionId") for e in pending if e.get("sessionId")))
    busy = streaming_sessions(cfg, sid_order, cap=int(cfg.get("max_status_checks_per_cycle", 8)))
    if busy:
        kept = [e for e in pending if e.get("sessionId") not in busy]
        log(f"deferred {len(pending) - len(kept)} event(s) in {len(busy)} streaming "
            f"session(s): premise gone, nothing to do this cycle")
        pending = kept
    if len(sid_order) > int(cfg.get("max_status_checks_per_cycle", 8)):
        log(f"{len(sid_order)} sessions have pending events; only the first "
            f"{int(cfg.get('max_status_checks_per_cycle', 8))} were checked for a live run")
    interesting = [e for e in pending if e.get("priority") in NOTIFY_PRIORITY]

    # sessions the anti-loop guard has already spoken into are not worth a wake
    py = sys.executable
    guard = os.path.join(WATCH, "bin", "guard.py")
    suppressed, blocked = [], set()
    for e in interesting:
        if e.get("kind") not in ("awaiting_user", "new_session"):
            continue
        sid = e.get("sessionId")
        if sid in blocked:
            suppressed.append(e["id"])
            continue
        try:
            rc = subprocess.run([py, guard, "check", sid], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).returncode
        except Exception:
            rc = 0
        if rc == 1:
            blocked.add(sid)
            suppressed.append(e["id"])
    if suppressed:
        interesting = [e for e in interesting if e.get("sessionId") not in blocked]
        subprocess.run([py, os.path.join(WATCH, "bin", "inbox.py"), "ack"] + suppressed,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        log(f"guard-suppressed {len(suppressed)} event(s) in {len(blocked)} session(s); acked")
    wakes = read_jsonl(WAKES)
    last_ts = max([float(w.get("ts") or 0) for w in wakes], default=0.0)
    hour_ago, day_ago = now - 3600, now - 86400
    per_hour = sum(1 for w in wakes if float(w.get("ts") or 0) > hour_ago)
    per_day = sum(1 for w in wakes if float(w.get("ts") or 0) > day_ago)

    pending_ids = [e.get("id") for e in pending if e.get("id")]
    fresh = has_new_pending(pending_ids, load_json(PENDING_SEEN, {}).get("ids"))
    gap = float(cfg.get("min_wake_gap_seconds", 150))
    hb = float(cfg.get("heartbeat_minutes", 60)) * 60
    # v4 (user 2026-10-15): "low 有可能也要及时回复，我需要这个 agent 能够主动些" —
    # a *new* low event still deserves a prompt reply, so low has no separate slow
    # schedule: it wakes on the same 5-minute cadence as high/medium. What the
    # user later asked for (2026-10-16: "降到 10 块钱一天") is the opposite of
    # waking on *stale* news, so `low_wake_minutes` now debounces only repeated
    # low-only wakes and `has_new_pending` stops the ones that carry no change at
    # all. Set `low_wake_minutes: 0` to restore the 5-minute-for-everything v4
    # behaviour, at ≈¥28/day.
    low_cadence = float(cfg.get("low_wake_minutes", 0)) * 60
    reason = None
    if interesting and (now - last_ts) >= gap:
        if per_hour < int(cfg.get("max_wakes_per_hour", 8)) and per_day < int(cfg.get("max_wakes_per_day", 60)):
            reason = "events"
        else:
            log(f"event wake suppressed by rate cap (hour={per_hour} day={per_day}) pending={len(pending)}")
    elif pending and (now - last_ts) >= gap:
        # only low-priority events: no dedicated wake (they ride the heartbeat)
        pass
    if reason is None and (now - last_ts) >= hb:
        # User 2026-10-15: keep the 5-minute heartbeat even when idle, because the
        # provider's prompt cache expires in roughly 5–10 minutes. A cached wake
        # (system prompt + tools + this card already in cache) is much cheaper
        # than the next real turn paying cold-cache input. So idle-skip is now
        # opt-in: set config.skip_idle_heartbeat=true to get the quiet behaviour
        # back (`liveness_minutes` is the other, slower knob).
        if not pending and cfg.get("skip_idle_heartbeat", False):
            log(f"heartbeat skipped: idle (pending=0, {int(now - last_ts)}s since last wake; "
                f"skip_idle_heartbeat=true)")
            return 0
        if pending and not fresh:
            # Nothing to reconcile: same events as the last wake weighed. Do not
            # return — the liveness floor below still has to be able to fire.
            log(f"heartbeat skipped: no new pending event ({len(pending_ids)} already "
                f"weighed; {int(now - last_ts)}s since last wake)")
        elif pending and (now - last_ts) < low_cadence:
            # low_wake_minutes debounces *new* low-only events; high/medium never
            # come through here (they take the `events` branch above).
            log(f"heartbeat deferred: low_wake_minutes debounce ({int(low_cadence)}s) "
                f"({int(now - last_ts)}s elapsed)")
        elif per_hour < int(cfg.get("max_wakes_per_hour", 8)) and per_day < int(cfg.get("max_wakes_per_day", 60)):
            reason = "heartbeat"

    # Optional liveness ping: off unless liveness_minutes > 0.
    live = float(cfg.get("liveness_minutes", 0)) * 60
    if reason is None and live > 0 and (now - last_ts) >= live:
        if per_hour < int(cfg.get("max_wakes_per_hour", 8)) and per_day < int(cfg.get("max_wakes_per_day", 60)):
            reason = "liveness"
    if reason is None:
        return 0

    # do not stack turns: only wake an idle monitor session
    rc, out, _ = future(cfg, ["session", "status", monitor, "--json"])
    if rc == 0:
        try:
            state = json.loads(out)
        except Exception:  # noqa: BLE001
            state = {}
        want = cfg.get("monitor_session_name") or MONITOR_NAME
        if name_heal_needed(state, want):
            # Self-heal, once, inside the only window in which it can be needed.
            hrc, _out, herr = future(cfg, ["session", "set", monitor, "--title", want])
            log(f"monitor name was {state.get('sessionName')!r} after its first run; "
                f"renamed back to {want!r} rc={hrc} {herr.strip()[:80]}")
        if state.get("isStreaming"):
            log(f"monitor {monitor} busy; deferring {reason} wake")
            return 0
    else:
        log(f"agent unreachable (rc={rc}); deferring {reason} wake")
        return 0

    # Record what this wake has now weighed, so the same events do not wake us
    # again (has_new_pending). Written only for a wake that really happens: a
    # deferred one must still look new on the next cycle.
    #
    # Order matters: this write used to happen *before* the spawn below, so any
    # failure to start `wake.sh` silently consumed the news — the event was marked
    # "weighed", no turn ran, and only the `liveness_minutes` floor (180 min) could
    # recover it. Measured 2026-10-20: the spawn raised `NameError: name 'BASH'`,
    # the events were marked seen anyway, and the supervisor was never woken between
    # 19:17 and 21:11. Record it only after the spawn returns.

    health = load_json(os.path.join(WATCH, "state", "health.json"), {})
    payload = {
        "wakeReason": reason,
        "wokenAtMs": int(now * 1000),
        "monitorSessionId": monitor,
        "pendingCount": len(pending),
        "pendingHighMedium": len(interesting),
        "wakesLastHour": per_hour,
        "wakesLastDay": per_day,
        "scannerHealth": health,
        # Triage list only: those fields have no newlines and no quotes, so
        # json.dumps keeps them on one line each. Full text lives in
        # inbox/pending.jsonl and is read per-event when actually acting.
        "pendingEvents": [{k: ev.get(k) for k in TRIAGE_FIELDS} for ev in pending[-25:]],
    }
    os.makedirs(os.path.dirname(PAYLOAD), exist_ok=True)
    with open(PAYLOAD, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    # `bash_exe()` — the same discovery `liveness.py` uses — because there is no
    # global `BASH` in this module; the old spelling raised NameError on every wake.
    # `DETACHED_PROCESS` on Windows for the same reason as liveness.start_watchd:
    # `start_new_session` is POSIX-only and silently ignored here, so the turn would
    # inherit the daemon's console and die with it (see docs/gotcha-tools.md D29).
    bash = bash_exe()
    if not bash:
        log("no bash found (PATH, $BASH, or the Git Bash prefixes); cannot wake")
        return 1
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([bash, os.path.join(WATCH, "bin", "wake.sh"), reason], **kwargs)
    except Exception as exc:  # noqa: BLE001
        # Nothing was recorded: the next cycle still sees these events as news.
        log(f"could not start wake.sh ({type(exc).__name__}: {exc}); will retry next cycle")
        return 1

    # Only now is the wake real.
    with open(WAKES, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now, "reason": reason, "pending": len(pending),
                             "highMedium": len(interesting)}) + "\n")
    try:
        with open(PENDING_SEEN, "w", encoding="utf-8") as fh:
            json.dump({"ts": now, "ids": pending_ids}, fh)
    except Exception:  # noqa: BLE001
        log("could not record state/pending_seen.json; heartbeat falls back to no-op wakes")
    log(f"woke supervisor: reason={reason} pending={len(pending)} highMedium={len(interesting)}")
    return 0


if __name__ == "__main__":
    main()
