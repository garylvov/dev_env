#!/usr/bin/env bash
# respawn_reader.sh — the READER for the directive lane_recycler.sh writes.
#
# THE DEFECT IT CLOSES: lane_recycler.sh appends a row to
# <LANE_DIR>/RESPAWN_REQUEST.md when it recycles a lane at FLOOR, and NOTHING
# has ever read that file. A stamped-but-unread directive is the same defect as
# a writer with no reader (reader/writer law). This script is that reader.
#
# WHY NOT SubagentStop. It is the obvious event and it is the WRONG one. The
# installed CLI (2.1.278) describes its output as: "Hook-specific output for the
# SubagentStop event. additionalContext is non-error feedback delivered to the
# SUBAGENT; the subagent continues so it can act on it." A SubagentStop hook
# therefore restarts the dying lane instead of telling the main thread anything
# — exactly what the recycler is trying to stop. The main thread is reachable
# from PostToolUse and from Stop, whose additionalContext is "delivered to the
# model" of the thread the hook fired in.
#
# SO IT IS TWO ENTRY POINTS, ONE SCRIPT, dispatched on hook_event_name:
#   PostToolUse (matcher "Agent") — fires in the MAIN thread when a spawn
#     returns. Two jobs: REGISTER the lane dir named in the spawn prompt, and
#     report any unconsumed respawn row. A FOREGROUND agent's row is already
#     there when this fires; a BACKGROUND agent's tool_result comes back
#     immediately (verified previously for both Agent calls in a live
#     transcript), so registration is the only useful half for those — hence:
#   Stop — fires at the end of every main-thread turn and sweeps the registry,
#     which is what actually catches a background lane that died minutes later.
#
# It never rewrites anything: the request file is append-only and consumption is
# recorded by APPENDING to a separate ledger, so both files stay one-writer,
# `cat >>`-only on NFS.
#
# BOUNDED: after RESPAWN_MAX respawns for one lane dir the message flips from
# "respawn" to "stop reading this lane back into the loop".
#
# Install (PROPOSED, never applied by this script) in settings.json:
#   hooks.PostToolUse[] = {"matcher":"Agent","hooks":[{"type":"command",
#     "command":".../tools/lane_recycler/respawn_reader.sh"}]}
#   hooks.Stop[]        = {"hooks":[{"type":"command",
#     "command":".../tools/lane_recycler/respawn_reader.sh"}]}

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=respawn_reader.conf
[ -r "$HERE/respawn_reader.conf" ] && . "$HERE/respawn_reader.conf"
MAX=${RESPAWN_MAX:-3}
STATE_ROOT=${RESPAWN_READER_STATE:-${LANE_RECYCLER_STATE:-${CLAUDE_PROJECT_DIR:-$HOME}/.lane_recycler}}
REQUEST_NAME=${RESPAWN_REQUEST_NAME:-RESPAWN_REQUEST.md}
LEDGER_NAME=${RESPAWN_LEDGER_NAME:-RESPAWN_CONSUMED.md}

silent() { exit 0; }   # no stdout == "this hook has no opinion"

STDIN_JSON=$(cat)
[ -n "$STDIN_JSON" ] || silent

eval "$(printf '%s' "$STDIN_JSON" | jq -r '
  @sh "EVENT=\(.hook_event_name // "")
       SESSION=\(.session_id // "")
       TOOL=\(.tool_name // "")
       STOP_ACTIVE=\(.stop_hook_active // false)
       PROMPT=\(.tool_input.prompt // "")"' 2>/dev/null)"
[ -n "${EVENT:-}" ] || silent
# A Stop hook that speaks while it is already re-running is how a turn loops
# forever. One pass only.
[ "${STOP_ACTIVE:-false}" = "true" ] && silent

REG="$STATE_ROOT/respawn_registry.tsv"
mkdir -p "$STATE_ROOT" 2>/dev/null || silent

lane_from_prompt() { # the spawn prompt names its lane; same rule the recycler uses
  printf '%s' "$1" | grep -oE 'LANE_DIR:[[:space:]]*/[^[:space:]]+' | head -1 \
    | sed 's/^LANE_DIR:[[:space:]]*//'
}

register() { # <lane dir> — append-only; duplicates are fine, reads dedup
  [ -n "$1" ] || return 0
  [ -d "$1" ] || return 0
  printf '%s\t%s\t%s\n' "$(date -Is)" "${SESSION:-none}" "$1" >> "$REG" 2>/dev/null
}

rows_of() { # <file> -> count of non-blank rows, 0 if absent
  # NOT `grep -c ... || printf 0`: grep -c prints 0 AND exits 1 on no match, so
  # that idiom emits "0\n0" and every arithmetic test downstream then explodes.
  local n=0
  [ -r "$1" ] && n=$(grep -c '[^[:space:]]' "$1" 2>/dev/null)
  printf '%s\n' "${n:-0}"
}

# ---- the message for ONE lane with unconsumed rows, or nothing --------------
report_lane() { # <lane dir>
  local dir="$1" req="$1/$REQUEST_NAME" led="$1/$LEDGER_NAME" nreq ncon last calls agent
  [ -r "$req" ] || return 1
  nreq=$(rows_of "$req"); ncon=$(rows_of "$led")
  [ "$nreq" -gt "$ncon" ] 2>/dev/null || return 1

  last=$(grep '[^[:space:]]' "$req" | tail -1)
  calls=$(printf '%s' "$last" | grep -oE 'calls=[0-9]+' | head -1 | cut -d= -f2)
  agent=$(printf '%s' "$last" | grep -oE 'agent=[^[:space:]]+' | head -1 | cut -d= -f2)
  # Consumption is an APPENDED row, never a rewrite of the request file.
  printf '%s\tconsumed_by=%s\tsession=%s\trespawn_no=%s\n' \
    "$(date -Is)" "${EVENT}" "${SESSION:-none}" "$((ncon + 1))" >> "$led"

  if [ "$((ncon + 1))" -gt "$MAX" ]; then
    printf 'STOP — lane %s has been recycled %s times (cap %s). Do NOT respawn it again. Read %s/out.md yourself and decide what to do; the lane is not converging.\n' \
      "$dir" "$((ncon + 1))" "$MAX" "$dir"
  else
    printf 'lane %s was recycled at %s calls (agent %s; respawn %s of %s); respawn a fresh agent on the same in.md — %s/in.md; its out.md holds the RESUME block, whose first line is the single next command.\n' \
      "$dir" "${calls:-?}" "${agent:-?}" "$((ncon + 1))" "$MAX" "$dir"
  fi
  return 0
}

emit() { # <event> <text>
  jq -n --arg e "$1" --arg c "$2" \
    '{hookSpecificOutput:{hookEventName:$e,additionalContext:$c}}'
  exit 0
}

MSG=""
add() { [ -n "$1" ] || return 0; MSG="${MSG:+$MSG
}$1"; }

case "$EVENT" in
  PostToolUse)
    [ "$TOOL" = "Agent" ] || [ "$TOOL" = "Task" ] || silent
    LANE="$(lane_from_prompt "${PROMPT:-}")"
    register "$LANE"
    [ -n "$LANE" ] && add "$(report_lane "$LANE")"
    ;;
  Stop)
    # Sweep every lane dir this session ever spawned. Registry rows only: no
    # directory walk, no process scan.
    [ -r "$REG" ] || silent
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      add "$(report_lane "$d")"
    done <<< "$(awk -F'\t' -v s="${SESSION:-none}" '$2==s {print $3}' "$REG" | awk '!seen[$0]++')"
    ;;
  *) silent ;;
esac

[ -n "$MSG" ] || silent
emit "$EVENT" "$MSG"
