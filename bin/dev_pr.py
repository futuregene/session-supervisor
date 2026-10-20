#!/usr/bin/env python3
"""Keep a per-developer `dev-<user>` branch moving into `main`, on a cadence.

Why this exists (user, 2026-10-20): a code repo can switch to one long-lived
branch per developer — task sessions only commit and push to `dev-<username>`;
they never open a PR or arm auto-merge. Taking that work into `main` becomes the
supervisor's job, every ~3 hours, so a red branch is noticed and handed to a
fresh session instead of sitting there.

The judgement lives in `decide()` — a pure function over facts gathered by
`gather()` — so `selftest_dev_pr.py` can pin every branch of it without a
network. `act()` is the only part that touches the repo.

Facts it needs (all from the remote, never from a stale local branch):

    branch            dev-<gh login>, resolved at run time (never hardcoded)
    ahead             commits on origin/dev-<login> that origin/main lacks
    pr                the open PR for that head, if any (number, mergeState,
                      armed auto-merge, failing required checks)
    last_merged       the newest merged PR for that head, and whether its
                      merge commit is already an ancestor of origin/main
    same_tree         whether the branch's content equals origin/main's

Actions (`act()`):

    open_pr        no PR and work ahead      -> create, arm squash auto-merge
    arm            PR exists, unarmed        -> `gh pr merge --squash --auto`
    fix_behind     PR is BEHIND              -> merge origin/main in, push
    report_red     required check failed     -> new session with the evidence
    repoint        merged, same content      -> reset dev-<login> to main
    keep_rebase    merged, more work on top  -> rebase those commits onto main
    waiting        checks in flight          -> nothing (next pass looks again)
    idle           nothing on the branch     -> nothing

Two rules that are easy to get wrong:

- **A squash merge never makes the branch's commits ancestors of main.** So
  "ahead > 0" is not "unmerged"; the merged-PR fact is what says merged.
- **The post-merge reset is the only sanctioned force-push** (`--force-with-lease`
  on the dev branch). Never delete the remote branch: it is the developer's
  continuous branch, and its PR review timeline dies with it.

Usage:
  bin/dev_pr.py --repo <path> [--dry-run] [--json]
"""
import argparse
import json
import os
import subprocess
import sys
import time

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolpath import future_exe, tool_path, config_path  # noqa: E402

LOG = os.path.join(WATCH, "logs", "dev-pr.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{time.strftime('%F %T')} {msg}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


def sh(args, cwd=None, timeout=180, check=False):
    """Run one command; return (rc, stdout, stderr). Never raises."""
    env = dict(os.environ)
    env["PATH"] = tool_path()
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {p.stderr[:400]}")
    return p.returncode, p.stdout, p.stderr


def gh(args, cwd=None, timeout=180):
    return sh(["gh"] + args, cwd=cwd, timeout=timeout)


def git(repo, args, timeout=180):
    return sh(["git", "-C", repo] + args, timeout=timeout)


def decide(facts):
    """The whole policy, as a pure function: facts in, one action out.

    Order matters. A merged PR is decided before "work ahead", because a squash
    merge leaves the branch's own commits looking unmerged forever; and a red
    check is decided before BEHIND, because rebasing a red branch just burns
    another CI run on the same failure.
    """
    if facts.get("local_ahead"):
        # Someone has commits only on the local branch. Not ours to push or
        # reset: report it so a human decides (it may be a running session).
        return {"action": "report_local_ahead",
                "reason": f"local dev branch is {facts['local_ahead']} commit(s) ahead of the remote"}

    pr = facts.get("pr") or {}
    if pr:
        if pr.get("failing"):
            return {"action": "report_red",
                    "reason": "required checks failing: " + ", ".join(pr["failing"]),
                    "pr": pr.get("number"), "failing": pr["failing"],
                    "run": pr.get("runUrl")}
        if pr.get("mergeState") == "BEHIND":
            return {"action": "fix_behind", "reason": "PR is BEHIND main",
                    "pr": pr.get("number")}
        if not pr.get("armed"):
            return {"action": "arm", "reason": "open PR has no auto-merge armed",
                    "pr": pr.get("number")}
        return {"action": "waiting", "reason": "checks in flight",
                "pr": pr.get("number")}

    last = facts.get("last_merged") or {}
    if last.get("merged") and last.get("in_main"):
        if not facts.get("branch_exists"):
            # The merge consumed the branch (GitHub's "delete branch on merge"
            # would do exactly that) and it is a *continuous* branch: put it back
            # at the new main, or the next task session has nowhere to push.
            return {"action": "recreate",
                    "reason": f"#{last['number']} merged and "
                              f"`{facts.get('branch', 'the dev branch')}` is gone; "
                              "it is a continuous branch and must exist at main",
                    "pr": last["number"]}
        if facts.get("same_tree"):
            return {"action": "repoint",
                    "reason": f"#{last['number']} is in main and the branch adds nothing",
                    "pr": last["number"]}
        return {"action": "keep_rebase",
                "reason": f"#{last['number']} is in main but the branch has newer commits",
                "pr": last["number"]}

    if not facts.get("branch_exists"):
        return {"action": "idle", "reason": "no dev branch on the remote yet"}
    if facts.get("ahead", 0) > 0:
        return {"action": "open_pr", "reason": f"{facts['ahead']} commit(s) ahead of main"}
    return {"action": "idle", "reason": "branch is level with main"}


def gather(repo, login=None):
    """Read the facts `decide()` needs. Everything comes from the remote."""
    facts = {"repo": repo}
    if not login:
        rc, out, err = gh(["api", "user", "--jq", ".login"])
        if rc != 0:
            raise RuntimeError(f"gh api user failed: {err[:200]}")
        login = out.strip()
    facts["login"] = login
    branch = f"dev-{login}"
    facts["branch"] = branch

    rc, out, err = git(repo, ["fetch", "--prune", "origin"], timeout=300)
    if rc != 0:
        raise RuntimeError(f"git fetch failed: {err[:200]}")
    rc, out, _ = gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd=repo)
    facts["repo_slug"] = out.strip() if rc == 0 else ""

    rc, out, _ = git(repo, ["rev-parse", "--verify", f"origin/{branch}"])
    facts["branch_exists"] = rc == 0
    if facts["branch_exists"]:
        rc, out, _ = git(repo, ["rev-list", "--count", f"origin/main..origin/{branch}"])
        facts["ahead"] = int(out.strip() or 0) if rc == 0 else 0
        rc, _, _ = git(repo, ["diff", "--quiet", "origin/main", f"origin/{branch}"])
        facts["same_tree"] = rc == 0
        # The local branch must track the remote; "ahead" locally is unpushed work.
        rc, out, _ = git(repo, ["rev-list", "--count", f"origin/{branch}..{branch}"])
        facts["local_ahead"] = int(out.strip() or 0) if rc == 0 else 0

    rc, out, _ = gh(["pr", "list", "--head", branch, "--state", "open", "--json",
                     "number,mergeStateStatus,statusCheckRollup,autoMergeRequest,url"],
                    cwd=repo)
    pr = None
    if rc == 0 and out.strip():
        items = json.loads(out)
        if items:
            p = items[0]
            failing, run_url = [], None
            for chk in p.get("statusCheckRollup") or []:
                if chk.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED"):
                    failing.append(chk.get("name") or chk.get("context") or "?")
                    run_url = run_url or chk.get("detailsUrl")
            pr = {"number": p["number"], "mergeState": p.get("mergeStateStatus"),
                  "armed": bool(p.get("autoMergeRequest")), "failing": failing,
                  "runUrl": run_url, "url": p.get("url")}
    facts["pr"] = pr

    rc, out, _ = gh(["pr", "list", "--head", branch, "--state", "merged", "--limit", "1",
                     "--json", "number,mergeCommit"], cwd=repo)
    last = {"merged": False}
    if rc == 0 and out.strip():
        items = json.loads(out)
        if items and items[0].get("mergeCommit"):
            sha = items[0]["mergeCommit"]["oid"]
            rc2, _, _ = git(repo, ["merge-base", "--is-ancestor", sha, "origin/main"])
            last = {"merged": True, "number": items[0]["number"], "sha": sha,
                    "in_main": rc2 == 0}
    facts["last_merged"] = last
    return facts


def run_worktree(repo, branch, name, fn):
    """Do branch work in a throwaway worktree so the main checkout is untouched."""
    path = os.path.join(repo, ".worktrees", name)
    rc, _, err = git(repo, ["worktree", "add", "--force", "--detach", path, f"origin/{branch}"],
                     timeout=300)
    if rc != 0:
        raise RuntimeError(f"worktree add failed: {err[:300]}")
    try:
        return fn(path)
    finally:
        git(repo, ["worktree", "remove", "--force", path])
        git(repo, ["worktree", "prune"])


def act(facts, repo, dry_run=False):
    """Carry out `decide()`'s verdict. Returns a short human sentence."""
    verdict = decide(facts)
    action = verdict["action"]
    branch = facts["branch"]
    if dry_run:
        return f"would {action}: {verdict['reason']}"
    if action == "open_pr":
        rc, out, err = gh(["pr", "create", "--base", "main", "--head", branch,
                           "--title", _pr_title(repo, branch),
                           "--body", _pr_body(facts)], cwd=repo, timeout=300)
        if rc != 0:
            raise RuntimeError(f"pr create failed: {err[:300]}")
        url = out.strip().splitlines()[-1]
        num = url.rstrip("/").split("/")[-1]
        gh(["pr", "merge", num, "--squash", "--auto"], cwd=repo, timeout=180)
        return f"opened #{num} for {branch} and armed squash auto-merge ({url})"
    if action == "arm":
        gh(["pr", "merge", str(verdict["pr"]), "--squash", "--auto"], cwd=repo)
        return f"armed squash auto-merge on #{verdict['pr']}"
    if action == "fix_behind":
        def merge_main(path):
            git(path, ["checkout", "-B", branch, f"origin/{branch}"])
            rc, _, err = git(path, ["merge", "--no-edit", "origin/main"], timeout=300)
            if rc != 0:
                raise RuntimeError(f"merge main failed (conflict?): {err[:300]}")
            rc, _, err = git(path, ["push", "origin", f"HEAD:{branch}"], timeout=300)
            if rc != 0:
                raise RuntimeError(f"push failed: {err[:300]}")
        run_worktree(repo, branch, "devpr-behind", merge_main)
        return f"merged origin/main into {branch}, pushed; #{verdict['pr']} will re-run"
    if action == "recreate":
        rc, _, err = git(repo, ["push", "origin", f"origin/main:refs/heads/{branch}"], timeout=300)
        if rc != 0:
            raise RuntimeError(f"recreate push failed: {err[:200]}")
        git(repo, ["fetch", "--prune", "origin"])
        return (f"#{verdict['pr']} merged and had removed {branch}; recreated it at origin/main "
                "(it is a continuous branch)")
    if action == "repoint":
        _repoint(repo, branch)
        return f"#{verdict['pr']} is in main; re-pointed {branch} to origin/main (kept the branch)"
    if action == "keep_rebase":
        def rebase(path):
            git(path, ["checkout", "-B", branch, f"origin/{branch}"])
            rc, _, err = git(path, ["rebase", "origin/main"], timeout=300)
            if rc != 0:
                raise RuntimeError(f"rebase onto main failed (conflict?): {err[:300]}")
            rc, _, err = git(path, ["push", "--force-with-lease", "origin", f"HEAD:{branch}"],
                             timeout=300)
            if rc != 0:
                raise RuntimeError(f"push failed: {err[:300]}")
        run_worktree(repo, branch, "devpr-rebase", rebase)
        return (f"#{verdict['pr']} is in main; rebased the newer commits onto origin/main "
                f"and pushed {branch}")
    if action == "report_red":
        return _report_red(facts, verdict)
    return action + ": " + verdict["reason"]


def _repoint(repo, branch):
    """Point dev-<login> at origin/main — the one sanctioned force-push."""
    rc, out, err = git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and out.strip() == branch:
        raise RuntimeError(f"{branch} is checked out in the main worktree; update it there instead")
    rc, _, err = git(repo, ["branch", "-f", branch, "origin/main"])
    if rc != 0:
        # A worktree holds it: fast-forward inside that worktree instead.
        rc2, out2, _ = git(repo, ["worktree", "list", "--porcelain"])
        path = ""
        for line in (out2 or "").splitlines():
            if line.startswith("worktree "):
                path = line.split(" ", 1)[1]
            if line.strip() == f"branch refs/heads/{branch}":
                break
        if not path:
            raise RuntimeError(f"cannot re-point {branch}: {err[:200]}")
        rc2, _, err2 = git(path, ["merge", "--ff-only", "origin/main"])
        if rc2 != 0:
            raise RuntimeError(f"ff-only in {path} failed: {err2[:200]}")
    rc, _, err = git(repo, ["push", "--force-with-lease", "origin", f"{branch}:{branch}"],
                     timeout=300)
    if rc != 0:
        raise RuntimeError(f"force-with-lease push failed: {err[:200]}")
    git(repo, ["fetch", "--prune", "origin"])


def _report_red(facts, verdict):
    """A red PR gets a NEW session with the evidence — never the author's.

    Messaging the session that wrote the code would land behind whatever it is
    doing and answer a state that has moved on; the user's rule for this repo is
    a fresh session with the job and the log (2026-10-20).
    """
    branch, pr = facts["branch"], verdict.get("pr")
    failing = verdict.get("failing") or []
    run_url = verdict.get("run") or "(see the PR's checks)"
    log(f"red: #{pr} {failing} {run_url}")
    text = (
        f"PR #{pr} on `{branch}` is red on its required checks and the supervisor "
        "will not merge it.\n\n"
        f"- failing: {', '.join(failing) or 'unknown'}\n"
        f"- run: {run_url}\n"
        f"- PR: https://github.com/{facts.get('repo_slug', '')}/pull/{pr}\n\n"
        "Fix it on the branch by the repo's own workflow: work in a worktree cut from "
        f"`origin/{branch}`, run the affected checks (not the whole suite), commit, "
        f"`git push origin HEAD:{branch}`. Do **not** open a PR (there is one), do not "
        "arm auto-merge, do not weaken an assertion to get green, and do not "
        "force-push. If the failure is a flake, `gh run rerun --failed` and say so.\n"
    )
    path = os.path.join(WATCH, "state", "devpr-red.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    rc, out, err = sh([sys.executable, os.path.join(WATCH, "bin", "send.py"), "--new",
                       "--cwd", facts["repo"], "--name", f"PR #{pr} 检查失败修复",
                       path, "--note", f"#{pr} 必需检查失败：{', '.join(failing) or '见 run'}"],
                      timeout=600)
    if rc != 0:
        return (f"#{pr} is red on {', '.join(failing) or 'unknown'}; the fix session could not "
                f"be created ({err.strip()[:160] or out.strip()[:160]})")
    return f"#{pr} is red on {', '.join(failing) or 'unknown'}; opened a fix session — {out.strip()[:200]}"


def _pr_title(repo, branch):
    rc, out, _ = git(repo, ["log", "-1", "--format=%s", f"origin/main..origin/{branch}"])
    lines = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
    return (lines[0] if lines else f"{branch}: bring the branch's work into main")[:120]


def _pr_body(facts):
    return ("Opened by the session supervisor on its dev-branch cadence: "
            f"`{facts['branch']}` has {facts.get('ahead', 0)} commit(s) that `main` lacks.\n\n"
            f"`{facts['branch']}` stays as the developer's continuous branch after the merge; "
            "it is re-pointed at the new `main`, not deleted.\n")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="path to the repo with the dev-<user> branch")
    ap.add_argument("--login", default=None, help="github login (default: `gh api user`)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        with open(config_path(), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001
        cfg = {}
    if not cfg.get("dev_pr_enabled", True) and not args.dry_run:
        print("dev_pr_enabled is false in bin/config.json; nothing done")
        return 0
    # `dev_pr_hours` documents the cadence the scheduler is set to. The scheduler
    # itself is a task trigger (a copy), so this is where a missed cadence shows
    # up: if the last run is more than two windows old, the trigger is not firing
    # — say so rather than letting a silent gap look like "nothing to merge".
    cov_file = os.path.join(WATCH, "state", "dev_pr.json")
    hours = float(cfg.get("dev_pr_hours", 3) or 3)
    try:
        last = json.load(open(cov_file, encoding="utf-8")).get("lastRunTs", 0)
    except Exception:  # noqa: BLE001
        last = 0
    if last and (time.time() - last) > 2 * hours * 3600:
        log(f"cadence missed: last run {time.strftime('%F %T', time.localtime(last))}, "
            f"expected every {hours:g}h")

    facts = gather(args.repo, args.login)
    verdict = decide(facts)
    if args.json:
        print(json.dumps({"facts": facts, "verdict": verdict}, ensure_ascii=False, indent=1))
        return 0
    log(f"{facts['branch']} ahead={facts.get('ahead')} pr={verdict.get('pr')} "
        f"-> {verdict['action']} ({verdict['reason']})")
    try:
        sentence = act(facts, args.repo, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        log(f"FAILED {verdict['action']}: {exc}")
        print(f"FAILED {verdict['action']}: {exc}")
        return 1
    log(f"done {verdict['action']}: {sentence}")
    if not args.dry_run:
        os.makedirs(os.path.dirname(cov_file), exist_ok=True)
        with open(cov_file, "w", encoding="utf-8") as fh:
            json.dump({"lastRunTs": int(time.time()), "action": verdict["action"],
                       "login": facts["login"]}, fh, ensure_ascii=False)
    print(sentence)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
