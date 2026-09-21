#!/usr/bin/env bash
# guard_test.sh — drives lane_recycler.sh with synthetic PreToolUse stdin.
# No sleep (the sandbox blocks it), no python, no network, no real agents.
# Fixtures are built under a mktemp dir and the hook is pointed at them with
# LANE_RECYCLER_PROJECTS_ROOT / LANE_RECYCLER_STATE.
#
# A guard that cannot fail is a defect: the state-write exemption in the hook is
# removed by hand and this suite is re-run; the FLOOR-write and HARD-write cases
# then fail. That demonstration is recorded in the lane out.md.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/../lane_recycler.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

PROJ=$TMP/projects
CWD=/oscar/data/stellex/glvov
SLUG=$(printf '%s' "$CWD" | tr -c '[:alnum:]' '-')
SESSION=sess-guard
LANE=$TMP/lanes/demo/v0
mkdir -p "$LANE" "$PROJ/$SLUG/$SESSION/subagents"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1 -- $2"; }

# fixture: an agent transcript with $2 tool_use blocks whose spawn prompt names
# the lane dir the way the brief template requires.
mkfix() { # $1 agent_id  $2 n_calls
  local f="$PROJ/$SLUG/$SESSION/subagents/agent-$1.jsonl"
  jq -nc --arg t "You are lane demo/v0.
LANE_DIR: $LANE
Read your brief." '{type:"user",isSidechain:true,message:{content:[{type:"text",text:$t}]}}' > "$f"
  awk -v n="$2" 'BEGIN{for(i=0;i<n;i++)
    print "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"name\":\"Bash\"}]}}"}' >> "$f"
}

run() { # $1 agent_id ("" = main thread)  $2 tool  $3 file_path
  local out
  out=$(jq -nc --arg a "$1" --arg s "$SESSION" --arg c "$CWD" --arg t "$2" --arg p "$3" \
    'if $a=="" then {session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:$t,tool_input:{file_path:$p}}
     else {agent_id:$a,agent_type:"general-purpose",session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:$t,tool_input:{file_path:$p}} end' \
  | env LANE_RECYCLER_PROJECTS_ROOT="$PROJ" LANE_RECYCLER_STATE="$TMP/state" bash "$HOOK")
  # empty stdout + exit 0 IS the CLI's "hook has no opinion" == allow
  if [ -z "$out" ]; then echo allow
  else printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"'; fi
}

check() { # $1 label  $2 expected  $3 actual
  [ "$2" = "$3" ] && ok "$1 ($3)" || bad "$1" "expected $2, got $3"
}

# --- 1. main thread, far past every threshold, is never capped ---------------
# (tool_name Agent is NOT used here any more: a spawn now goes to the router,
#  which has its own cases in section B. The cap's discriminator is unchanged.)
mkfix main 900
check "main-thread call (no agent_id) at 900 calls" allow "$(run "" Bash "")"

# --- 2. a lane below the warn ------------------------------------------------
mkfix low 100
check "subagent below WARN (100)" allow "$(run low Bash "")"

# --- 3. the warn band: deny exactly once, then let it through ----------------
mkfix mid 160
check "subagent in WARN band, first call" deny  "$(run mid Bash "")"
check "subagent in WARN band, second call" allow "$(run mid Bash "")"
check "subagent in WARN band, third call"  allow "$(run mid Bash "")"

# --- 4. past the floor -------------------------------------------------------
mkfix hi 240
check "past FLOOR: Bash"                 deny  "$(run hi Bash "")"
check "past FLOOR: Write to lane out.md" allow "$(run hi Write "$LANE/out.md")"
check "past FLOOR: Write elsewhere"      deny  "$(run hi Write "$TMP/other.md")"
check "past FLOOR: SubagentHandback"     allow "$(run hi SubagentHandback "")"
check "past FLOOR: tool_name is Task"    deny  "$(run hi Task "")"
check "past FLOOR: Read of own in.md"    allow "$(run hi Read "$LANE/in.md")"

# --- 5. past the hard ceiling: out.md STILL permitted, reason escalates ------
mkfix vhi 260
check "past HARD: Write to out.md still permitted" allow "$(run vhi Write "$LANE/out.md")"
D=$(jq -nc --arg a vhi --arg s "$SESSION" --arg c "$CWD" \
   '{agent_id:$a,session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{}}' \
   | env LANE_RECYCLER_PROJECTS_ROOT="$PROJ" LANE_RECYCLER_STATE="$TMP/state" bash "$HOOK")
case "$(printf '%s' "$D" | jq -r .hookSpecificOutput.permissionDecisionReason)" in
  *"PAST HARD CEILING"*) ok "past HARD: reason escalates" ;;
  *) bad "past HARD: reason escalates" "reason was: $D" ;;
esac

# --- 6. the deny reason is the channel: it must carry the live count ---------
D=$(jq -nc --arg a mid2 --arg s "$SESSION" --arg c "$CWD" \
   '{agent_id:$a,session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{}}')
mkfix mid2 171
R=$(printf '%s' "$D" | env LANE_RECYCLER_PROJECTS_ROOT="$PROJ" LANE_RECYCLER_STATE="$TMP/state" \
    bash "$HOOK" | jq -r .hookSpecificOutput.permissionDecisionReason)
case "$R" in *"calls=171"*) ok "WARN reason carries the live count" ;;
  *) bad "WARN reason carries the live count" "got: $R" ;; esac

# --- 7. the respawn request is written once, and is a row --------------------
n=$(grep -c "lane=$LANE" "$LANE/RESPAWN_REQUEST.md" 2>/dev/null || echo 0)
[ "$n" -ge 2 ] && ok "RESPAWN_REQUEST rows appended (one per recycled agent: $n)" \
  || bad "RESPAWN_REQUEST rows" "expected >=2 (agents hi, vhi), got $n"
m=$(grep -c "agent=hi" "$LANE/RESPAWN_REQUEST.md" 2>/dev/null || echo 0)
[ "$m" = 1 ] && ok "RESPAWN_REQUEST is one-shot per agent" \
  || bad "RESPAWN_REQUEST one-shot" "agent=hi appears $m times"

# --- 8. unresolvable transcript fails OPEN and says so ----------------------
check "unknown agent fails open" allow "$(run ghost Bash "")"
grep -q unresolved "$TMP/state/$SESSION/ghost/events.tsv" \
  && ok "fail-open is logged loudly" || bad "fail-open logged" "no unresolved row"

# ===========================================================================
# B. THE ROUTER. Every case below fails if the Agent branch is removed from
#    lane_recycler.sh, and the budget case fails if the hook goes back to
#    reading WARN/FLOOR/HARD out of lane_recycler.conf.
# ===========================================================================
MATRIX="$HERE/../../../agent_trigger_matrix.toml"
RSTATE="$TMP/rstate"
[ -r "$MATRIX" ] || { bad "B0 matrix unreadable" "$MATRIX"; }

spawn() { # $1 description  $2 prompt  $3 session (default rsess)  [$4 matrix]
  jq -nc --arg d "$1" --arg p "$2" --arg s "${3:-rsess}" \
    '{session_id:$s,cwd:"/oscar/data/stellex/glvov",hook_event_name:"PreToolUse",
      tool_name:"Agent",tool_input:{description:$d,prompt:$p,
      subagent_type:"general-purpose",model:"sonnet"}}' \
  | env LANE_RECYCLER_STATE="$RSTATE" LANE_RECYCLER_MATRIX="${4:-$MATRIX}" bash "$HOOK"
}
reason()   { printf '%s' "$1" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""'; }
decision() { local d; d=$(printf '%s' "$1" | jq -r '.hookSpecificOutput.permissionDecision // ""')
             [ -n "$d" ] && echo "$d" || echo allow; }
newprompt(){ printf '%s' "$1" | jq -r '.hookSpecificOutput.updatedInput.prompt // ""'; }
field()    { printf '%s' "$1" | jq -r --arg k "$2" '.hookSpecificOutput.updatedInput[$k] // ""'; }

# --- B1. the description routes, and the rewrite really rewrites -------------
O=$(spawn "squeue check on the training node" "do the thing")
case "$(reason "$O")" in *"row cluster-state"*) ok "B1 description routes to cluster-state" ;;
  *) bad "B1 description routing" "got: $(reason "$O")" ;; esac
[ "$(field "$O" model)" = "sonnet" ] && ok "B1 model is rewritten from the row" \
  || bad "B1 model rewrite" "got $(field "$O" model)"
case "$(newprompt "$O")" in "[MATRIX ROW: cluster-state"*) ok "B1 header is prepended to the prompt" ;;
  *) bad "B1 prompt header" "prompt starts: $(newprompt "$O" | head -c 60)" ;; esac

# --- B2. THE RULING: topics match the description ONLY, never the body -------
# This body is what every brief we write contains. Under body matching it would
# select watch-poll-wait and be REFUSED.
BODY="Every claim is a LEAD TO VERIFY. No background waits, no sleep, no polling,
no Monitor tool. Do not watch or wait for anything."
O=$(spawn "squeue check on the training node" "$BODY")
case "$(reason "$O")" in *"row cluster-state"*) ok "B2 body words do NOT route (ruling)" ;;
  *) bad "B2 body must not route" "got: $(reason "$O")" ;; esac
[ "$(decision "$O")" = allow ] && ok "B2 that spawn is not refused" \
  || bad "B2 not refused" "decision $(decision "$O")"

# --- B3. an explicit ROW: line beats the description -------------------------
O=$(spawn "squeue check on the training node" "ROW: harness-broker
now debug the broker")
case "$(reason "$O")" in *"row harness-broker (row_line)"*) ok "B3 ROW: line wins" ;;
  *) bad "B3 ROW: line" "got: $(reason "$O")" ;; esac
[ "$(field "$O" subagent_type)" = "grizzly-veteran" ] && ok "B3 subagent_type is rewritten" \
  || bad "B3 subagent_type rewrite" "got $(field "$O" subagent_type)"
[ "$(field "$O" model)" = "opus" ] && ok "B3 model follows the row (opus)" \
  || bad "B3 model" "got $(field "$O" model)"

# --- B4. an unknown ROW: name is denied, and the deny names the valid rows ---
O=$(spawn "x" "ROW: nosuchrow")
check "B4 unknown row is denied" deny "$(decision "$O")"
case "$(reason "$O")" in *"cluster-state"*"harness-broker"*) ok "B4 deny carries the menu" ;;
  *) bad "B4 deny carries the menu" "got: $(reason "$O" | head -c 80)" ;; esac

# --- B5. refuse-and-substitute ----------------------------------------------
O=$(spawn "babysit the training run" "x")
check "B5 watch-poll-wait refuses" deny "$(decision "$O")"
case "$(reason "$O")" in *"may not be the loop"*) ok "B5 refusal carries the substitute" ;;
  *) bad "B5 substitute" "got: $(reason "$O" | head -c 80)" ;; esac

# --- B6. globs, when the description says nothing ----------------------------
O=$(spawn "please handle this" "the file is /oscar/data/stellex/glvov/wbc/wbc-latest/launch/x.sbatch")
case "$(reason "$O")" in *"row launcher-edit (globs)"*) ok "B6 globs route when topics do not" ;;
  *) bad "B6 glob routing" "got: $(reason "$O")" ;; esac

# --- B7. unrouted: deny ONCE per session, with the menu, then fail open ------
O=$(spawn "frobnicate the widget" "zzz" b7sess)
check "B7 unrouted denies once" deny "$(decision "$O")"
case "$(reason "$O")" in *"ROW: cluster-state"*) ok "B7 the deny carries the row menu" ;;
  *) bad "B7 menu in deny" "got: $(reason "$O" | head -c 80)" ;; esac
O=$(spawn "frobnicate the widget" "zzz" b7sess)
check "B7 the retry goes through" allow "$(decision "$O")"
case "$(reason "$O")" in *unrouted*) ok "B7 retry runs on [defaults]" ;;
  *) bad "B7 retry defaults" "got: $(reason "$O")" ;; esac

# --- B8. [budget] is a READ of the matrix, not a copy in the conf ------------
# Change ONE number in a copy of the matrix and the hook's behaviour must change.
LOWMX="$TMP/low.toml"
sed 's/^warn = 150/warn = 5/' "$MATRIX" > "$LOWMX"
grep -q '^warn = 5' "$LOWMX" && ok "B8 fixture: warn lowered to 5 in a matrix copy" \
  || bad "B8 fixture" "sed did not lower warn"
mkfix b8 10        # 10 calls: under the real warn of 150, over the fixture's 5
R=$(jq -nc --arg a b8 --arg s "$SESSION" --arg c "$CWD" \
     '{agent_id:$a,session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{}}' \
   | env LANE_RECYCLER_PROJECTS_ROOT="$PROJ" LANE_RECYCLER_STATE="$TMP/s8a" \
         LANE_RECYCLER_MATRIX="$MATRIX" bash "$HOOK")
check "B8 10 calls under the real matrix budget" allow "$(decision "$R")"
R=$(jq -nc --arg a b8 --arg s "$SESSION" --arg c "$CWD" \
     '{agent_id:$a,session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{}}' \
   | env LANE_RECYCLER_PROJECTS_ROOT="$PROJ" LANE_RECYCLER_STATE="$TMP/s8b" \
         LANE_RECYCLER_MATRIX="$LOWMX" bash "$HOOK")
check "B8 same 10 calls, warn=5 in the matrix" deny "$(decision "$R")"
case "$(reason "$R")" in *"warn=5"*) ok "B8 the matrix number reaches the deny reason" ;;
  *) bad "B8 matrix number in reason" "got: $(reason "$R" | head -c 80)" ;; esac

# --- B9. the menu is GENERATED from the toml, so it cannot drift -------------
RENAMED="$TMP/renamed.toml"
sed 's/^name = "cluster-state"/name = "queue-state"/' "$MATRIX" > "$RENAMED"
# NB: capture, never `hook --menu | grep -q` — grep -q closes the pipe early and
# `set -o pipefail` then reports the SIGPIPE as a failure of the whole case.
M9=$(env LANE_RECYCLER_STATE="$RSTATE" LANE_RECYCLER_MATRIX="$RENAMED" bash "$HOOK" --menu)
case "$M9" in *"ROW: queue-state"*) ok "B9 renaming a row renames it in the menu" ;;
  *) bad "B9 menu is generated" "queue-state absent from the menu" ;; esac
case "$M9" in *"ROW: cluster-state"*) bad "B9 menu is stale" "old name still in the menu" ;;
  *) ok "B9 the old name is gone from the menu" ;; esac
n=$(env LANE_RECYCLER_STATE="$RSTATE" LANE_RECYCLER_MATRIX="$MATRIX" bash "$HOOK" --menu | wc -w)
[ "$n" -lt 400 ] && ok "B9 menu is compact ($n words, target under ~400 tokens)" \
  || bad "B9 menu size" "$n words"

# --- B10. the delegation header appears ONLY when codex is on this host ------
# Operator ruling 2026-09-20: codex runs on demand; availability is the binary,
# not an endpoint file. Both halves are driven by pointing [codex].binary at a
# name that does/does not exist, so the case runs the same on any host.
ABSENT="$TMP/absent.toml"
sed 's/^binary = .*/binary = "codex-no-such-binary-here"/' "$MATRIX" > "$ABSENT"
O=$(spawn "please handle this" "ROW: launcher-edit
edit it" cxsess1 "$ABSENT")
case "$(newprompt "$O")" in *"delegation: you own the judgement"*)
    bad "B10 header with no codex" "delegation header present while codex is absent" ;;
  *) ok "B10 no codex binary -> no delegation header" ;; esac
grep -q codex_fallback "$RSTATE/cxsess1/spawn_events.tsv" 2>/dev/null \
  && ok "B10 the fallback is logged loudly" || bad "B10 codex_fallback row" "no row"
FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
printf '#!/bin/sh\nexit 0\n' > "$FAKEBIN/codex-guard-stub"; chmod +x "$FAKEBIN/codex-guard-stub"
PRESENT="$TMP/present.toml"
sed 's/^binary = .*/binary = "codex-guard-stub"/' "$MATRIX" > "$PRESENT"
O=$(PATH="$FAKEBIN:$PATH" spawn "please handle this" "ROW: launcher-edit
edit it" cxsess2 "$PRESENT")
case "$(newprompt "$O")" in *"delegation: you own the judgement"*"codex_fleet.sh"*)
    ok "B10 codex present -> the dispatcher command is in the header" ;;
  *) bad "B10 present-codex header" "delegation header missing" ;; esac
case "$(newprompt "$O")" in *"refusal code"*)
    ok "B10 the header names the dispatcher refusal code" ;;
  *) bad "B10 refusal code in header" "absent" ;; esac
# a codex-ENGINE row with no wrapper agent falls back to its Claude model
O=$(PATH="$FAKEBIN:$PATH" spawn "squeue check" "x" cxsess3 "$PRESENT")
[ "$(field "$O" subagent_type)" = "general-purpose" ] \
  && ok "B10 codex row with no wrapper falls back to Claude" \
  || bad "B10 codex row fallback" "subagent_type $(field "$O" subagent_type)"

# --- B11. a matrix that will not parse FAILS OPEN, loudly --------------------
BADMX="$TMP/broken.toml"
printf 'this is not toml at all\n' > "$BADMX"
O=$(spawn "squeue check" "x" badsess "$BADMX")
check "B11 unparsable matrix allows the spawn" allow "$(decision "$O")"
grep -q matrix_parse_failed "$RSTATE/badsess/spawn_events.tsv" 2>/dev/null \
  && ok "B11 the parse failure is logged loudly" || bad "B11 parse-failure row" "no row"

echo "---"
echo "GUARD: $([ "$FAIL" = 0 ] && echo PASS || echo FAIL)  pass=$PASS fail=$FAIL"
[ "$FAIL" = 0 ]
