#!/usr/bin/env python3
"""Self-test for the auto-approve deny list (no model calls).

Run: python3 bin/selftest_deny.py     -> prints cases and a mismatch count.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ap", os.path.join(HERE, "approvals.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SLASH = "/"
RF = "rm -rf " + SLASH
CASES = [
    # allowed under "yolo + B + merge-auto"
    ("git push -u origin chore/publish-to-github && gh pr create --base main --fill", False),
    ("gh pr create --fill", False),
    ("gh pr merge 1078 --squash", False),
    ("git push origin fix/x && gh pr create --fill", False),
    ("npm test && python3 -m py_compile bin/scan.py", False),
    ("cat reports/needs-you.md", False),
    # still requires the user
    ("git push --force origin main", True),
    ("git push -f origin fix/x", True),
    ("git push origin main", True),
    ("git push origin master", True),
    ("git push origin P2", True),
    ("git push --force-with-lease origin fix/x", True),
    ("git reset --hard HEAD~3", True),
    ("npm publish", True),
    ("gh release create v1.2.2", True),
    ("kubectl apply -f k8s/", True),
    (RF, True),
    ("DROP TABLE sessions", True),
]


def main():
    bad = 0
    for cmd, want in CASES:
        got = bool(m.DENY_RE.search(json.dumps({"command": cmd})))
        if got != want:
            bad += 1
        print(f"  {'ok      ' if got == want else 'MISMATCH'} 拦截={got} 期望={want} :: {cmd}")

    # The argv that approves a parked request. A missing comma once turned
    # `--note <text>` into `--note<text>`, which the CLI rejects as an unknown
    # option, so every auto-approval failed while the ledger claimed success.
    argv = m.approve_argv("sid", 42)
    want = ["session", "approve", "sid", "42", "--note", m.APPROVE_NOTE]
    jargon = [a for a in argv if a.startswith("--note") and a != "--note"]
    ok = argv == want and not jargon
    if not ok:
        bad += 1
    print(f"  {'ok      ' if ok else 'MISMATCH'} approve argv :: {argv}")

    print(f"cases={len(CASES) + 1} mismatches={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())