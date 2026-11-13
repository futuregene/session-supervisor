#!/usr/bin/env python3
"""Rescue branches whose tip exists only on this machine.

Why this exists
---------------
A finished piece of work repeatedly lived only as a local branch in a
`<repo>/.worktrees/` checkout while the session that made it was ending:

  * one commit stayed reachable only through `git rev-parse --verify` — no ref
    pointed at it, so one `git gc --prune` would have lost it;
  * several branches were unpushed when their sessions stopped, and one of them
    was the firmware already flashed on the board.

Which branches and which repo is deployment detail, recorded in `journal/` and
`.future/memory/`, not here.

A session ending removes its worktree, so "the branch is local" plus "the
session stopped" is a loss waiting to happen. Pushing a branch is the cheapest
irreversible-free safety net we have: it never rewrites history and the ref can
be deleted again in one command.

What it does
------------
For every branch in `refs/heads` (except protected names), decide whether its
tip is reachable from *some* remote branch. Unreachable means unpushed work.
Then, with `--push`, push it to that remote — but never with `--force`, and
never when the remote already has that branch name at a different sha (that is a
divergence a human should look at, not something to overwrite).

Read-only unless `--push` is given.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Pushing to a shared branch is a different decision (POLICY.md §B); these are
# reported, never pushed.
PROTECTED = ("main", "master", "dev", "dev-huichen", "gh-pages")


def git(repo: str, *args: str) -> tuple[int, str, str]:
    p = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def local_branches(repo: str) -> list[tuple[str, str]]:
    code, out, err = git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    if code != 0:
        raise SystemExit(f"cannot list branches in {repo}: {err}")
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def remote_remotes(repo: str) -> list[tuple[str, str]]:
    """(remote, remote_branch) pairs we have refs for locally."""
    code, out, _ = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes")
    pairs = []
    if code != 0:
        return pairs
    for ref in out.splitlines():
        if ref.endswith("/HEAD"):
            continue
        remote, _, branch = ref.partition("/")
        if branch:
            pairs.append((remote, branch))
    return pairs


def is_on_remote(repo: str, sha: str, remotes: list[tuple[str, str]]) -> bool:
    for remote, branch in remotes:
        code, _, _ = git(repo, "merge-base", "--is-ancestor", sha, f"refs/remotes/{remote}/{branch}")
        if code == 0:
            return True
    return False


def remote_sha(repo: str, remote: str, branch: str) -> str:
    code, out, _ = git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if code != 0 or not out:
        return ""
    return out.split()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repository to inspect")
    ap.add_argument("--remote", default="", help="remote to push to (default: first configured)")
    ap.add_argument("--push", action="store_true", help="actually push (default: report only)")
    ap.add_argument("--only", default="", help="comma separated branch names to consider")
    args = ap.parse_args()

    repo = args.repo
    code, out, err = git(repo, "remote")
    if code != 0:
        print(f"not a git repository: {repo} ({err})", file=sys.stderr)
        return 2
    remotes_cfg = [r for r in out.splitlines() if r.strip()]
    remote = args.remote or (remotes_cfg[0] if remotes_cfg else "")
    if not remote:
        print("no remote configured; nothing can be rescued", file=sys.stderr)
        return 2

    only = {b for b in args.only.split(",") if b}
    known = remote_remotes(repo)
    unpushed, protected_hits, diverged = [], [], []

    for branch, sha in local_branches(repo):
        if only and branch not in only:
            continue
        if is_on_remote(repo, sha, known):
            continue
        if branch in PROTECTED:
            protected_hits.append((branch, sha))
            continue
        theirs = remote_sha(repo, remote, branch)
        if theirs:
            diverged.append((branch, sha, theirs))
            continue
        unpushed.append((branch, sha))

    for branch, sha in unpushed:
        line = f"{branch} {sha[:9]}"
        if not args.push:
            print(f"would push  {line}")
            continue
        code, _, err = git(repo, "push", remote, f"refs/heads/{branch}:refs/heads/{branch}")
        print(("pushed      " if code == 0 else "FAILED      ") + line + ("" if code == 0 else f"  {err}"))

    for branch, sha in protected_hits:
        print(f"protected   {branch} {sha[:9]}  (local-only; pushing this is a shared-branch decision)")

    for branch, sha, theirs in diverged:
        print(f"diverged    {branch} local {sha[:9]} vs {remote} {theirs[:9]}  (not touched)")

    if not unpushed and not protected_hits and not diverged:
        print("nothing local-only; every branch tip is on the remote")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
