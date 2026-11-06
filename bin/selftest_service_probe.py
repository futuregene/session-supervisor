#!/usr/bin/env python3
"""Selftest for service_probe.py — the "it died silently" detector.

The probe's whole value is that it is quiet: a healthy service produces no event at
all, and a degraded one produces **one**, not one per scan. Both halves fail the same
way — silently — so every branch is pinned here on synthetic inputs (no server, no
`life-search` call, no network: the only socket used is a loopback connect to a port
nothing listens on, where the answer arrived before any packet could leave the host).

What is pinned:

  1. healthy  -> zero events, and the state file says so;
  2. refused  -> exactly one event, and a second probe inside the gap adds nothing;
  3. recovered -> the memory is cleared, so the next outage reports again;
  4. a `life-search stats` report with failed dates (and one with lag) is judged
     degraded, and its numbers land in the event's `detail`;
  5. `loading` (503) is not a failure, a missing CLI is a skip, and one probe can
     never outrun the 5 s it is allowed to take.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load():
    spec = importlib.util.spec_from_file_location("service_probe", ROOT / "bin" / "service_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def healthy():
    return {"state": "healthy", "code": 200, "status": "healthy",
            "body": '{"status":"healthy","default_model":"m"}'}


def refused():
    return {"state": "failed", "signature": "embed-refused",
            "reason": "[Errno 61] Connection refused"}


def stats_ok(text=None):
    return {"state": "ok", "parsed": SP.parse_stats(text if text is not None else STATS_HEALTHY)}


# A real `life-search stats` report while everything is fine (measured format).
STATS_HEALTHY = """📊 索引统计:
  总条目数:   69976
  录音数:     69860
  照片数:     113
  笔记数:     3
  已索引日期: 145
  日期范围:   1970-01-22T01:39:23.842+08:00 至 2026-11-05T22:55:17.071+08:00
  向量维度:   1024
  状态:       ✅ 就绪，索引已跟上
"""

# The shape of the report that would have caught the two-week outage: the index run
# failed on dates, and nothing told anyone.
STATS_FAILED = """📊 索引统计:
  总条目数:   68663
  录音数:     68547
  照片数:     113
  笔记数:     3
  已索引日期: 138
  向量维度:   1024
  状态:       ⚠️  就绪，但落后 14 天（1313 条尚未进索引）
  尚未索引:   1313 条（录音 1313 / 照片 0 / 笔记 0）
  最近索引到: 2026-10-23T09:12:41.000+08:00
  上次索引失败: 7 个日期
    - 2026-10-24
    - 2026-10-25
    - 2026-10-26
    - 2026-10-27
    - 2026-10-28
    - 2026-10-29
    - 2026-10-30
  最后错误:   dial tcp 127.0.0.1:8010: connect: connection refused
"""


def tmp_paths():
    d = pathlib.Path(tempfile.mkdtemp(prefix="service-probe-"))
    return d / "service-probe.json", d / "pending.jsonl"


def count(path):
    if not path.exists():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


def events(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


SP = load()


def main() -> int:
    # ---------------------------------------------------------------- parsing
    p = SP.parse_stats(STATS_HEALTHY)
    check("healthy report parses to no degradation",
          p["pending_total"] is None and p["behind_days"] is None
          and p["failed_count"] == 0 and p["failed_dates"] == [], f"{p}")

    p = SP.parse_stats(STATS_FAILED)
    check("failed report: 7 failed dates, first three kept for the event",
          p["failed_count"] == 7 and p["failed_dates"][:3] == ["2026-10-24", "2026-10-25", "2026-10-26"],
          f"{p['failed_count']} {p['failed_dates']}")
    check("failed report: pending total and lag parsed",
          p["pending_total"] == 1313 and p["behind_days"] == 14, f"{p}")
    check("failed report: the one error line is quoted verbatim",
          "connection refused" in p["last_error"], p["last_error"])

    h = SP.classify_health(503, '{"status":"loading"}')
    check("503 loading is not a failure", h["state"] == "loading" and h["signature"] is None, f"{h}")
    h = SP.classify_health(200, '{"status":"healthy","engine_pool":{"loaded_count":0,"note":"model not loaded"}}')
    check("200 with a not-loaded marker stays healthy but records the marker",
          h["state"] == "healthy" and "note" in h, f"{h}")
    h = SP.classify_health(200, '{"status":"error"}')
    check("200 with a non-healthy status is a failure",
          h["state"] == "failed" and h["signature"] == "embed-unhealthy", f"{h}")
    h = SP.classify_health(502, "bad gateway")
    check("a non-200, non-503 answer is a failure",
          h["state"] == "failed" and h["signature"] == "embed-http-502", f"{h}")

    # ------------------------------------------------------- 1. healthy is silent
    st, pend = tmp_paths()
    res = SP.probe(now=1_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=healthy, stats_fn=lambda: stats_ok())
    check("healthy: zero events", count(pend) == 0 and res["emitted"] == [], f"{res}")
    check("healthy: no criteria", res["criteria"] == [], f"{res['criteria']}")
    check("healthy: a probe was recorded (so the gap guard can work)",
          json.loads(st.read_text(encoding="utf-8"))["lastProbeTs"] == 1_000_000)

    # ------------------------------------------- 2. refused reports exactly once
    st, pend = tmp_paths()
    res = SP.probe(now=2_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=refused, stats_fn=lambda: stats_ok())
    check("refused: one event", count(pend) == 1 and len(res["emitted"]) == 1, f"{res}")
    ev = events(pend)[0]
    check("refused: event is a high-priority service_degraded with no session",
          ev["kind"] == "service_degraded" and ev["priority"] == "high" and ev["sessionId"] == "",
          f"{ev['kind']}/{ev['priority']}/{ev['sessionId']!r}")
    check("refused: event names the criterion, the error and the time",
          ev["detail"]["criteria"][0]["criterion"] == "embed-refused"
          and "refused" in ev["detail"]["lastError"] and ev["detail"]["probedAt"],
          json.dumps(ev["detail"], ensure_ascii=False)[:300])

    res2 = SP.probe(now=2_000_000 + 60, probe_gap_seconds=0, state_path=st, pending_path=pend,
                    health_fn=refused, stats_fn=lambda: stats_ok())
    check("refused again 60 s later: no second event (gap guard)",
          count(pend) == 1 and res2["suppressed"] is True, f"{res2}")

    # the probe cadence is its own guard: a fresh probe is skipped before any I/O
    called = []
    res3 = SP.probe(now=2_000_000 + 90, probe_gap_seconds=300, state_path=st, pending_path=pend,
                    health_fn=lambda: called.append(1) or refused(), stats_fn=lambda: stats_ok())
    check("inside the probe gap nothing is probed at all",
          res3.get("skipped") == "fresh" and not called, f"{res3}")

    # ---------------------------------------------- 3. recovery clears the memory
    res = SP.probe(now=2_000_000 + 200, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=healthy, stats_fn=lambda: stats_ok())
    check("recovery: no event and the signature is forgotten",
          count(pend) == 1 and res["emitted"] == [] and res["recovered"] == ["embed-refused"],
          f"{res}")
    res = SP.probe(now=2_000_000 + 260, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=refused, stats_fn=lambda: stats_ok())
    check("a second outage after recovery reports again (state was cleared)",
          count(pend) == 2 and len(res["emitted"]) == 1, f"{count(pend)} events")

    # ------------------------------- 4. the index's own numbers can be the verdict
    st, pend = tmp_paths()
    res = SP.probe(now=3_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=healthy, stats_fn=lambda: stats_ok(STATS_FAILED))
    ev = events(pend)
    check("failed dates > 0 is judged degraded on its own",
          len(ev) == 1 and "index-failed-dates" in res["signature"], f"{res['signature']}")
    check("the event carries the first three failed dates",
          len(ev) == 1 and ev[0]["detail"]["failedDates"] == ["2026-10-24", "2026-10-25", "2026-10-26"],
          json.dumps(ev[0]["detail"]["failedDates"] if ev else None))
    check("the event carries the last error line and the index numbers",
          len(ev) == 1 and "connection refused" in ev[0]["detail"]["lastError"]
          and ev[0]["detail"]["index"]["pending_total"] == 1313,
          json.dumps(ev[0]["detail"].get("index") if ev else None, ensure_ascii=False))
    check("one verdict -> one event even with three criteria at once",
          len(ev) == 1 and set(res["signature"].split("|"))
          == {"index-failed-dates", "index-behind", "index-pending"}, f"{res['signature']}")

    # lag alone (no failed dates) is still degradation, and `落后 1 天` is not
    only_lag = STATS_HEALTHY.replace("  状态:       ✅ 就绪，索引已跟上",
                                     "  状态:       ⚠️  就绪，但落后 3 天（410 条尚未进索引）")
    st, pend = tmp_paths()
    res = SP.probe(now=4_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=healthy, stats_fn=lambda: stats_ok(only_lag))
    check("lag alone degrades (behind 3 days, 410 pending)",
          len(events(pend)) == 1 and "index-behind" in res["signature"], f"{res['signature']}")
    one_day = STATS_HEALTHY.replace("  状态:       ✅ 就绪，索引已跟上",
                                    "  状态:       ⚠️  就绪，但落后 1 天（40 条尚未进索引）")
    st, pend = tmp_paths()
    res = SP.probe(now=4_100_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=healthy, stats_fn=lambda: stats_ok(one_day))
    check("daily churn (behind 1 day, 40 items) is not degraded",
          len(events(pend)) == 0 and res["criteria"] == [], f"{res['criteria']}")

    # --------------------------- 5. skips, loading, and the 5 s budget
    st, pend = tmp_paths()
    res = SP.probe(now=5_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                   health_fn=lambda: SP.classify_health(503, '{"status":"loading"}'),
                   stats_fn=lambda: {"state": "skipped", "reason": "no-cli"})
    check("a missing life-search CLI is a skip, not an event",
          count(pend) == 0 and res["criteria"] == [] and res["statsState"] == "skipped", f"{res}")

    # A one-off slow/failed stats call is not a fault: 2026-11-06 a single 3.0 s
    # timeout woke the supervisor while /health answered in 4 ms. It takes two
    # consecutive probes to become an event — and "slow, then fine" must stay silent.
    slow = lambda: {"state": "failed", "signature": "stats-error",
                    "reason": "search server unreachable"}
    st, pend = tmp_paths()
    first = SP.probe(now=5_100_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                     health_fn=healthy, stats_fn=slow)
    check("the first slow stats call only defers (no event, no page)",
          count(pend) == 0 and first.get("deferred"),
          f"events={count(pend)} deferred={first.get('deferred')!r}")

    second = SP.probe(now=5_100_060, probe_gap_seconds=0, state_path=st, pending_path=pend,
                      health_fn=healthy, stats_fn=slow)
    check("a second consecutive failure is degraded (the pipeline is visible either way)",
          count(pend) == 1 and "stats-error" in second["signature"], f"{second['signature']}")

    st, pend = tmp_paths()
    SP.probe(now=5_100_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
             health_fn=healthy, stats_fn=slow)
    back = SP.probe(now=5_100_060, probe_gap_seconds=0, state_path=st, pending_path=pend,
                    health_fn=healthy,
                    stats_fn=lambda: {"state": "ok", "parsed": {}})
    check("a slow call that recovers never becomes an event",
          count(pend) == 0 and back["criteria"] == [], f"events={count(pend)} {back['criteria']}")

    # The real classifier, over a real socket: port 9 on loopback has nothing on it,
    # so this is a local refusal — no packet leaves the host.
    real = SP.http_health("http://127.0.0.1:9/health", timeout=1.0)
    check("a refused connection is classified as embed-refused",
          real["state"] == "failed" and real["signature"] == "embed-refused", f"{real}")

    check("one probe cannot outrun the 5 s the scan can spare",
          SP.HEALTH_TIMEOUT + SP.STATS_TIMEOUT <= SP.TOTAL_BUDGET,
          f"{SP.HEALTH_TIMEOUT}+{SP.STATS_TIMEOUT} > {SP.TOTAL_BUDGET}")

    # a probe against a dead port must actually come back inside the budget
    st, pend = tmp_paths()
    t0 = time.monotonic()
    absent = SP.probe(now=6_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                      health_url="http://127.0.0.1:9/health",
                      stats_fn=lambda: {"state": "skipped", "reason": "no-cli"})
    elapsed = time.monotonic() - t0
    check("a dead service returns promptly (not a slow scan)", elapsed < 4.5, f"{elapsed:.2f}s")

    # Nothing listening *and* no `life-search` to maintain an index: this machine has
    # no indexer, so there is nothing to degrade. It used to wake at `high` every 3 h
    # (measured 2026-11-06: CLI absent, 8010 refused).
    check("a machine with no indexer is not degraded",
          absent["criteria"] == [], f"{absent['criteria']}")
    check("and it writes no event", count(pend) == 0, f"events={count(pend)}")

    # The moment the indexer exists, the same dead port is an outage again.
    st, pend = tmp_paths()
    gone = SP.probe(now=6_000_000, probe_gap_seconds=0, state_path=st, pending_path=pend,
                    health_url="http://127.0.0.1:9/health",
                    stats_fn=lambda: {"state": "ok", "parsed": {}})
    check("with a CLI, the same dead port is degraded",
          [c["criterion"] for c in gone["criteria"]] == ["embed-refused"],
          f"{gone['criteria']}")

    # the event the probe writes must be one the inbox can read back
    rows = events(pend)
    check("the written event parses as an inbox row with an id",
          bool(rows) and rows[0]["id"].startswith("ev_") and rows[0]["summary"], f"{rows}")

    # A *hang* is still a timeout: the port accepts the connect and never answers, so
    # the second look says "open" and the signature stays the flaky-gated one.
    import socket as _socket
    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    try:
        hang = SP.http_health(f"http://127.0.0.1:{srv.getsockname()[1]}/health", timeout=0.5)
    finally:
        srv.close()
    check("an open port that never answers is embed-timeout",
          hang["signature"] == "embed-timeout", f"{hang}")

    # The classifier this whole path rests on, against a port with nothing on it.
    check("port_state sees a closed loopback port as refused",
          SP.port_state("http://127.0.0.1:9/health") == "refused", "")

    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
