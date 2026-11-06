#!/usr/bin/env python3
"""Sample whether the local embedding service and its index are still working.

Why this exists: `KeepAlive=true` restarts a service, it never reports one. The
embedding service on `127.0.0.1:8010` died in its import phase and restart-looped
for about two weeks — the port never listened, so every `life-search index` run
failed with `connection refused`, and 1313 recordings across 7 dates never entered
the index. Nothing anywhere said so: no event, no log line anyone reads, no error
that reaches a turn. It was found by chance, in an unrelated session, by running
`life-search stats`.

The lesson is not "watch the process": `launchctl` already knew the pid was there
and dying. It is that the only observer that can see this class of failure is an
**outside** one — something that asks the service to answer, and asks the index how
far behind it is.

So this is a *sampler*, not a watchdog, in the same shape as `bin/balance_probe.py`:

  * it is called from the ordinary 30 s scan (`bin/scan.py`), not from a timer of
    its own, so there is no second scheduler to lose;
  * it has its own gap guard, because the scan's cadence is not this probe's
    cadence: `life-search stats` costs ~2.5 s per call, so probing every 30 s
    would spend two hours of CPU a day to learn nothing new;
  * **silence is its normal output.** A healthy probe writes nothing at all — no
    log line, no event. It writes one event, and only one, when a criterion says
    the service or the index has degraded;
  * one failure signature is reported at most once per `--min-gap`, and a healthy
    probe clears the "already reported" memory, or the *second* outage would be
    silently swallowed by the first one's bookkeeping.

What it probes (both with a timeout, total budget 5 s so the scan cannot be held up):

  1. `GET /health` — `200 healthy` is fine; `503` / `status: loading` is the
     server's own "still preloading" answer, which is **not** a failure; a refused
     connection or a timeout is.
  2. `life-search stats` — the index's own numbers: items not yet indexed (`尚未索引`),
     how far behind (`落后 N 天`), and which dates the last full index run failed on
     (`上次索引失败: N 个日期`). A missing CLI is skipped and logged, never an error.

Deliberately **read only** (see the red lines in the brief this was built under):
it does not restart the service, does not kick `launchctl`, does not run an index
job. Restarting and backfilling are separate decisions; this probe only exists to
make the failure *visible*.

One failure mode is out of reach here, by design: a service that answers `/health`
and `/v1/models` with 200 but hangs on the first real embedding request. Catching
that needs an inference call, which is not a probe. It does surface in this
probe's own numbers one step later — the index run fails, so `上次索引失败` /
`尚未索引` go up.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

WATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Locating `life-search` and `python3`: discovery, not one machine's paths.
from toolpath import tool_path  # noqa: E402

STATE_PATH = os.path.join(WATCH, "state", "service-probe.json")
PENDING_PATH = os.path.join(WATCH, "inbox", "pending.jsonl")

#: The embedding service the index depends on.
HEALTH_URL = "http://127.0.0.1:8010/health"
#: Per-call ceilings. Together with the total budget below they bound one probe.
HEALTH_TIMEOUT = 1.5
STATS_TIMEOUT = 3.0
#: One probe may never hold up the 30 s scan (the brief's red line).
TOTAL_BUDGET = 5.0
#: Same failure signature reported at most this often (config can override).
REPORT_GAP_SECONDS = 180 * 60
#: How often a probe actually probes. ~2.5 s per `life-search stats`, so a 30 s
#: cadence would cost two hours of CPU a day for information that moves once a day.
PROBE_GAP_SECONDS = 5 * 60
#: Degradation thresholds. Both are deliberately above the normal daily churn: one
#: day of fresh recordings arrives between index runs, and `落后 1 天` is that
#: slice, not a fault (`behind_days` = hours since the newest indexed item / 24).
BEHIND_DAYS = 2
PENDING_ITEMS = 200
#: Events this probe emits.
EVENT_KIND = "service_degraded"
EVENT_PRIORITY = "high"
#: A body marker that means "the server is up but has no engine loaded".
BODY_MARKERS = ("not loaded", "no model", "not ready", "model not loaded")
#: Reported signatures older than this are dropped from the state file.
KEEP_REPORTED_SECONDS = 7 * 24 * 3600

PENDING_LINE = re.compile(r"尚未索引[:：]\s*(\d+)\s*条")
BEHIND_LINE = re.compile(r"落后\s*(\d+)\s*天")
FAILED_HEADER = re.compile(r"上次索引失败[:：]\s*(\d+)\s*个日期")
LAST_ERROR_LINE = re.compile(r"最后错误[:：]\s*(.+)")
WARNING_LINE = re.compile(r"统计警告[:：]\s*(.+)")


# ----------------------------------------------------------------- small utils


def now_iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else time.time()))


def new_event_id(ts=None):
    """Same shape as `scan.py`'s ids: sortable, and unique per emit."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts if ts is not None else time.time()))
    return "ev_" + stamp + "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def load_state(path=None):
    try:
        with open(path or STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state, path=None):
    path = os.fspath(path or STATE_PATH)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def append_event(event, path=None):
    path = path or PENDING_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def which_cli(name="life-search"):
    """Absolute path of `life-search`, or None. Discovery only, never a guess."""
    try:
        return shutil.which(name, path=tool_path())
    except Exception:  # noqa: BLE001 — a broken PATH must not break the scan
        return None


# ------------------------------------------------------------------- probe 1: /health


def _unreachable(reason, elapsed):
    """Classify a transport failure (`URLError.reason`, an OSError, a timeout)."""
    text = str(reason) or type(reason).__name__
    low = text.lower()
    if isinstance(reason, ConnectionRefusedError) or "refused" in low:
        sig = "embed-refused"
    elif isinstance(reason, TimeoutError) or "timed out" in low or "timeout" in low:
        sig = "embed-timeout"
    else:
        sig = "embed-unreachable"
    return {"state": "failed", "signature": sig, "reason": text,
            "elapsedMs": round(elapsed * 1000)}


def classify_health(code, body, elapsed=None):
    """One `/health` answer -> a verdict. Pure, so the selftest can pin every branch.

    `503` + `status: loading` is the server telling us it is still preloading its
    pinned models (the port is bound first, on purpose) — a startup state, not a
    fault. Anything the server answers with a body is *reachable*, so only a
    non-healthy answer counts against it.
    """
    text = (body or "").strip()
    word = ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            word = str(obj.get("status") or "").lower()
    except ValueError:
        pass
    out = {"code": code, "status": word,
           "elapsedMs": round(elapsed * 1000) if elapsed is not None else None,
           "body": text[:400]}
    low = text.lower()
    for marker in BODY_MARKERS:
        if marker in low:
            # Recorded, never a verdict on its own: the brief asks for it in `detail`
            # ("200 但 body 里有 not loaded 之类"), and a marker is weaker evidence
            # than the index's own numbers.
            out["note"] = f"响应体含 {marker!r}"
            break
    if code == 503 or word == "loading":
        out.update(state="loading", signature=None)
        return out
    if code == 200 and word in ("", "healthy"):
        out.update(state="healthy", signature=None)
        return out
    if code == 200:
        out.update(state="failed", signature="embed-unhealthy",
                   reason=f"/health 200 但 status={word!r}")
        return out
    out.update(state="failed", signature=f"embed-http-{code}", reason=f"/health 返回 HTTP {code}")
    return out


def http_health(url=None, timeout=HEALTH_TIMEOUT):
    """Ask the embedding service how it is. Never raises: a failure is a verdict."""
    url = url or HEALTH_URL
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code, raw = resp.status, resp.read(4096)
    except urllib.error.HTTPError as exc:  # 503 etc: an answer, not a refusal
        code = exc.code
        try:
            raw = exc.read(4096) or b""
        except Exception:  # noqa: BLE001
            raw = b""
    except urllib.error.URLError as exc:
        return _unreachable(exc.reason, time.monotonic() - t0)
    except TimeoutError:
        return {"state": "failed", "signature": "embed-timeout",
                "reason": f"/health 超过 {timeout}s 无响应",
                "elapsedMs": round((time.monotonic() - t0) * 1000)}
    except OSError as exc:
        return _unreachable(exc, time.monotonic() - t0)
    return classify_health(code, raw.decode("utf-8", "replace"), time.monotonic() - t0)


# --------------------------------------------------------------------- probe 2: stats


def parse_stats(text):
    """Pull the degradation numbers out of `life-search stats`' printed report.

    The output is meant for a human, so this parses the lines it prints rather than
    a schema: `尚未索引: N 条`, `落后 N 天`, `上次索引失败: N 个日期` and the
    `    - <date>` lines under it. Every field is optional — an absent field means
    "this build did not print it", which must not read as zero.
    """
    text = text or ""
    out = {"pending_total": None, "behind_days": None, "failed_count": None,
           "failed_dates": [], "last_error": "", "warnings": [],
           "indexing": "索引进行中" in text}
    m = PENDING_LINE.search(text)
    if m:
        out["pending_total"] = int(m.group(1))
    m = BEHIND_LINE.search(text)
    if m:
        out["behind_days"] = int(m.group(1))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = FAILED_HEADER.search(line)
        if not m:
            continue
        out["failed_count"] = int(m.group(1))
        for nxt in lines[i + 1:]:
            stripped = nxt.strip()
            if not stripped.startswith("-"):
                break
            date = stripped[1:].strip()
            if date:
                out["failed_dates"].append(date)
        break
    m = LAST_ERROR_LINE.search(text)
    if m:
        out["last_error"] = m.group(1).strip()
    out["warnings"] = [w.strip() for w in WARNING_LINE.findall(text) if w.strip()]
    if out["failed_count"] is None:
        out["failed_count"] = len(out["failed_dates"])
    return out


def run_stats(cli="auto", timeout=STATS_TIMEOUT):
    """Run `life-search stats`, or say why not. A missing CLI is a skip, not an error."""
    path = which_cli() if cli in (None, "", "auto") else cli
    if not path or not os.path.exists(path):
        return {"state": "skipped", "reason": "no-cli",
                "detail": "life-search 不在 PATH（这台机器没装或换了位置）"}
    try:
        proc = subprocess.run([path, "stats"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "PATH": tool_path()})
    except subprocess.TimeoutExpired:
        return {"state": "failed", "signature": "stats-timeout",
                "reason": f"life-search stats 超过 {timeout}s 无响应"}
    except FileNotFoundError:
        return {"state": "skipped", "reason": "no-cli", "detail": f"{path} 不存在"}
    except OSError as exc:
        return {"state": "skipped", "reason": "cli-error",
                "detail": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        tail = [ln.strip() for ln in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if ln.strip()]
        return {"state": "failed", "signature": "stats-error", "returncode": proc.returncode,
                "reason": tail[-1] if tail else f"exit {proc.returncode}"}
    return {"state": "ok", "parsed": parse_stats(proc.stdout or "")}


# ----------------------------------------------------------------------- verdict


def criteria(health, stats):
    """Which criteria say "degraded", with the sentence that justifies each.

    Empty list = healthy (a `loading` service counts as healthy-starting). The
    signatures are the dedupe identity: the same set never reports twice inside the
    gap, and a *changed* set is news (e.g. the embedder came back but the index is
    now failing).
    """
    out = []
    if health.get("state") == "failed":
        out.append({"criterion": health.get("signature") or "embed-unreachable",
                    "why": health.get("reason") or (health.get("signature") or "")})
    if stats.get("state") == "failed":
        out.append({"criterion": stats.get("signature") or "stats-error",
                    "why": stats.get("reason") or ""})
    if stats.get("state") == "ok":
        parsed = stats.get("parsed") or {}
        count = parsed.get("failed_count") or 0
        if count:
            dates = "、".join((parsed.get("failed_dates") or [])[:3]) or "见 detail"
            out.append({"criterion": "index-failed-dates",
                        "why": f"上次索引 {count} 个日期失败（{dates}）"})
        days = parsed.get("behind_days")
        if days is not None and days >= BEHIND_DAYS:
            out.append({"criterion": "index-behind", "why": f"索引落后 {days} 天"})
        pending = parsed.get("pending_total")
        if pending is not None and pending >= PENDING_ITEMS:
            out.append({"criterion": "index-pending", "why": f"{pending} 条尚未进索引"})
    return out


def signature_of(crit):
    return "|".join(sorted({c["criterion"] for c in crit}))


def last_error_text(health, stats):
    """The one error line worth quoting: the concrete one, not the most general."""
    if stats.get("state") == "failed" and stats.get("reason"):
        return stats["reason"]
    parsed = stats.get("parsed") or {}
    if parsed.get("last_error"):
        return parsed["last_error"]
    if health.get("state") == "failed" and health.get("reason"):
        return health["reason"]
    if stats.get("state") == "failed" and stats.get("reason"):
        return stats["reason"]
    return ""


def build_event(sig, crit, health, stats, ts):
    """One event for the whole verdict: which criterion fired, and the evidence."""
    parsed = stats.get("parsed") or {}
    failed_dates = list(parsed.get("failed_dates") or [])[:3]
    error_text = last_error_text(health, stats)
    saying = "；".join(f"{c['criterion']}（{c['why']}）" for c in crit)
    summary = f"服务探针判退化：{saying}"[:200]
    detail = {
        "signature": sig,
        "criteria": crit,
        "probedAt": now_iso(ts),
        "failedDates": failed_dates,
        "lastError": error_text,
        "health": {k: health.get(k) for k in ("state", "code", "status", "reason", "note",
                                              "elapsedMs", "signature")},
        "index": {k: parsed.get(k) for k in ("pending_total", "behind_days", "failed_count",
                                             "last_error", "warnings", "indexing")},
        "statsState": stats.get("state"),
        "statsReason": stats.get("reason") or stats.get("detail") or "",
        "readOnly": "只读探测：没有重启服务、没有跑索引；补索引与重启是另一件事",
        "probe": "bin/service_probe.py",
    }
    return {"id": new_event_id(ts), "kind": EVENT_KIND, "priority": EVENT_PRIORITY,
            "sessionId": "", "sessionName": "服务探针",
            "summary": summary, "detail": detail, "detectedAtMs": int(ts * 1000)}


# -------------------------------------------------------------------------- probe


def probe(now=None, report_gap_seconds=REPORT_GAP_SECONDS, probe_gap_seconds=PROBE_GAP_SECONDS,
          health_url=None, cli="auto", state_path=None, pending_path=None,
          health_fn=None, stats_fn=None, budget=TOTAL_BUDGET):
    """Probe once. Writes at most one event; returns what it found.

    Returns one of:

      {"skipped": "fresh"}                      — the last probe is younger than the gap
      {"signature": "", "criteria": [], ...}    — healthy, nothing written
      {"signature": "…", "emitted": [id]}       — degraded, one event appended
      {"signature": "…", "suppressed": True}    — degraded, already reported < gap ago

    `health_fn` / `stats_fn` exist so the selftest can drive every branch without a
    server and without a real `life-search`; the defaults are the real probes.
    """
    now = time.time() if now is None else float(now)
    state_path = state_path or STATE_PATH
    pending_path = pending_path or PENDING_PATH
    state = load_state(state_path)

    last = float(state.get("lastProbeTs") or 0)
    if probe_gap_seconds and last and (now - last) < probe_gap_seconds:
        return {"skipped": "fresh", "lastProbeAgoSeconds": round(now - last),
                "state": state}

    deadline = time.monotonic() + max(float(budget), 0.1)
    if health_fn is not None:
        health = health_fn()
    else:
        health = http_health(health_url or HEALTH_URL,
                             timeout=max(0.2, min(HEALTH_TIMEOUT, deadline - time.monotonic())))
    if stats_fn is not None:
        stats = stats_fn()
    else:
        stats = run_stats(cli, timeout=max(0.2, min(STATS_TIMEOUT, deadline - time.monotonic())))

    crit = criteria(health, stats)
    sig = signature_of(crit)
    out = {"probedAt": now_iso(now), "signature": sig, "criteria": crit,
           "health": health, "index": stats.get("parsed") or {},
           "statsState": stats.get("state"), "emitted": [], "suppressed": False,
           "recovered": []}

    state["lastProbeTs"] = now
    state["lastResult"] = {
        "probedAt": now_iso(now), "signature": sig,
        "health": health.get("state"), "statsState": stats.get("state"),
        "criteria": [c["criterion"] for c in crit],
        "lastError": last_error_text(health, stats)[:300],
    }

    if not crit:
        # Recovery clears the memory of what was already reported. Without this the
        # next outage of the same signature would be swallowed by the previous one's
        # gap (the brief's point 3), which is the failure mode of every "remind me
        # once" guard: it reminds once, forever.
        out["recovered"] = sorted((state.get("reported") or {}))
        state["reported"] = {}
        state["healthyAt"] = now_iso(now)
        save_state(state, state_path)
        return out

    reported = dict(state.get("reported") or {})
    prev = float(reported.get(sig) or 0)
    if prev and (now - prev) < report_gap_seconds:
        out["suppressed"] = True
        out["reportedAgoSeconds"] = round(now - prev)
        state["reported"] = reported
        save_state(state, state_path)
        return out

    event = build_event(sig, crit, health, stats, now)
    append_event(event, pending_path)
    reported[sig] = now
    # Keep the file bounded: a signature reported a week ago can no longer suppress
    # anything (the gap is minutes-to-hours), so its only use is archaeology.
    state["reported"] = {k: v for k, v in reported.items()
                         if (now - float(v or 0)) < KEEP_REPORTED_SECONDS}
    save_state(state, state_path)
    out["emitted"] = [event["id"]]
    out["event"] = event
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-gap", type=int, default=REPORT_GAP_SECONDS,
                    help="seconds; the same failure signature is reported once per gap "
                         f"(default {REPORT_GAP_SECONDS})")
    ap.add_argument("--probe-gap", type=int, default=PROBE_GAP_SECONDS,
                    help=f"seconds between probes (default {PROBE_GAP_SECONDS})")
    ap.add_argument("--health-url", default=HEALTH_URL)
    ap.add_argument("--cli", default="auto", help="path to life-search, or 'auto'")
    ap.add_argument("--state", default=None, help="state file (default state/service-probe.json)")
    ap.add_argument("--pending", default=None, help="pending inbox (default inbox/pending.jsonl)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = probe(report_gap_seconds=args.min_gap, probe_gap_seconds=args.probe_gap,
                health_url=args.health_url, cli=args.cli, state_path=args.state,
                pending_path=args.pending)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if res.get("skipped"):
        print(f"[service_probe] skipped: {res['skipped']}")
    elif res.get("emitted"):
        print(f"[service_probe] DEGRADED {res['signature']} -> event {res['emitted'][0]}")
    elif res.get("suppressed"):
        print(f"[service_probe] degraded {res['signature']}, already reported "
              f"{res.get('reportedAgoSeconds')}s ago")
    else:
        print(f"[service_probe] ok ({res.get('health', {}).get('state')}, "
              f"stats {res.get('statsState')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
