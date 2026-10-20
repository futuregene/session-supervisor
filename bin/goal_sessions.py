#!/usr/bin/env python3
"""What a `future goal` session is, in one place (2026-10-16).

Four consumers have to agree on this and used to each guess:
`bin/scan.py` (should this session wake the supervisor at all?),
`bin/send.py` (may a message be relayed into it?), and the two selftests.
Getting it wrong has cost both ways — relaying into a worker mixes a second
driver into the goal plane's todo/claim loop, and *waking* for every worker turn
burns the user's credits on sessions nothing can be said to.

Two signals, strongest first (see `docs/gotcha-send.md` A15):

1. **the goal plane bound this session to a worker agent** — `goals/<goal>/
   events.jsonl` records `worker_session_bound` with the session id
   (`orchestration/goal/src/executor.rs`). Exact, durable, and independent of the
   session's name — which the plane lets the supervisor change. Measured
   2026-10-16: it covered all 18 sessions of a real goal, while the name
   signal covered 12 and the system-prompt boundary below covered **none** of them
   (their `appendSystemPrompt` reads back 0 bytes, contra what #1118 suggested).
   Costs one file read per goal, substring-filtered before parsing (~1 ms for
   759 KB), so even the scanner can pay for it;
2. **the boundary is in the session's recorded system prompt** — the goal plane
   appends `compose_goal_boundary()`'s text
   (`orchestration/goal/src/decision/goal_boundary.rs`), which contains
   `BOUNDARY_MARKER`. Readable from `future session status <id> --json`, so it
   works no matter where the goal lives. Costs one call per session, so the
   scanner uses it only where it is already paying for a read — and note that it
   measured empty for every worker seen so far, so it is a fallback, not the
   primary signal;
3. **the session name is the goal objective** — the goal plane names a worker
   `truncate(objective, 60)`. Free (the name is in `session list`), but it only
   sees goals whose `registry.json` this module can find: the fixed `STATE_ROOTS`
   plus `extra_roots` — which is how a caller hands over the **sessions' own cwds**,
   because a goal's registry lives in the goal's state root and a goal may live in
   a worktree. It also sees nothing once the
   session has been renamed (a worker's name is not always the objective).

`supervisor_session()` is the carve-out the whole facility depends on: the
supervisor itself runs under a goal of its own, so a mis-tagged
supervisor must never silence its own wake or relay.
"""
import json
import os

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOUNDARY_MARKER = "deterministic control plane"
# The goal plane records `agent_id -> session_id` here when it binds a worker
# (executor.rs). One exact id per worker, no matter what the session is called.
BOUND_KIND = "worker_session_bound"
# Where a goal's state may live, for the name-based fallback: goals predate the
# recorded append, so their older workers carry no marker. Each root is checked
# for `.future/goal` and the legacy `.future/loop`.
STATE_ROOTS = [WATCH, os.path.expanduser("~")]
# A worker name is `truncate(objective, 60)`; anything shorter cannot be one, and
# the length floor also keeps a short, generic name from matching by accident.
MIN_NAME = 12


def _unique(paths):
    seen, out = set(), []
    for path in paths:
        path = path.strip() if isinstance(path, str) else ""
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _state_dirs(roots):
    """`<root>/.future/goal` and the legacy `<root>/.future/loop`, for each root."""
    for root in roots:
        for state_dir in (".future/goal", ".future/loop"):
            yield os.path.join(root, *state_dir.split("/"))


def goal_entries(roots=None, extra_roots=()):
    """Every goal record we can see, as the registry files carry it.

    A goal's registry sits in the goal's own state root — the directory it runs in,
    which is what a caller passes as `extra_roots` when it learned it from the
    sessions (their cwds). That is what reaches a goal living in a worktree
    that no fixed root covers.
    """
    return _read_registries(_unique(list(roots or STATE_ROOTS) + list(extra_roots or [])))


def _read_registries(roots):
    found = []
    for base in _state_dirs(roots):
        try:
            with open(os.path.join(base, "registry.json"), encoding="utf-8") as fh:
                entries = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(entries, dict):
            entries = entries.get("goals") or []
        for goal in entries if isinstance(entries, list) else []:
            if isinstance(goal, dict):
                found.append(goal)
    return found


def objectives(roots=None, extra_roots=()):
    """Objectives of the goals this module can find (deduplicated, order preserved).

    Deliberately shallow: one `registry.json` per root (plus the legacy
    `.future/loop` name). `extra_roots` is where a caller adds roots it learned from
    the sessions themselves (their cwds). A goal whose registry is nowhere in this
    list is still missed here; `worker_sessions()` and the boundary marker cover
    those.

    Cost is one `open` per distinct root, no subprocess, so a caller may pass every
    session's cwd (~50 paths, mostly `ENOENT`).
    """
    seen, out = set(), []
    for goal in goal_entries(roots, extra_roots):
        objective = (goal.get("objective") or "").strip()
        if objective and objective not in seen:
            seen.add(objective)
            out.append(objective)
    return out


def worker_sessions(roots=None, extra_roots=()):
    """Session ids the goal plane has bound to a worker agent.

    Read from `goals/<goal>/events.jsonl`, which records one `worker_session_bound`
    event per worker. Cheap because the lines are filtered by substring before they
    are parsed (759 KB in 1 ms).

    Why this is the signal that matters: the other two both failed for
    a goal while its workers kept waking the supervisor — the sessions
    had been renamed (so the name no longer looked like the objective) and their
    `appendSystemPrompt` read back empty (so the boundary marker was not there
    either). This mapping is exact and does not care about either.
    """
    found = set()
    for base in _state_dirs(_unique(list(roots or STATE_ROOTS) + list(extra_roots or []))):
        goals_dir = os.path.join(base, "goals")
        try:
            names = sorted(os.listdir(goals_dir))
        except OSError:
            continue
        for name in names:
            try:
                with open(os.path.join(goals_dir, name, "events.jsonl"),
                          encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if BOUND_KIND not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("kind") == BOUND_KIND and event.get("session_id"):
                            found.add(str(event["session_id"]))
            except OSError:
                continue
    return found


def name_matches(name, objectives_):
    """True when `name` is how the goal plane would name a worker of one of these.

    The plane names a worker after the objective, shortened to 60 characters **with
    a trailing ellipsis** (`truncate(objective, 60)`), so the name is the objective's
    first 59 characters plus `…` — neither string is a prefix of the other until that
    suffix is removed. Missing this kept the fallback from ever matching a long
    objective: measured once, thirteen workers of the same goal carried
    names ending in `…` and were all read as "not a goal session".
    """
    name = (name or "").strip()
    for suffix in ("…", "..."):
        if name.endswith(suffix):
            name = name[: -len(suffix)].rstrip()
            break
    if len(name) < MIN_NAME:
        return False
    return any(objective.startswith(name) or name.startswith(objective)
               for objective in objectives_)


def owned(status=None, objectives_=None, roots=None, extra_roots=(), workers=None, sid=""):
    """`(True, why)` when this session was started by `future goal`.

    `status` is a `future session status --json` payload (or just the fields a
    caller happens to have: `sessionName` alone is enough for the name signal).
    `workers` is `worker_sessions()`' result, passed in when the caller already has
    it (the scanner pays for it once per cycle, not once per session).
    """
    sid = sid or (status or {}).get("sessionId") or ""
    if sid and workers and sid in workers:
        return True, "the goal plane bound this session to a worker agent"
    append = (status or {}).get("appendSystemPrompt") or ""
    if BOUNDARY_MARKER in append:
        return True, "its system prompt carries the goal boundary"
    if objectives_ is None:
        objectives_ = objectives(roots, extra_roots)
    if name_matches((status or {}).get("sessionName") or "", objectives_):
        return True, "its name is a goal objective"
    return False, ""


def supervisor_session():
    """The id of the live supervisor conversation, or `""`.

    The supervisor runs under a goal of its own, so every consumer must carve it
    out: a mis-tagged supervisor would silence the channel the facility runs on.
    """
    try:
        with open(os.path.join(WATCH, "state", "monitor_session.txt"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def config_flag(cfg, key, default=True):
    """`cfg[key]` when it is a bool, else `default`."""
    value = (cfg or {}).get(key, default)
    return value if isinstance(value, bool) else default
