#!/usr/bin/env python3
"""Self-test for `bin/dev_pr.py::decide` — the dev-branch PR policy (no network).

Run: python3 bin/selftest_dev_pr.py   -> prints cases and a mismatch count.

Why this is worth pinning: the branch it manages is one developer's continuous
branch, so the two expensive mistakes are both silent. Merging a red PR puts a
broken `main` in front of every other session; and treating a *squash-merged*
branch as unmerged (the branch's commits are never ancestors of `main`) makes
the supervisor open the same PR again or re-point over work that has not landed.
Both are pure decisions, so they are testable without a repo.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("dev_pr", os.path.join(HERE, "dev_pr.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# (name, facts, expected action)
CASES = [
    # ── nothing to do ──
    ("no branch on the remote", {"branch_exists": False}, "idle"),
    ("branch level with main", {"branch_exists": True, "ahead": 0}, "idle"),
    # ── open PR: order matters ──
    ("work ahead, no PR", {"branch_exists": True, "ahead": 3}, "open_pr"),
    ("open PR, unarmed", {"branch_exists": True, "ahead": 3,
                          "pr": {"number": 7, "mergeState": "CLEAN", "armed": False}},
     "arm"),
    ("open PR, armed, checks running", {"branch_exists": True, "ahead": 3,
                                        "pr": {"number": 7, "mergeState": "BLOCKED",
                                               "armed": True}}, "waiting"),
    ("open PR BEHIND", {"branch_exists": True, "ahead": 3,
                        "pr": {"number": 7, "mergeState": "BEHIND", "armed": True}},
     "fix_behind"),
    # a red check outranks BEHIND: rebasing a red branch just burns another run
    ("open PR red and BEHIND", {"branch_exists": True, "ahead": 3,
                                "pr": {"number": 7, "mergeState": "BEHIND", "armed": False,
                                       "failing": ["Rust build (Linux)"]}}, "report_red"),
    ("open PR red", {"branch_exists": True, "ahead": 3,
                     "pr": {"number": 7, "mergeState": "BLOCKED", "armed": True,
                            "failing": ["Rust tests (Linux)"]}}, "report_red"),
    # ── merged: the squash-merge trap ──
    # ahead>0 is NOT evidence of unmerged work: the branch's commits never become
    # ancestors of main after a squash merge, so only the merged PR says "merged".
    ("merged, branch adds nothing", {"branch_exists": True, "ahead": 5, "same_tree": True,
                                     "last_merged": {"merged": True, "number": 9,
                                                     "in_main": True}}, "repoint"),
    ("merged, newer commits on top", {"branch_exists": True, "ahead": 7, "same_tree": False,
                                      "last_merged": {"merged": True, "number": 9,
                                                      "in_main": True}}, "keep_rebase"),
    # a merged PR whose commit is not in main (deleted? reverted?) is not "merged"
    ("last PR closed, not in main", {"branch_exists": True, "ahead": 2, "same_tree": False,
                                     "last_merged": {"merged": True, "number": 9,
                                                     "in_main": False}}, "open_pr"),
    # ── local-only work outranks everything: it is not ours to push or reset ──
    ("local branch ahead of remote", {"branch_exists": True, "ahead": 2, "local_ahead": 1,
                                      "pr": {"number": 7, "armed": True,
                                             "mergeState": "BLOCKED"}}, "report_local_ahead"),
    ("local ahead and merged", {"branch_exists": True, "ahead": 0, "local_ahead": 2,
                                "last_merged": {"merged": True, "number": 9,
                                                "in_main": True}}, "report_local_ahead"),
]


def main():
    bad = 0
    for name, facts, want in CASES:
        got = m.decide(facts)["action"]
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'ok      ' if ok else 'MISMATCH'} {name:34} -> {got} (want {want})")
    # The verdict must always carry a reason: the journal line is built from it.
    for name, facts, want in CASES:
        v = m.decide(facts)
        if not v.get("reason"):
            bad += 1
            print(f"  MISMATCH {name}: verdict has no reason")
    print(f"cases={len(CASES) * 2} mismatches={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
