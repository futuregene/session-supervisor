#!/bin/bash
# Session supervisor daemon: deterministic scan + wake gate, forever.
# Started by the platform scheduler (launchd / systemd / Task Scheduler); safe to run
# in the foreground.
set -u

WATCH="$(cd "$(dirname "$0")/.." && pwd)"
# PATH for everything below: a fixed, hermetic list, built from directories that
# actually exist on this host (2026-10-18: discovery, not a hardcoded prefix — the
# old line replaced PATH with one machine's Homebrew path; inheriting the caller's
# PATH would instead let a dev working-tree build shadow the installed CLI).
INHERITED_PATH="$PATH"
PATH=""
_add_path() {
  [ -n "$1" ] && [ -d "$1" ] || return 0
  case ":$PATH:" in *":$1:"*) return 0 ;; esac
  PATH="${PATH:+$PATH:}$1"
}
for d in "${FUTURE_TOOLBIN:-}" /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" \
         /usr/bin /bin /usr/sbin /sbin; do
  _add_path "$d"
done
# Git Bash / Cygwin on Windows: the inherited PATH is the Windows PATH already in
# POSIX form, and that is where the interpreter and the CLI actually live
# (`…\Programs\Python313`, `~/.future/bin`). None of the POSIX prefixes above exist
# there, so without this the daemon finds neither `python3` nor `future` and falls
# through to the bare-name fallbacks — it starts, scans nothing, and only the
# scanner health check notices. Split on colons only: Windows paths carry spaces.
case "$(uname -s 2>/dev/null || true)" in
  MINGW*|MSYS*|CYGWIN*)
    _oldifs="$IFS"; IFS=':'
    for d in $INHERITED_PATH; do _add_path "$d"; done
    IFS="$_oldifs" ;;
esac
export PATH
FUTURE="$(command -v future || true)"
[ -n "$FUTURE" ] || FUTURE="future"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || PY="/usr/bin/python3"
# The documented knob is `config.scan_interval_seconds`; the env var is an override
# for tests. Before this line the shell used its own default and ignored the config
# entirely, so editing bin/config.json silently changed nothing (docs/architecture.md
# and INSTALL.md both promise it works).
INTERVAL="${SESSION_WATCH_INTERVAL:-}"
if [ -z "$INTERVAL" ]; then
  INTERVAL="$("$PY" -c 'import json,sys;print(int(json.load(open(sys.argv[1])).get("scan_interval_seconds") or 30))' "$WATCH/bin/config.json" 2>/dev/null || echo 30)"
fi

mkdir -p "$WATCH/logs" "$WATCH/state" "$WATCH/inbox" "$WATCH/journal" "$WATCH/reports"

# `$$` is the pid of *this shell's* numbering scheme. Under Git Bash that is an
# MSYS pid, which no Windows-side tool can look up — `bin/liveness.py` then reports
# "watchd pid N is not running" while the daemon is plainly scanning (measured
# 2026-10-19 on the first Windows install: pid 311 in the file, 23452 in the
# process list). Record the native pid when the runtime exposes it, and do the
# duplicate check in the same numbering scheme, so both readers agree.
_is_mingw() {
  case "$(uname -s 2>/dev/null || true)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
  return 1
}
_alive() {
  [ -n "$1" ] || return 1
  if _is_mingw; then
    # `ps -W` column 4 is the WINPID; `kill -0` would test an MSYS pid instead.
    ps -W 2>/dev/null | awk -v p="$1" 'NR > 1 && $4 == p { found = 1 } END { exit !found }'
  else
    kill -0 "$1" 2>/dev/null
  fi
}
_any_alive() {
  # A pid file written before this change holds an MSYS pid, which `_alive` cannot
  # find; fall back to the shell's own numbering so the upgrade does not start a
  # second daemon next to the running one. `kill -0` only probes, and an unmatched
  # pid is an error, not a signal.
  _alive "$1" && return 0
  kill -0 "$1" 2>/dev/null && return 0
  return 1
}
SELF_PID="$(cat "/proc/$$/winpid" 2>/dev/null || true)"
[ -n "$SELF_PID" ] || SELF_PID="$$"

# never run twice
if [ -f "$WATCH/state/watchd.pid" ]; then
  OLD="$(cat "$WATCH/state/watchd.pid" 2>/dev/null || true)"
  if [ "$OLD" != "$SELF_PID" ] && [ "$OLD" != "$$" ] && _any_alive "$OLD"; then
    echo "$(date '+%F %T') [watchd] already running as pid $OLD; exiting" >> "$WATCH/logs/watchd.log"
    exit 0
  fi
fi
echo "$SELF_PID" > "$WATCH/state/watchd.pid"
echo "$(date '+%F %T') [watchd] start pid=$SELF_PID interval=${INTERVAL}s" >> "$WATCH/logs/watchd.log"

# 每轮扫描的端点断言。扫描器"退出码 0 但没跑到收尾"是这份日志自己看不出来的
# 失败：2026-11 的一次合法函数截断让每轮只写两行 `resync …` 就退出，`logs/scan.log`
# 看起来仍像在正常巡检，18.4 小时里没有一条事件、一次唤醒（docs/gotcha-scan.md B15）。
# 所以把这一轮的输出先收进临时文件：照旧原样追加进 scan.log（行为不变），再看
# 「退出码非 0」或「没有 `[scan] sessions=` 收尾行」，明写一行。
SCAN_OUT="$(mktemp "${TMPDIR:-/tmp}/scan-round.XXXXXX" 2>/dev/null || true)"
[ -n "${SCAN_OUT:-}" ] || SCAN_OUT="$WATCH/state/scan-round.out"
trap 'rm -f "$SCAN_OUT"' EXIT
# SIGTERM keeps meaning "stop": a bare `trap … TERM` would swallow it and the loop
# would carry on (the daemon is stopped by hand and by liveness tooling).
trap 'rm -f "$SCAN_OUT"; exit 143' INT TERM

while true; do
  : > "$SCAN_OUT"
  "$PY" "$WATCH/bin/scan.py" > "$SCAN_OUT" 2>&1
  SCAN_RC=$?
  cat "$SCAN_OUT" >> "$WATCH/logs/scan.log"
  if [ "$SCAN_RC" -ne 0 ] || ! grep -qF '[scan] sessions=' "$SCAN_OUT"; then
    echo "$(date '+%F %T') [watchd] scan produced no summary (exit=$SCAN_RC)" \
      >> "$WATCH/logs/scan.log"
  fi
  "$PY" "$WATCH/bin/blind.py"         >> "$WATCH/logs/scan.log"   2>&1
  "$PY" "$WATCH/bin/approvals.py" auto >> "$WATCH/logs/approvals.log" 2>&1
  "$PY" "$WATCH/bin/notice.py" _trim     >> "$WATCH/logs/scan.log"   2>&1
  "$PY" "$WATCH/bin/directives.py" _expire >> "$WATCH/logs/scan.log" 2>&1
  "$PY" "$WATCH/bin/decide.py"        >> "$WATCH/logs/decide.log" 2>&1
  sleep "$INTERVAL"
done
