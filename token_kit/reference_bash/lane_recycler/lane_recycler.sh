#!/usr/bin/env bash
# lane_recycler.sh — PreToolUse hook. TWO jobs, one file:
#
#  A) THE ROUTER (tool_name == "Agent"): reads agent_trigger_matrix.toml and
#     REWRITES the spawn through hookSpecificOutput.updatedInput — model,
#     subagent_type, and a header prepended to the prompt. This is the matrix's
#     only reader. PROVEN in CLI 2.1.278 (probe: lanes/matrix_live/v0/out.md).
#
#  B) THE CALL CAP (every other tool, inside a lane): lanes grind, hit a
#     budget, die with their state in out.md, and are replaced by a fresh
#     agent. The budget numbers are READ from the matrix's [budget] block;
#     lane_recycler.conf carries only an explicit override.
#
# THE TRAP (B) SOLVES: a hard deny at the cap denies the very Write the dying
# agent needs to save its state, so the lane dies with nothing. Hence TWO
# thresholds — a one-shot WARN deny that tells the agent its count, and a FLOOR
# past which ONLY the lane's own out.md write and SubagentHandback survive.
#
# WHAT IT READS (no counter of its own; the CLI is the sole writer):
#   $PROJECTS_ROOT/<slug>/<session_id>/subagents/agent-<agent_id>.jsonl
#   slug = cwd with every non-alphanumeric replaced by '-'
#   calls = number of tool_use blocks in that file (verified live: the record
#   for the call being gated is already present when the hook runs)
#   lane dir = the `LANE_DIR: <abs path>` line in the spawn prompt, which is the
#   FIRST record of that same file. Nothing else has to be registered anywhere.
#
# DISCRIMINATOR: a subagent's PreToolUse stdin carries `agent_id`; the main
# thread has none, and session_id is SHARED, so presence of agent_id is the only
# test. No process scan is performed anywhere here (law 14 cannot bite).
#
# ROUTING ORDER (operator ruling 2026-09-20, do not soften):
#   1. an explicit `ROW: <name>` line in the spawn prompt wins. Unknown name =
#      deny, naming the valid rows.
#   2. otherwise the row's `topics` are matched as whole words/phrases against
#      the spawn's `description` ONLY — never the prompt body. Every brief we
#      write says "no background waits" and "do not poll", so body matching
#      sends nearly every spawn to watch-poll-wait, which REFUSES.
#   3. otherwise `globs` against paths found in the prompt.
#   4. otherwise [defaults], plus an `unrouted` event row, plus ONE deny per
#      session carrying the row menu so the caller can name a row next time.
#
# FAIL OPEN, LOUDLY: any resolution or parse failure allows the call and
# appends a row. A budget that wedges a lane, or a router that blocks a spawn,
# is worse than one that misses.
#
# MODES (not a hook; for the guard, the menu and humans):
#   --menu           print the row menu that a SessionStart hook injects
#   --session-start  the same menu as SessionStart additionalContext JSON
#   --compile        print the matrix compiled to JSON (awk; no python)
#
# Install (PROPOSED, never applied by this script) in settings.json — see
# lanes/matrix_live/v0/out.md for the exact snippet.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lane_recycler.conf
[ -r "$HERE/lane_recycler.conf" ] && . "$HERE/lane_recycler.conf"
STATE_ROOT=${LANE_RECYCLER_STATE:-${CLAUDE_PROJECT_DIR:-$HOME}/.lane_recycler}
PROJECTS_ROOT=${LANE_RECYCLER_PROJECTS_ROOT:-$HOME/.claude/projects}
MATRIX=${LANE_RECYCLER_MATRIX:-$HERE/../../agent_trigger_matrix.toml}
CACHE_ROOT=${LANE_RECYCLER_CACHE:-$STATE_ROOT/matrix_cache}
DATA_ROOT=${LANE_RECYCLER_DATA_ROOT:-/oscar/data/stellex/glvov}

allow() { exit 0; }   # silence + exit 0 == "hook has no opinion"

deny() { # $1 = reason text
  jq -n --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",
    permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

row() { # append one row; never rewrite a file (NFS: one writer, cat >>)
  [ -n "${EVENTS:-}" ] || return 0
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date -Is)" "${AGENT_ID:-none}" \
    "${CALLS:--1}" "$1" "$2" >> "$EVENTS"
}

claim() { # one-shot marker via noclobber; rc 0 only for the first claimant
  mkdir -p "$(dirname "$1")" 2>/dev/null
  ( set -o noclobber; : > "$1" ) 2>/dev/null
}

# ===========================================================================
# The TOML -> JSON compiler. awk only (no python on this box, no toml tool).
# It understands exactly the subset this matrix uses: [table], [[row]],
# key = "string" | 123 | ["a", "b"] possibly spanning lines, and trailing
# `#` comments outside quotes. Anything else is mis-read — a known limit, and
# the reason a parse failure fails OPEN rather than guessing.
# ===========================================================================
MATRIX_AWK='
function jesc(s){gsub(/\\/,"\\\\",s);gsub(/"/,"\\\"",s);gsub(/\t/," ",s);return s}
function strip(line,  i,c,q,out){q=0;out="";
  for(i=1;i<=length(line);i++){c=substr(line,i,1);
    if(c=="\"")q=1-q;
    if(c=="#"&&q==0)break;
    out=out c}
  sub(/[ \t]+$/,"",out); return out}
function kv(line,  k,v,p,i,res,n){
  p=index(line,"="); k=substr(line,1,p-1); v=substr(line,p+1);
  gsub(/^[ \t]+|[ \t]+$/,"",k); gsub(/^[ \t]+|[ \t]+$/,"",v);
  if(substr(v,1,1)=="["){
    res=""; n=0;
    while(match(v,/"[^"]*"/)){
      if(n++)res=res ",";
      res=res "\"" jesc(substr(v,RSTART+1,RLENGTH-2)) "\"";
      v=substr(v,RSTART+RLENGTH)}
    return "\"" k "\":[" res "]"}
  if(substr(v,1,1)=="\""){
    i=length(v); while(i>1 && substr(v,i,1)!="\"")i--;
    return "\"" k "\":\"" jesc(substr(v,2,i-2)) "\""}
  if(v ~ /^-?[0-9]+$/) return "\"" k "\":" v;
  if(v=="true"||v=="false") return "\"" k "\":" v;
  return "\"" k "\":\"" jesc(v) "\""}
function add(frag){ if(inrow){ if(rowbuf!="")rowbuf=rowbuf ","; rowbuf=rowbuf frag }
  else if(tbl!=""){ if(tbuf!="")tbuf=tbuf ","; tbuf=tbuf frag } }
function flushrow(){ if(inrow){ if(rows!="")rows=rows ","; rows=rows "{" rowbuf "}";
  rowbuf=""; inrow=0 } }
function flushtbl(){ if(tbl!=""){ if(tables!="")tables=tables ",";
  tables=tables "\"" tbl "\":{" tbuf "}"; tbuf=""; tbl="" } }
{
  L=strip($0)
  if(L ~ /^[ \t]*$/) next
  if(pend){ buf=buf " " L; if(index(L,"]")>0){ add(kv(buf)); pend=0; buf="" } next }
  if(L ~ /^\[\[row\]\]/){ flushrow(); flushtbl(); inrow=1; rowbuf=""; next }
  if(L ~ /^\[[a-z_]+\]/){ flushrow(); flushtbl(); tbl=substr(L,2,index(L,"]")-2); tbuf=""; next }
  if(L ~ /^[a-z_]+[ \t]*=/){
    if(L ~ /=[ \t]*\[/ && index(substr(L,index(L,"[")),"]")==0){ pend=1; buf=L; next }
    add(kv(L)) }
}
END{ flushrow(); flushtbl();
  printf "{%s%s\"rows\":[%s]}\n", tables, (tables!="" ? "," : ""), rows }
'

MJ=""
compile_matrix() {
  [ -r "$MATRIX" ] || return 1
  local key tmp dir
  # keyed on the matrix's mtime AND size AND path: a guard run points the hook
  # at a temp copy, and two different files can share an mtime.
  key=$(stat -c '%Y-%s' "$MATRIX" 2>/dev/null) || return 1
  dir="$CACHE_ROOT/$(printf '%s' "$MATRIX" | tr -c '[:alnum:]' '-')"
  MJ="$dir/matrix-$key.json"
  [ -s "$MJ" ] && return 0
  CACHE_ROOT="$dir"
  mkdir -p "$CACHE_ROOT" 2>/dev/null || return 1
  tmp="$MJ.$$"
  awk "$MATRIX_AWK" "$MATRIX" > "$tmp" 2>/dev/null
  if ! jq -e 'has("rows") and (.rows|length>0)' "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"; return 1
  fi
  mv -f "$tmp" "$MJ" 2>/dev/null || { rm -f "$tmp"; return 1; }
  return 0
}

menu_text() { # generated FROM the toml, so it cannot drift
  printf '%s\n' "AGENT TRIGGER MATRIX — put a line 'ROW: <name>' in every Agent spawn prompt." \
                "The row sets the model, the agent and the work shape. Pick by 'use when'."
  jq -r '.rows[] |
      "  ROW: " + .name
      + " [" + (.shape // "claude-direct") + "]"
      + " — " + (.use_when // "(no use_when)")' "$MJ"
  printf '%s\n' "Shapes: codex-direct = luna high does it all; claude-direct = the Claude model does it;" \
                "claude-plans-codex-executes = Claude judges, every mechanistic sub-step goes to codex;" \
                "refuse = the spawn is denied and a shell substitute is given."
}

case "${1:-}" in
  --compile)       compile_matrix || { echo "MATRIX PARSE FAILED: $MATRIX" >&2; exit 1; }
                   cat "$MJ"; exit 0 ;;
  --menu)          compile_matrix || { echo "MATRIX PARSE FAILED: $MATRIX" >&2; exit 1; }
                   menu_text; exit 0 ;;
  --session-start) compile_matrix || exit 0
                   jq -n --arg c "$(menu_text)" \
                     '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$c}}'
                   exit 0 ;;
esac

STDIN_JSON=$(cat)
[ -n "$STDIN_JSON" ] || allow

eval "$(printf '%s' "$STDIN_JSON" | jq -r '
  @sh "AGENT_ID=\(.agent_id // "")
       SESSION=\(.session_id // "")
       CWD=\(.cwd // "")
       TOOL=\(.tool_name // "")
       TARGET=\(.tool_input.file_path // "")"' 2>/dev/null)"

# --- budget is a READ of the matrix; the conf is an override only ------------
MW=""; MF=""; MH=""
if compile_matrix; then
  IFS=$'\t' read -r MW MF MH < <(jq -r '[(.budget.warn//empty),(.budget.floor//empty),
                                         (.budget.hard//empty)]|@tsv' "$MJ" 2>/dev/null)
fi
WARN=${LANE_RECYCLER_WARN:-${MW:-150}}
FLOOR=${LANE_RECYCLER_FLOOR:-${MF:-230}}
HARD=${LANE_RECYCLER_HARD:-${MH:-250}}

# ===========================================================================
# A) THE ROUTER — before the agent_id discriminator, because a spawn comes
#    from the main thread and has no agent_id at all.
# ===========================================================================
if [ "${TOOL:-}" = "Agent" ]; then
  SPAWN_STATE="$STATE_ROOT/${SESSION:-nosession}"
  mkdir -p "$SPAWN_STATE" 2>/dev/null
  EVENTS="$SPAWN_STATE/spawn_events.tsv"
  CALLS=0

  [ -n "$MJ" ] && [ -s "$MJ" ] || { row matrix_parse_failed "$MATRIX"; allow; }

  DESC=$(printf '%s' "$STDIN_JSON" | jq -r '.tool_input.description // ""' 2>/dev/null)
  PROMPT=$(printf '%s' "$STDIN_JSON" | jq -r '.tool_input.prompt // ""' 2>/dev/null)

  # --- 1. an explicit ROW: line wins ----------------------------------------
  WANT=$(printf '%s\n' "$PROMPT" \
         | grep -oE '^[[:space:]]*ROW:[[:space:]]*[A-Za-z0-9_-]+' \
         | head -1 | sed 's/.*ROW:[[:space:]]*//')
  PICK=""; HOW=""
  if [ -n "$WANT" ]; then
    if jq -e --arg n "$WANT" 'any(.rows[]; .name==$n)' "$MJ" >/dev/null 2>&1; then
      PICK="$WANT"; HOW="row_line"
    else
      row deny_unknown_row "$WANT"
      deny "MATRIX — unknown row '$WANT'. The valid rows are:
$(menu_text)
Fix the ROW: line in the spawn prompt, or delete it and let the description route."
    fi
  fi

  # --- 2. topics against the DESCRIPTION ONLY -------------------------------
  if [ -z "$PICK" ] && [ -n "$DESC" ]; then
    PICK=$(jq -r --arg d "$DESC" '
      def nrm: ascii_downcase | gsub("[^a-z0-9]+";" ") | gsub("^ +| +$";"");
      (" " + ($d|nrm) + " ") as $D |
      first(.rows[]
            | select(any((.topics//[])[];
                         . as $t | ($t|nrm) as $T
                         | ($T != "") and ($D | contains(" " + $T + " "))))
            | .name) // ""' "$MJ" 2>/dev/null)
    [ -n "$PICK" ] && HOW="topics_description"
  fi

  # --- 3. globs against paths found in the prompt ---------------------------
  if [ -z "$PICK" ]; then
    CANDS=$( { printf '%s\n' "$PROMPT" | grep -oE '/[A-Za-z0-9._*/-]+'
               printf '%s\n' "$PROMPT" | grep -oE '[A-Za-z0-9._*-]+(/[A-Za-z0-9._*-]+)+'
             } 2>/dev/null | sort -u | head -100)
    if [ -n "$CANDS" ]; then
      while IFS=$'\t' read -r rn rg; do
        [ -n "$rn" ] && [ -n "$rg" ] || continue
        pat=${rg//\*\*\//*}; pat=${pat//\*\*/*}
        while IFS= read -r p; do
          [ -n "$p" ] || continue
          rel=${p#"$DATA_ROOT"/}; rel=${rel#/}
          case "$rel" in $pat) PICK="$rn"; HOW="globs"; break ;; esac
          case "$p"   in $pat) PICK="$rn"; HOW="globs"; break ;; esac
        done <<< "$CANDS"
        [ -n "$PICK" ] && break
      done < <(jq -r '.rows[] | . as $r | (.globs//[])[] | [$r.name, .] | @tsv' "$MJ" 2>/dev/null)
    fi
  fi

  DEF_MODEL=$(jq -r '.defaults.model // "sonnet"' "$MJ")

  # --- 4. unrouted: one deny per session carrying the menu, then fail open --
  if [ -z "$PICK" ]; then
    row unrouted "${DESC:-(no description)}"
    if claim "$SPAWN_STATE/menu_denied"; then
      deny "MATRIX — this spawn matched no row, so it would run on [defaults] (model $DEF_MODEL, no load, no stop condition, no shape). This deny happens ONCE per session; retry the SAME spawn and it will go through unrouted. Before you retry, add a 'ROW: <name>' line as the FIRST line of the spawn prompt:
$(menu_text)
If none fits, retry unchanged and open a row for it."
    fi
    HDR="[MATRIX: UNROUTED — no row matched description \"${DESC}\"]
This spawn is running on [defaults] (model $DEF_MODEL). An unrouted spawn is the
signal that the table needs a row. Say so in one line of your report.
budget: warn $WARN / floor $FLOOR / hard $HARD calls."
    printf '%s' "$STDIN_JSON" | jq -c --arg h "$HDR" --arg m "$DEF_MODEL" \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"allow",
        permissionDecisionReason:"matrix: unrouted, defaults applied",
        updatedInput:(.tool_input + {model:$m,
          prompt:($h + "\n\n" + (.tool_input.prompt // ""))})}}'
    exit 0
  fi

  # --- the routed row -------------------------------------------------------
  eval "$(jq -r --arg n "$PICK" '.rows[] | select(.name==$n) |
    @sh "R_AGENT=\(.agent // "general-purpose")
         R_MODEL=\(.model // "")
         R_SHAPE=\(.shape // "claude-direct")
         R_ACTION=\(.action // "")
         R_ENGINE=\(.engine // "claude")
         R_LOAD=\(.load // "")
         R_STOP=\(.stop // "")
         R_REPORT=\(.max_report // 2000)
         R_HOME=\(.repo_home // "unset")
         R_EFFORT=\(.effort // "medium")
         R_REVIEWER=\(.reviewer // "")"' "$MJ" 2>/dev/null)"
  [ -n "${R_AGENT:-}" ] || { row row_unreadable "$PICK"; allow; }

  # refuse-and-substitute
  if [ "${R_ACTION:-}" = "refuse-and-substitute" ]; then
    SUB=$(jq -r --arg n "$PICK" '.rows[] | select(.name==$n) | .stop // ""' "$MJ")
    row refuse "$PICK"
    deny "MATRIX ROW $PICK — REFUSED. A model may not be the loop. Do this instead: write a shell loop that appends ONE line per sample to a status file under $DATA_ROOT/agrescap/evidence/watch/<name>.tsv, start it with nohup, and read the file ONCE when you next need it. A model may read the status file; a model may not be the waiting. Done when: ${SUB}. If you believe this spawn is not a watch/poll/wait, put an explicit 'ROW: <name>' line in the prompt."
  fi

  # codex availability. Operator ruling 2026-09-20: codex runs ON DEMAND on
  # this host. There is no endpoint file and no serving job; availability is
  # "is the binary here". The dispatcher's own refusal code is the other half,
  # and it can only be seen by the agent that runs it, which is why the header
  # names it.
  CODEX_BIN=$(jq -r '.codex.binary // "codex"' "$MJ")
  CODEX_AGENTS=$(jq -r '.codex.agents_dir // "/users/glvov/.claude/agents"' "$MJ")
  CODEX_DISPATCH=$(jq -r '.codex.dispatch // ""' "$MJ")
  CODEX_RC=$(jq -r '.codex.refusal_code // 2' "$MJ")
  codex_available() { command -v "$CODEX_BIN" >/dev/null 2>&1; }

  OUT_AGENT="$R_AGENT"
  OUT_MODEL="${R_MODEL:-$DEF_MODEL}"
  SHAPE_LINE=""

  if [ "$R_ENGINE" = "codex" ]; then
    if codex_available && [ -r "$CODEX_AGENTS/codex-$PICK.md" ]; then
      OUT_AGENT="codex-$PICK"
    else
      row codex_fallback "$PICK"
      SHAPE_LINE="codex is not available on this host (no '$CODEX_BIN' binary, or $CODEX_AGENTS/codex-$PICK.md is absent), so this row FELL BACK to its Claude model $OUT_MODEL. Do the work yourself."
    fi
  elif [ "$R_SHAPE" = "claude-plans-codex-executes" ]; then
    if codex_available; then
      SHAPE_LINE="delegation: you own the judgement. Do NOT do mechanistic reads yourself — hand every lookup, log read, grep and deterministic transform to codex luna high with:
  $CODEX_DISPATCH
You cannot spawn an agent; that dispatcher script IS the delegation path. If it exits $CODEX_RC (its refusal code), stop delegating and do the reads yourself for the rest of this lane."
    else
      row codex_fallback "$PICK"
    fi
  fi

  HDR="[MATRIX ROW: $PICK | shape=$R_SHAPE | routed by $HOW]
load: ${R_LOAD:-none — the row carries its own judgement}
stop: ${R_STOP:-(none named)}
max_report: $R_REPORT bytes — a longer report is truncated with a pointer
repo_home: $R_HOME (law 7)
effort: $R_EFFORT${R_REVIEWER:+
reviewer: $R_REVIEWER must run on the result before it is done}
budget: warn $WARN / floor $FLOOR / hard $HARD calls; past the floor only a
write of your own out.md and SubagentHandback are permitted.${SHAPE_LINE:+
$SHAPE_LINE}"

  row routed "$PICK/$HOW/$OUT_AGENT/$OUT_MODEL"
  printf '%s' "$STDIN_JSON" | jq -c --arg st "$OUT_AGENT" --arg m "$OUT_MODEL" \
      --arg h "$HDR" --arg r "matrix: row $PICK ($HOW) -> $OUT_AGENT/$OUT_MODEL" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"allow",
      permissionDecisionReason:$r,
      updatedInput:(.tool_input + {subagent_type:$st, model:$m,
        prompt:($h + "\n\n" + (.tool_input.prompt // ""))})}}'
  exit 0
fi

# ===========================================================================
# B) THE CALL CAP
# ===========================================================================
# --- the discriminator: no agent_id => main thread => never capped -----------
[ -n "${AGENT_ID:-}" ] || allow
[ -n "${SESSION:-}" ] && [ -n "${CWD:-}" ] || allow

SLUG=$(printf '%s' "$CWD" | tr -c '[:alnum:]' '-')
JSONL="$PROJECTS_ROOT/$SLUG/$SESSION/subagents/agent-$AGENT_ID.jsonl"

STATE="$STATE_ROOT/$SESSION/$AGENT_ID"
mkdir -p "$STATE" 2>/dev/null
EVENTS="$STATE/events.tsv"

if [ ! -r "$JSONL" ]; then
  row unresolved "$JSONL"
  allow
fi

CALLS=$(jq -r 'select(.type=="assistant") | .message.content[]?
               | select(.type=="tool_use") | 1' "$JSONL" 2>/dev/null | wc -l)
[ "$CALLS" -gt 0 ] 2>/dev/null || { row uncountable "$JSONL"; allow; }

# --- the lane, from the spawn prompt in the first record ---------------------
LANE_DIR=$(head -1 "$JSONL" | jq -r '
    .message.content | if type=="string" then . else ([.[]?.text?]|join("\n")) end' 2>/dev/null \
  | grep -oE 'LANE_DIR:[[:space:]]*/[^[:space:]]+' | head -1 | sed 's/^LANE_DIR:[[:space:]]*//')
if [ -z "$LANE_DIR" ]; then
  # Fallback: the brief path the spawn named, e.g. .../lanes/<lane>/<v>/in.md
  LANE_DIR=$(head -1 "$JSONL" | jq -r '
      .message.content | if type=="string" then . else ([.[]?.text?]|join("\n")) end' 2>/dev/null \
    | grep -oE '/[^[:space:]]+/in\.md' | head -1 | xargs -r dirname)
fi
OUT_MD=""
[ -n "$LANE_DIR" ] && OUT_MD="$LANE_DIR/out.md"

# --- is this call the state write itself? ------------------------------------
IS_STATE_WRITE=0
case "$TOOL" in
  Write|Edit|NotebookEdit)
    [ -n "$OUT_MD" ] && [ "$TARGET" = "$OUT_MD" ] && IS_STATE_WRITE=1 ;;
  SubagentHandback) IS_STATE_WRITE=1 ;;
  Read) [ -n "$OUT_MD" ] && { [ "$TARGET" = "$OUT_MD" ] || [ "$TARGET" = "$LANE_DIR/in.md" ]; } && IS_STATE_WRITE=1 ;;
esac

# --- below the warn: the hook has no opinion ---------------------------------
if [ "$CALLS" -lt "$WARN" ]; then allow; fi

# --- FLOOR: only the state write and the handback survive --------------------
if [ "$CALLS" -ge "$FLOOR" ]; then
  if [ "$IS_STATE_WRITE" = 1 ]; then
    row permit_state_write "$TOOL"
    allow
  fi
  if [ -n "$LANE_DIR" ] && claim "$STATE/respawn_requested"; then
    printf '%s\tlane=%s\tagent=%s\tcalls=%s\treason=floor\tresume=in.md+out.md\n' \
      "$(date -Is)" "$LANE_DIR" "$AGENT_ID" "$CALLS" >> "$LANE_DIR/RESPAWN_REQUEST.md"
  fi
  row deny_floor "$TOOL"
  if [ "$CALLS" -ge "$HARD" ]; then
    deny "LANE RECYCLER — PAST HARD CEILING. calls=$CALLS hard=$HARD. Stop now: call SubagentHandback with one line saying out.md is written (or not) and why. No other tool will be permitted."
  fi
  deny "LANE RECYCLER — FLOOR. calls=$CALLS floor=$FLOOR hard=$HARD. This lane is over budget and is being recycled. ONLY these are still permitted: the Write tool on ${OUT_MD:-your out.md} (not a bash heredoc — bash is denied here), and SubagentHandback. Write out.md now in the brief's format: line 1 RESULT, line 2 absolute paths, a RESUME block whose first line is the single next command, then '### SESSION n status=CONTINUE next=<one line>'. Then hand back: 'recycled at $CALLS calls; out.md written; respawn a fresh lane with the same in.md'. Do not explain, do not verify, do not read anything else."
fi

# --- WARN band: one deny, once, ever -----------------------------------------
if [ "$IS_STATE_WRITE" = 1 ]; then allow; fi
if claim "$STATE/warned"; then
  row deny_warn "$TOOL"
  deny "LANE RECYCLER — WARN (one time only; your next call goes through). calls=$CALLS warn=$WARN floor=$FLOOR. From the floor on, only a Write to ${OUT_MD:-your out.md} and SubagentHandback are permitted. Estimate the calls your remaining work needs. If it does not fit in $((FLOOR-CALLS)) calls, stop that work NOW, write out.md (RESULT line, absolute paths, RESUME block whose first line is the single next command, '### SESSION n status=CONTINUE next=<one line>'), and hand back asking for a fresh lane on the same in.md. If it does fit, continue and finish."
fi
row warn_passed "$TOOL"
allow
