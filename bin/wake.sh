#!/bin/bash
# Wake one supervisor agent turn in the monitor conversation.
# Usage: wake.sh [events|heartbeat]
#
# The message is composed by bin/turn_prompt.py: the stable operating manual is
# normally carried by the monitor session's *system prompt* (applied and
# canary-verified by bin/apply_prompt.py), because user messages are never
# dropped during compaction and would otherwise accumulate forever. Until that
# append is verified the composer returns the manual followed by the card, which
# is the old behaviour.
set -u

WATCH="$(cd "$(dirname "$0")/.." && pwd)"
# PATH for everything below: a fixed, hermetic list, built from directories that
# actually exist on this host (2026-10-18: discovery, not a hardcoded prefix — the
# old line replaced PATH with one machine's Homebrew path; inheriting the caller's
# PATH would instead let a dev working-tree build shadow the installed CLI).
PATH=""
for d in "${FUTURE_TOOLBIN:-}" /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" \
         /usr/bin /bin /usr/sbin /sbin; do
  [ -n "$d" ] && [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="${PATH:+$PATH:}$d" ;; esac
done
export PATH
FUTURE="$(command -v future || true)"
[ -n "$FUTURE" ] || FUTURE="future"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || PY="/usr/bin/python3"
REASON="${1:-events}"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$WATCH/logs/wake-$TS-$REASON.log"

mkdir -p "$WATCH/logs"
MON="$(cat "$WATCH/state/monitor_session.txt" 2>/dev/null || true)"
if [ -z "$MON" ]; then
  echo "$(date '+%F %T') [wake] no monitor session registered" >> "$WATCH/logs/wake.log"
  exit 1
fi

cd "$WATCH" || exit 1
# Roll onto a fresh monitor session when the current one gets long (context %),
# so a 7x24 supervisor never dies of context exhaustion. Prints the session id
# to wake; on any failure it prints the current id unchanged. Never rolls while
# the monitor is streaming (that would abandon a live run), and never at all when
# run from a manual/out-of-tree invocation of this script.
if [ -f "$WATCH/bin/monitor_roll.py" ]; then
  ROLLED="$("$PY" "$WATCH/bin/monitor_roll.py" 2>>"$WATCH/logs/monitor_roll.log" || true)"
  if [ -n "$ROLLED" ]; then MON="$ROLLED"; fi
fi
# Heal the manual's home before composing: editing prompts/append.md invalidates the
# canary record, and the composer then re-sends the whole manual on every wake until
# someone re-applies it (the user saw that duplicate 2026-10-16). Best-effort — a wake
# must never fail because of this, and the script exits 0 either way.
if [ -f "$WATCH/bin/ensure_append.py" ]; then
  "$PY" "$WATCH/bin/ensure_append.py" >>"$WATCH/logs/ensure_append.log" 2>&1 || true
fi
# Keep logs/ bounded (user, 2026-10-20: "logs 只保留最近 7 天"). Per-run logs expire
# by age; append-only ones rotate in place (never unlinked — a live writer holds the
# path). Best effort: a wake must never fail because housekeeping did.
if [ -f "$WATCH/bin/prune.py" ]; then
  "$PY" "$WATCH/bin/prune.py" --logs >>"$WATCH/logs/prune.log" 2>&1 || true
fi
{
  echo "=== wake $TS reason=$REASON monitor=$MON ==="
  # No `@state/turn_payload.json` attachment: the target renders an @file as a raw
  # `<file name=…>{…}</file>` JSON blob, which is unreadable in Markdown. `turn_prompt.py`
  # renders the same triage as a short markdown section inside the message instead
  # (user, 2026-10-16).
  "$FUTURE" run --session "$MON" --permission all --tools read,write,edit,shell \
      --cwd "$WATCH" \
      "$("$PY" "$WATCH/bin/turn_prompt.py")"
  echo "=== exit=$? ==="
} >> "$LOG" 2>&1
echo "$(date '+%F %T') [wake] reason=$REASON monitor=$MON log=$LOG" >> "$WATCH/logs/wake.log"
