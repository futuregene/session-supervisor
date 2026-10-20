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

# never run twice
if [ -f "$WATCH/state/watchd.pid" ]; then
  OLD="$(cat "$WATCH/state/watchd.pid" 2>/dev/null || true)"
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    echo "$(date '+%F %T') [watchd] already running as pid $OLD; exiting" >> "$WATCH/logs/watchd.log"
    exit 0
  fi
fi
echo $$ > "$WATCH/state/watchd.pid"
echo "$(date '+%F %T') [watchd] start pid=$$ interval=${INTERVAL}s" >> "$WATCH/logs/watchd.log"

while true; do
  "$PY" "$WATCH/bin/scan.py"          >> "$WATCH/logs/scan.log"   2>&1
  "$PY" "$WATCH/bin/approvals.py" auto >> "$WATCH/logs/approvals.log" 2>&1
  "$PY" "$WATCH/bin/notice.py" _trim     >> "$WATCH/logs/scan.log"   2>&1
  "$PY" "$WATCH/bin/directives.py" _expire >> "$WATCH/logs/scan.log" 2>&1
  "$PY" "$WATCH/bin/decide.py"        >> "$WATCH/logs/decide.log" 2>&1
  sleep "$INTERVAL"
done
