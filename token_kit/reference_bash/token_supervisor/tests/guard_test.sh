#!/usr/bin/env bash
# guard_test.sh — drives `ccsup watch` against a SYNTHETIC transcript that this
# test grows itself, with tiny SOFT/HARD dials. Rollover is redirected to a
# recording stub (CCSUP_ROLLOVER_CMD), so nothing is ever really killed.
# bash + jq only. No sleep (the sandbox blocks it): file ages are set with
# `touch -d`.
#
# Asserts: (1) below SOFT nothing happens, (2) crossing SOFT emits exactly one
# soft action and not two, (3) an idle session in the drain band rolls over,
# (4) crossing HARD rolls over, (5) a rollover is not repeated, (6) a stale
# heartbeat is reported dead.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CCSUP="$HERE/../ccsup"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccsup-guard.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FAILED=0
ok()   { printf 'ok   %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; FAILED=1; }
check(){ [[ "$2" == "$3" ]] && ok "$1 ($2)" || bad "$1: expected [$3] got [$2]"; }

CAMPAIGN="$TMP/campaign"; mkdir -p "$CAMPAIGN"
RUN="$CAMPAIGN/.ccsup"; LOG="$RUN/ccsup.log"
TR="$TMP/synthetic.jsonl"
STUB="$TMP/rollover_stub.sh"
STUBLOG="$TMP/rollover_calls.log"

cat > "$STUB" <<'EOS'
#!/usr/bin/env bash
printf 'ROLLOVER_CALLED pid=%s tokens=%s reason=%s\n' "$1" "$2" "$3" >> "$ROLLOVER_RECORD"
EOS
chmod +x "$STUB"

# tiny sealed dials for the fixture; the real ccsup.conf is untouched
cat > "$TMP/ccsup.conf" <<EOC
: "\${SOFT_TOKENS:=100}"
: "\${HARD_TOKENS:=200}"
: "\${DRAIN_WAIT_SECS:=600}"
: "\${POLL_SECS:=1}"
: "\${CAMPAIGN_DIR:=$CAMPAIGN}"
: "\${CLAUDE_HOME:=$TMP/claudehome}"
: "\${CLAUDE_BIN:=true}"
: "\${CLAUDE_FLAGS:=}"
: "\${TMUX_PREFIX:=ccsup-test}"
: "\${HEARTBEAT_STALE_SECS:=240}"
: "\${CCSUP_ROLLOVER_CMD:=$STUB}"
: "\${SEED_AS_ARG:=1}"
: "\${AUTOWATCH:=0}"
: "\${LAUNCH_PID_WAIT_SECS:=2}"
EOC

export CCSUP_CONF="$TMP/ccsup.conf" ROLLOVER_RECORD="$STUBLOG"
: > "$STUBLOG"

grow() { # grow <context-tokens>: append one main-thread assistant record
  printf '{"type":"assistant","isSidechain":false,"message":{"model":"m","usage":{"input_tokens":2,"cache_creation_input_tokens":8,"cache_read_input_tokens":%s,"output_tokens":1}}}\n' \
    "$(( $1 - 10 ))" >> "$TR"
}
# a sidechain record must NOT move the number
grow_sidechain() {
  printf '{"type":"assistant","isSidechain":true,"message":{"model":"m","usage":{"input_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":%s,"output_tokens":1}}}\n' \
    "$(( $1 - 2 ))" >> "$TR"
}
w() { "$CCSUP" watch --session-pid 424242 --transcript "$TR" --once "$@" 2>/dev/null; }
count() { grep -c "	$1	" "$LOG" 2>/dev/null || true; }

printf '== case 1: below SOFT\n'
grow 50; grow_sidechain 999999
w --status-override busy
check "no soft action below SOFT" "$(count soft_request)" "0"
check "no rollover below SOFT"    "$(count rollover_begin)" "0"
check "sidechain ignored, reading 50" "$(grep -c 'tokens=50 ' "$LOG")" "1"

printf '== case 2: cross SOFT, twice\n'
grow 150
w --status-override busy
w --status-override busy
check "exactly one soft action" "$(count soft_request)" "1"
check "still no rollover in drain band while busy" "$(count rollover_begin)" "0"
check "request file written once" "$(grep -c 'SOFT threshold crossed' "$CAMPAIGN/ROLLOVER_REQUEST.md")" "1"

printf '== case 3: drain band + session idle -> rollover\n'
w --status-override idle
check "one rollover"      "$(count rollover_begin)" "1"
check "stub called once"  "$(grep -c ROLLOVER_CALLED "$STUBLOG")" "1"
check "reason is drain_idle" "$(grep -c 'reason=drain_idle' "$STUBLOG")" "1"

printf '== case 4/5: cross HARD, twice -> no repeat\n'
grow 250
w --status-override busy
w --status-override busy
check "rollover not repeated" "$(grep -c ROLLOVER_CALLED "$STUBLOG")" "1"
check "skip rows recorded"    "$( [[ $(count rollover_skipped) -ge 1 ]] && echo yes || echo no )" "yes"

printf '== case 6: stale heartbeat is DEAD\n'
"$CCSUP" status >/dev/null 2>&1; check "fresh heartbeat -> status rc 0" "$?" "0"
touch -d '-1 hour' "$RUN/watch.heartbeat"
out="$("$CCSUP" status 2>/dev/null)"; rc=$?
check "stale heartbeat -> status rc 1" "$rc" "1"
check "stale heartbeat -> reported DEAD" "$(printf '%s' "$out" | grep -c 'watcher=DEAD')" "1"

printf '== case 7: launch DELIVERS the seed as an argv prompt, and resolves the pid\n'
# CLAUDE_BIN is a recorder: it writes the argv it was actually launched with and
# exits, so the tmux pane dies and the pid resolver is forced down its failure
# path. Nothing claude-like is started.
ARGV="$TMP/launch_argv.txt"
cat > "$TMP/recorder.sh" <<EOS
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$ARGV"
EOS
chmod +x "$TMP/recorder.sh"
: > "$ARGV"
CLAUDE_BIN="$TMP/recorder.sh" "$CCSUP" launch -- --model haiku >/dev/null 2>&1
for _i in 1 2 3 4 5 6 7 8 9 10; do [[ -s "$ARGV" ]] && break; sleep 1; done
check "launch passed the recorded flags"      "$(grep -c -- '--model haiku' "$ARGV")" "1"
check "launch passed a seed ARGUMENT"         "$(grep -c 'Rollover seed: read the file' "$ARGV")" "1"
check "the seed argument names a real file"   "$( f=$(grep -o '/[^ ]*/seed\.[^ ]*\.md' "$ARGV" | head -1); [[ -r "$f" ]] && echo yes || echo no )" "yes"
check "unresolvable pid is a loud ROW, not a crash" "$( [[ $(count launch_pid_unresolved) -ge 1 ]] && echo yes || echo no )" "yes"
check "AUTOWATCH=0 spawns no watcher"         "$(count watch_spawned)" "0"
tmux kill-session -t "$(awk -F'tmux=' '/\tlaunch\t/{split($2,a," ");n=a[1]}END{print n}' "$LOG")" 2>/dev/null

printf '== rows written (tail)\n'; tail -6 "$LOG"
if (( FAILED )); then printf 'GUARD: FAIL\n'; exit 1; fi
printf 'GUARD: PASS\n'
