#!/usr/bin/env python3
"""Selftest for the one judgement this change adds to scan.py.

`transient_failure()` decides whether a failed run is news worth waking someone for or
a network blip that retries itself. It is pinned here because both directions of a
mistake are expensive: a false negative hides a real crash, a false positive would
downgrade every failure to `low` and nobody would ever be woken.

Measured 2026-11-13: three `failed_run`/`run_error` wakes in one morning, all
`Connection refused` to the intranet model host, each followed by a completed run in
the same session.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import scan  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# The exact strings seen in this deployment's run ledger.
REFUSED = ("error sending request for url (http://10.28.1.21:30185/v1/chat/completions): "
           "client error (Connect): tcp connect error: Connection refused (os error 61)")

check("the real refused-connection error is transient", scan.transient_failure(REFUSED))
check("case and wording do not matter",
      scan.transient_failure("CONNECTION REFUSED") and scan.transient_failure("Error sending request…"))
check("a reset connection is transient", scan.transient_failure("connection reset by peer"))
check("an upstream 503 is transient", scan.transient_failure("server returned 503 Service Unavailable"))

# The other direction: things that must stay `high`.
check("a panic is not transient",
      not scan.transient_failure("thread 'main' panicked at agent/src/run_loop.rs:1145"))
check("an assertion failure is not transient",
      not scan.transient_failure("assertion `left == right` failed: expected 3, got 4"))
check("an HTTP 400 is not transient",
      not scan.transient_failure("LLM API request failed (HTTP 400): At most 0 image(s) may be provided"))
check("no error text means no downgrade", not scan.transient_failure(None) and not scan.transient_failure(""))

# `timeout` alone is deliberately transient (a wedged request retries); make sure the
# bare word does not accidentally catch a code-level message that merely mentions it.
check("a lint message mentioning timeout is still transient by design",
      scan.transient_failure("clippy: this call passes a timeout"))

print()
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
