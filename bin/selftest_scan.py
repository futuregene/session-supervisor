#!/usr/bin/env python3
"""Self-test for the degraded-scan guard in bin/scan.py (no model calls).

Run: python3 bin/selftest_scan.py   -> prints cases and a mismatch count.

Why: on 2026-10-15 17:25 `future session list --json` started returning 60
sessions instead of 257 and never recovered, and the scan kept reporting
`ok=true` while silently watching ~200 fewer sessions. `list_shrank()` is the
pure decision behind the new `degraded` marker, so it is worth pinning down:
too loose and a normal fluctuation raises a false alarm every turn; too tight
and the real drop goes unnoticed again.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import goal_sessions as gs  # noqa: E402
spec = importlib.util.spec_from_file_location("scan", os.path.join(HERE, "scan.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# (previous total, current total, expected "shrank")
CASES = [
    # the real incident: 257 -> 60 is a drop, and 60 -> 257 is a recovery, not a drop
    (257, 60, True),
    (60, 257, False),
    # steady state and small fluctuation: quiet
    (250, 250, False),
    (250, 254, False),
    (250, 249, False),
    (250, 160, False),   # 36% gone: under the 40% threshold
    (250, 148, True),    # 41% gone: over it
    (100, 59, True),     # exactly 41%
    (100, 61, False),    # exactly 39%
    # degenerate inputs must not raise or fire
    (0, 0, False),
    (None, 60, False),
    (250, None, False),
    ("257", "60", True),  # the CLI side may hand these back as strings
]


# The sticky half. Same incident, one scan later: 60 vs 60 shows no transition,
# yet the list is still nothing like it was.
PEAK_CASES = [
    # (peak seen, current total, expected "still degraded")
    (257, 60, True),    # the incident, every scan after the drop
    (257, 257, False),  # recovered
    (257, 300, False),  # grew past the peak
    (257, 200, False),  # 22% under: within normal churn
    (60, 60, False),    # peak adopted: never flags its own level
    (0, 60, False),     # no peak on record yet
    (None, 60, False),
    (257, None, False),  # unknown count is not evidence of a drop
]


# The goal skip (user, 2026-10-16: goal-launched workers must not wake the
# supervisor at all, only the supervisor itself is exempt). Pure decision, so it is
# pinned here: too loose and a worker turn costs a wake per turn; too tight and the
# supervisor stops waking itself, which is the whole facility.
OBJECTIVES = ["求解 16x16 网格瓷砖铺放问题：每行每列恰有一个单位格不被覆盖",
              "把一个 README 总结成可验证的摘要"]
GOAL_CASES = [
    # (sid, name, objectives, supervisor, expected "skip")
    ("s1", "求解 16x16 网格瓷砖铺放问题：每行每列恰有一", OBJECTIVES, "me", True),
    ("s1", "把一个 README 总结成可验证的摘要", OBJECTIVES, "me", True),
    ("s1", "本地重启脚本与移动端重连", OBJECTIVES, "me", False),
    ("s1", "", OBJECTIVES, "me", False),
    ("s1", None, OBJECTIVES, "me", False),
    # no goals on record: nothing is ever skipped
    ("s1", "求解 16x16 网格瓷砖铺放问题：每行每列恰有一", [], "me", False),
    # a short name must never match by accident
    ("s1", "短", OBJECTIVES, "me", False),
    # the carve-out: the supervisor runs under a goal of its own
    ("me", "求解 16x16 网格瓷砖铺放问题：每行每列恰有一", OBJECTIVES, "me", False),
    # ... and it is not carved out for anyone else
    ("other", "求解 16x16 网格瓷砖铺放问题：每行每列恰有一", OBJECTIVES, "me", True),
]


def main():
    bad = 0

    # A goal's registry lives in the goal's own state root — which is the cwd of the
    # sessions it drives, and that may be a *worktree*. Ten workers of such a goal
    # were invisible to the fixed roots, so the skip did not cover them. The cwd
    # must therefore reach `objectives()` as an extra root.
    print("the cwd fallback: a goal that lives in a worktree")
    tmp = tempfile.mkdtemp(prefix="selftest-goal-roots-")
    goal_root = os.path.join(tmp, "repo", ".worktrees", "some-goal")
    os.makedirs(os.path.join(goal_root, ".future", "goal"))
    objective = "把所有模块的测试覆盖率提升到 100%（口径 A）"
    with open(os.path.join(goal_root, ".future", "goal", "registry.json"), "w") as fh:
        json.dump([{"objective": objective, "cwd": goal_root}], fh)
    worker_name = objective[:60]  # the plane truncates the objective to 60 chars
    ellipsized = objective[:59] + "…"  # ... and marks the cut with an ellipsis
    fixed_only = gs.objectives(roots=[tmp])
    with_cwd = gs.objectives(roots=[tmp], extra_roots=[goal_root])

    # The exact signal: the plane records agent -> session when it binds a worker.
    # It must be found without any session cwd being handed in (the goal's own
    # recorded cwd leads to it), must survive a *renamed* worker, and must not be
    # confused by other event kinds or a truncated line.
    print("the bound-worker record: renamed workers are still goal sessions")
    bound_renamed = "20261016-175258-7e4ce8d75ffc4a8ba5c9bf8de548e77f"
    bound_named = "20261016-174511-ebda4d7650a8411785ebff814939092d"
    unrelated = "20261015-022856-812e9e8559b54255b961dd5344dbfe02"
    events_dir = os.path.join(goal_root, ".future", "goal", "goals", "goal_test")
    os.makedirs(events_dir)
    with open(os.path.join(events_dir, "events.jsonl"), "w") as fh:
        fh.write(json.dumps({"kind": "worker_session_bound", "agent_id": "worker-cli",
                             "session_id": bound_renamed}) + "\n")
        fh.write(json.dumps({"kind": "worker_session_bound", "agent_id": "worker-web",
                             "session_id": bound_named}) + "\n")
        fh.write(json.dumps({"kind": "supervisor_registered",
                             "session_id": bound_named}) + "\n")
        fh.write('{"kind": "worker_session_bound", "session_id": TRUNCATED\n')
    # `roots=[tmp]` keeps this hermetic (the default is STATE_ROOTS, i.e. the real
    # home directory); `extra_roots` is the path the scanner uses.
    workers = gs.worker_sessions(roots=[tmp], extra_roots=[goal_root])
    renamed_skipped = m.skip_goal_session(bound_renamed, "CLI 模块测试覆盖率补至 100%",
                                          with_cwd, "me", workers)
    cases = [
        ("fixed roots alone miss it", fixed_only, []),
        ("the session's own cwd finds it", with_cwd, [objective]),
        ("a passing cwd is not looked at twice",
         gs.objectives(roots=[goal_root], extra_roots=[goal_root]), [objective]),
        ("the truncated worker name matches", gs.name_matches(worker_name, with_cwd), True),
        # measured 2026-10-16: the real name is 59 chars plus `…`, and comparing it
        # without stripping that suffix matched nothing (thirteen workers unseen).
        ("the plane's real name (with the ellipsis) matches",
         gs.name_matches(ellipsized, with_cwd), True),
        ("a bare ellipsis is not a name", gs.name_matches("…", with_cwd), False),
        ("a name that is not the objective still does not",
         gs.name_matches("命令行MCP覆盖率提升", with_cwd), False),
        ("a missing cwd is harmless", gs.objectives(roots=[tmp], extra_roots=[None, ""]), []),
        ("both bound workers are found", workers, {bound_renamed, bound_named}),        ("an unrelated session is not in the set", unrelated in workers, False),
        ("no roots pointing at the goal find no workers",
         gs.worker_sessions(roots=[tmp]), set()),
        ("a renamed worker is skipped: its name no longer says "
         "anything, its binding does", renamed_skipped, True),
        ("... but never at the supervisor's expense",
         m.skip_goal_session("me", "whatever", with_cwd, "me", workers), False),
    ]
    for name, got, want in cases:
        tag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"  {tag} {name}")

    for sid, name, objectives, supervisor, want in GOAL_CASES:
        got = m.skip_goal_session(sid, name, objectives, supervisor)
        tag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"  {tag} goal skip {name!r:34} -> {got} (want {want})")
    for prev, total, want in CASES:
        got = bool(m.list_shrank(prev, total))
        if got != want:
            bad += 1
        print(f"  {'ok      ' if got == want else 'MISMATCH'} shrank={got} 期望={want} :: {prev} -> {total}")
    for peak, total, want in PEAK_CASES:
        got = bool(m.peak_degraded(peak, total))
        if got != want:
            bad += 1
        print(f"  {'ok      ' if got == want else 'MISMATCH'} peak_degraded={got} 期望={want} :: peak {peak} vs {total}")
    # `event_key` decides whether an emitted event has already been reported. The
    # bug it fixes (2026-10-20): one failed run was reported four times in eleven
    # minutes because the key carried the session's last-activity stamp, which
    # moves with every later record — the run id alone is the identity.
    print("event_key: a stable identity ignores the moving stamp")
    # (kind, key_extra, stamp) on both sides, so one case can vary the stamp,
    # the run id, or both.
    KEY_CASES = [
        # same run, later activity: one run = one report
        ("run_error", "run_1", 1000, "run_1", 2000, True, True),
        ("run_error", "run_1", 1000, "run_1", 9999, True, True),
        # a different run id is news even with the same stamp
        ("run_error", "run_1", 1000, "run_2", 1000, True, False),
        # the old, buggy shape: the moving stamp splits one run into many events
        ("run_error", "run_1", 1000, "run_1", 2000, False, False),
        # awaiting_user: the stamp IS the identity, so it must still re-fire
        ("awaiting_user", "585", 1000, "585", 2000, False, False),
        ("awaiting_user", "585", 1000, "585", 1000, False, True),
    ]
    for kind, extra_a, stamp_a, extra_b, stamp_b, stable, want in KEY_CASES:
        a = m.event_key(kind, extra_a, stamp_a, stable)
        b = m.event_key(kind, extra_b, stamp_b, stable)
        got = a == b
        if got != want:
            bad += 1
        print(f"  {'ok      ' if got == want else 'MISMATCH'} same_key={got} 期望={want} "
              f":: {kind} {extra_a}@{stamp_a} vs {extra_b}@{stamp_b} stable={stable}")
    if not m.event_key("run_error", "run_1", 1000, True).split("|")[1].isdigit():
        print(f"  ok       stable key carries no stamp :: "
              f"{m.event_key('run_error', 'run_1', 1000, True)}")
    else:
        bad += 1
        print("  MISMATCH stable key still carries a stamp")

    # The exclusion that keeps the supervisor from waking itself. The same checkout
    # comes back as `D:\repo` from `session list` and `D:/repo` from `session new`
    # (measured 2026-10-20 on the first Windows install), so a raw `startswith`
    # missed the supervisor's own session — the one loop the exclusion exists to
    # prevent. The comparison folds separators, and case where the platform does.
    print("cwd_excluded: one spelling of a path, whichever tool produced it")
    SEP = os.sep
    CWD_CASES = [
        ("the prefix is the cwd itself", f"D:{SEP}repo", [f"D:{SEP}repo"], True),
        ("a child of the prefix", f"D:{SEP}repo{SEP}sub", [f"D:{SEP}repo"], True),
        ("the prefix carries a trailing separator",
         f"D:{SEP}repo{SEP}sub", [f"D:{SEP}repo{SEP}"], True),
        ("the cwd carries a trailing separator", f"D:{SEP}repo{SEP}", [f"D:{SEP}repo"], True),
        ("the cwd carries a doubled separator",
         f"D:{SEP}repo{SEP}{SEP}sub", [f"D:{SEP}repo"], True),
        ("a sibling with a shared stem is not excluded", f"D:{SEP}repo2", [f"D:{SEP}repo"], False),
        ("outside the prefix", f"D:{SEP}other{SEP}sub", [f"D:{SEP}repo"], False),
        ("a missing cwd excludes nothing", None, [f"D:{SEP}repo"], False),
        ("an empty prefix list excludes nothing", f"D:{SEP}repo", [], False),
    ]
    if os.name == "nt":
        # Only Windows hands back both separator spellings for one directory, and
        # only Windows folds case — so only Windows can prove these two.
        CWD_CASES += [
            ("the other separator", "D:/repo/sub", ["D:\\repo"], True),
            ("case differs", "d:/REPO/sub", ["D:\\repo"], True),
        ]
    for name, cwd, prefixes, want in CWD_CASES:
        got = m.cwd_excluded(cwd, prefixes)
        if got != want:
            bad += 1
        print(f"  {'ok      ' if got == want else 'MISMATCH'} excluded={got} want={want} :: {name}")

    print(f"cases={len(CASES) + len(PEAK_CASES) + len(KEY_CASES) + len(CWD_CASES)} mismatches={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
