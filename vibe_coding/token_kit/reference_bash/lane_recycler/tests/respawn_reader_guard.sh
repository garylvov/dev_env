#!/usr/bin/env bash
# respawn_reader_guard.sh — drives respawn_reader.sh with synthetic hook stdin.
# bash + jq only; nothing is installed, no hook is registered, no agent spawned.
#
# Asserts: (1) a spawn with no respawn row says nothing, (2) a recycled lane is
# reported to the MAIN thread on PostToolUse(Agent), (3) the same row is never
# reported twice, (4) the row is marked consumed by APPENDING and the request
# file is byte-identical afterwards, (5) Stop sweeps a lane registered earlier
# in the session — the background-agent case PostToolUse cannot catch, (6) Stop
# says nothing for another session's lanes, (7) the 4th recycle flips the
# message to STOP, (8) stop_hook_active short-circuits (no turn loop),
# (9) a non-Agent tool is ignored.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
READER="$HERE/../respawn_reader.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/respawn-guard.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FAILED=0
ok()   { printf 'ok   %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; FAILED=1; }
check(){ [[ "$2" == "$3" ]] && ok "$1 ($2)" || bad "$1: expected [$3] got [$2]"; }

LANE="$TMP/lanes/demo/v0";  mkdir -p "$LANE"; : > "$LANE/in.md"
OTHER="$TMP/lanes/other/v0"; mkdir -p "$OTHER"; : > "$OTHER/in.md"
export RESPAWN_READER_STATE="$TMP/state" RESPAWN_MAX=3
SESS=s-1111

recycle() { # <lane dir> <calls> — exactly the row lane_recycler.sh appends
  printf '%s\tlane=%s\tagent=ag-%s\tcalls=%s\treason=floor\tresume=in.md+out.md\n' \
    "$(date -Is)" "$1" "$2" "$2" >> "$1/RESPAWN_REQUEST.md"
}

post() { # <lane dir> [tool] -> reader stdout
  jq -n --arg s "$SESS" --arg t "${2:-Agent}" --arg p "LANE_DIR: $1
brief follows" '{hook_event_name:"PostToolUse",session_id:$s,cwd:"/x",
    tool_name:$t,tool_input:{prompt:$p},tool_response:{},tool_use_id:"tu1"}' \
  | "$READER" 2>/dev/null
}
stop() { # [session] [stop_hook_active] -> reader stdout
  jq -n --arg s "${1:-$SESS}" --argjson a "${2:-false}" \
    '{hook_event_name:"Stop",session_id:$s,cwd:"/x",stop_hook_active:$a}' \
  | "$READER" 2>/dev/null
}
ctx() { jq -r '.hookSpecificOutput.additionalContext // ""' 2>/dev/null; }

printf '== case 1: a spawn with no respawn row is silent\n'
out="$(post "$LANE")"
check "silent when nothing was recycled" "${out:-EMPTY}" "EMPTY"
check "but the lane got REGISTERED" "$(grep -c "$LANE\$" "$RESPAWN_READER_STATE/respawn_registry.tsv")" "1"

printf '== case 2/3/4: a recycled lane is reported once, and only once\n'
recycle "$LANE" 231
before="$(md5sum < "$LANE/RESPAWN_REQUEST.md")"
msg="$(post "$LANE" | ctx)"
check "reported to the main thread"       "$(printf '%s' "$msg" | grep -c 'respawn a fresh agent on the same in.md')" "1"
check "message carries the call count"    "$(printf '%s' "$msg" | grep -c 'recycled at 231 calls')" "1"
check "message names the lane"            "$(printf '%s' "$msg" | grep -c "$LANE")" "1"
out2="$(post "$LANE")"
check "the same row is not reported twice" "${out2:-EMPTY}" "EMPTY"
check "request file untouched (append-only ledger)" "$(md5sum < "$LANE/RESPAWN_REQUEST.md")" "$before"
check "consumption recorded as one appended row" "$(grep -c respawn_no= "$LANE/RESPAWN_CONSUMED.md")" "1"

printf '== case 5: Stop sweeps the registry (the background-agent case)\n'
recycle "$LANE" 245
msg="$(stop | ctx)"
check "Stop reports the new row"  "$(printf '%s' "$msg" | grep -c 'recycled at 245 calls')" "1"
check "Stop does not repeat it"   "$(stop | ctx)" ""

printf '== case 6: another session sees nothing\n'
recycle "$LANE" 246
check "other session sweeps nothing" "$(stop s-9999 | ctx)" ""
stop >/dev/null   # consume it as the owning session

printf '== case 7: the 4th recycle flips to STOP\n'
recycle "$LANE" 247
msg="$(stop | ctx)"
check "cap reached -> STOP, not respawn" "$(printf '%s' "$msg" | grep -c '^STOP — lane')" "1"
check "cap message forbids respawn"      "$(printf '%s' "$msg" | grep -c 'Do NOT respawn it again')" "1"
check "cap message says read out.md"     "$(printf '%s' "$msg" | grep -c 'out.md yourself')" "1"

printf '== case 8/9: loop guard and tool filter\n'
recycle "$OTHER" 233
check "stop_hook_active short-circuits" "$(stop "$SESS" true | ctx)" ""
check "non-Agent PostToolUse ignored"   "$(post "$OTHER" Read | ctx)" ""
check "Agent PostToolUse still reports" "$(post "$OTHER" | ctx | grep -c 'recycled at 233 calls')" "1"

if (( FAILED )); then printf 'GUARD: FAIL\n'; exit 1; fi
printf 'GUARD: PASS\n'
