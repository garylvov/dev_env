#!/usr/bin/env bash
# codex_lane_dispatch.sh — ONE Bash call that reaches a codex turn on a COMPUTE
# node from a Claude session that lives on login009, and refuses fast when it
# cannot.
#
# THE ONE LINE A CLAUDE SUBAGENT NEEDS (no Agent tool required, no plugin):
#   /oscar/data/stellex/glvov/agrescap/canonical/tools/codex_native/codex_lane_dispatch.sh \
#     --model gpt-5.6-luna --effort high --cwd <abs dir> \
#     --task-file <abs in.md> --out <abs out.md>
#   stdout  = ONLY the final codex answer, truncated to --max-bytes (default 4000)
#   --out   = the same answer, untruncated
#   log     = <out>.log  (full jsonl transcript; the caller must NEVER print it)
#   rc 0    = answered            rc 3  = codex ran and failed
#   rc 2    = bad usage           rc 42 = NO ENDPOINT (absent | stale | dead)
#   rc 42 is the FALLBACK SIGNAL: no compute endpoint is serving, so the calling
#   agent must do the step itself with a Claude model. It is reached in ~5 s and
#   never hangs.
#
# WHY NOT THE PLUGIN ANY MORE
#   The previous version of this file drove codex through
#   plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs. That whole
#   runtime is `spawn("codex", ["app-server"])`, it deliberately leaves model and
#   effort unset, and it only worked here because of a hand patch inside the
#   plugin CACHE that any plugin update silently reverts. The engine is now
#   tools/codex_surface/codex_fleet.sh, which speaks to the codex binary
#   directly. No node wrapper, no cache patch, no per-dispatch `srun`.
#
# WHERE THE COMPUTE SIDE COMES FROM
#   tools/codex_surface/codex_fleet_serve.sh runs inside a CPU-only Slurm job and
#   publishes the ENDPOINT CONTRACT below. Liveness is that file's MTIME.
set -uo pipefail

ENDPOINT="${CODEX_FLEET_ENDPOINT:-/oscar/data/stellex/glvov/agrescap/evidence/codex_fleet/endpoint.json}"
FLEET="${CODEX_FLEET_SH:-/oscar/data/stellex/glvov/agrescap/canonical/tools/codex_surface/codex_fleet.sh}"
MAX_AGE="${CODEX_FLEET_MAX_AGE:-120}"     # seconds; server refreshes every 30
PROBE_TIMEOUT="${CODEX_FLEET_PROBE_TIMEOUT:-5}"

RC_USAGE=2
RC_TURN_FAILED=3
RC_NO_ENDPOINT=42

ROW=""; LANE_DIR=""; MODEL=""; EFFORT=""; PROMPT_FILE=""; TASK_FILE=""
OUT=""; CWD=""; THREAD=""; MAX_BYTES="${CODEX_MAX_BYTES:-4000}"
RESUME=0; DRY=0; CHECK=0

die()   { printf 'DISPATCH_REFUSE: %s\n' "$*" >&2; exit $RC_USAGE; }
norun() { printf 'CODEX_NO_ENDPOINT reason=%s endpoint=%s -- fall back to a Claude model\n' "$1" "$ENDPOINT" >&2; exit $RC_NO_ENDPOINT; }

while [ $# -gt 0 ]; do
  case "$1" in
    --row)         ROW="${2:-}"; shift 2 ;;
    --lane-dir)    LANE_DIR="${2:-}"; shift 2 ;;
    --model)       MODEL="${2:-}"; shift 2 ;;
    --effort)      EFFORT="${2:-}"; shift 2 ;;
    --prompt-file) PROMPT_FILE="${2:-}"; shift 2 ;;
    --task-file)   TASK_FILE="${2:-}"; shift 2 ;;
    --out)         OUT="${2:-}"; shift 2 ;;
    --cwd)         CWD="${2:-}"; shift 2 ;;
    --thread)      THREAD="${2:-}"; shift 2 ;;
    --max-bytes)   MAX_BYTES="${2:-}"; shift 2 ;;
    --resume)      RESUME=1; shift ;;
    --dry-run)     DRY=1; shift ;;
    --check)       CHECK=1; shift ;;
    --jobid)       shift 2 ;;   # accepted and IGNORED: placement comes from the endpoint now
    *) die "unknown flag $1" ;;
  esac
done

# ---------------------------------------------------------------- endpoint ---
# Three refusal cases, ONE exit code, each naming its reason. Bounded by
# PROBE_TIMEOUT so a dead node cannot turn into a hang.
read_endpoint() {
  [ -f "$ENDPOINT" ] || norun absent
  local now mt age
  now="$(date +%s)"
  mt="$(stat -c %Y "$ENDPOINT" 2>/dev/null)" || norun absent
  age=$(( now - mt ))
  [ "$age" -le "$MAX_AGE" ] || norun "stale age=${age}s max=${MAX_AGE}s"
  EP_HOST="$(sed -n 's/.*"host":"\([^"]*\)".*/\1/p' "$ENDPOINT")"
  EP_PORT="$(sed -n 's/.*"port":\([0-9]*\).*/\1/p' "$ENDPOINT")"
  EP_HOME="$(sed -n 's/.*"codex_home":"\([^"]*\)".*/\1/p' "$ENDPOINT")"
  EP_JOB="$(sed -n 's/.*"slurm_jobid":"\([^"]*\)".*/\1/p' "$ENDPOINT")"
  [ -n "$EP_HOST" ] && [ -n "$EP_PORT" ] && [ -n "$EP_HOME" ] \
    || norun "malformed endpoint json"
  [ "${CODEX_FLEET_SKIP_PROBE:-0}" = "1" ] && return 0
  local code
  code="$(timeout "$PROBE_TIMEOUT" ssh -o BatchMode=yes -o ConnectTimeout=3 "$EP_HOST" \
            "curl -sS -m3 -o /dev/null -w '%{http_code}' http://127.0.0.1:$EP_PORT/readyz" 2>/dev/null)"
  [ "$code" = "200" ] || norun "dead host=$EP_HOST port=$EP_PORT readyz=${code:-no-answer}"
  return 0
}

# ---------------------------------------------------------------- validate ---
[ -n "$MODEL" ]  || die "--model is required; never inherit the codex config default"
[ -n "$EFFORT" ] || die "--effort is required; never inherit the codex config default"
case "$EFFORT" in none|minimal|low|medium|high|xhigh) ;; *) die "bad --effort $EFFORT" ;; esac
case "$MAX_BYTES" in ''|*[!0-9]*) die "--max-bytes must be an integer" ;; esac
[ "$MAX_BYTES" -ge 200 ] || die "--max-bytes below 200 would truncate every real answer"

if [ "$CHECK" -eq 1 ]; then
  read_endpoint
  printf 'CODEX_ENDPOINT ok host=%s port=%s jobid=%s home=%s\n' "$EP_HOST" "$EP_PORT" "$EP_JOB" "$EP_HOME"
  exit 0
fi

# Two shapes, one engine.
#   DIRECT: --cwd + --task-file + --out      (a Claude subagent mid-task)
#   ROW:    --row + --lane-dir               (the six wrapper agents)
if [ -n "$TASK_FILE" ] || [ -n "$OUT" ]; then
  [ -n "$TASK_FILE" ] || die "direct mode needs --task-file"
  [ -n "$OUT" ]       || die "direct mode needs --out"
  [ -n "$CWD" ]       || die "direct mode needs --cwd (absolute workspace root)"
  case "$CWD" in /*) ;; *) die "--cwd must be absolute" ;; esac
  case "$OUT" in /*) ;; *) die "--out must be absolute" ;; esac
  [ -f "$TASK_FILE" ] || die "task file does not exist: $TASK_FILE"
  PROMPT_FILE="$TASK_FILE"
  LOG="$OUT.log"
  LABEL="direct"
else
  [ -n "$ROW" ]      || die "--row is required (or use direct mode: --cwd/--task-file/--out)"
  [ -n "$LANE_DIR" ] || die "--lane-dir is required (absolute)"
  case "$LANE_DIR" in /*) ;; *) die "--lane-dir must be absolute" ;; esac
  if [ "$RESUME" -eq 0 ] && [ -z "$PROMPT_FILE" ]; then die "give --prompt-file or --resume"; fi
  [ -n "$PROMPT_FILE" ] && [ ! -f "$PROMPT_FILE" ] && die "prompt file does not exist: $PROMPT_FILE"
  CWD="${CWD:-$LANE_DIR}"
  WORKDIR="$LANE_DIR/codex/$ROW"
  LOG="$WORKDIR/$(date +%Y%m%dT%H%M%S).log"
  OUT="$LOG.answer"
  LABEL="$ROW"
fi

VERB=turn; [ "$RESUME" -eq 1 ] && VERB=resume
if [ "$RESUME" -eq 1 ] && [ -z "$PROMPT_FILE" ]; then
  PROMPT_FILE="$(dirname "$OUT")/.resume-nudge.txt"
fi

if [ "$DRY" -eq 1 ]; then
  printf 'DRYRUN label=%s verb=%s model=%s effort=%s max_bytes=%s\n' "$LABEL" "$VERB" "$MODEL" "$EFFORT" "$MAX_BYTES"
  printf '%s %s --node <endpoint.host> --home <endpoint.codex_home> --model %s --effort %s --cwd %s --prompt-file %s --log %s\n' \
    "$FLEET" "$VERB" "$MODEL" "$EFFORT" "$CWD" "$PROMPT_FILE" "$LOG"
  printf '%s\n' "$LOG"
  exit 0
fi

read_endpoint

mkdir -p "$(dirname "$LOG")" "$(dirname "$OUT")" || die "cannot create output dirs"
if [ "$RESUME" -eq 1 ] && [ ! -f "$PROMPT_FILE" ]; then
  printf 'Continue from where you stopped and give the final answer only.\n' > "$PROMPT_FILE"
fi

RAW="$("$FLEET" "$VERB" \
        --node "$EP_HOST" --home "$EP_HOME" \
        --model "$MODEL" --effort "$EFFORT" --cwd "$CWD" \
        ${THREAD:+--thread "$THREAD"} \
        --prompt-file "$PROMPT_FILE" --log "$LOG" 2>&1)"

# The NODE prints the final agent message between these markers, so the answer is
# never read back across NFS right after another client wrote it.
printf '%s\n' "$RAW" > "$LOG.transport"
TURN_RC="$(printf '%s\n' "$RAW" | sed -n 's/.*---FLEET-FINAL-END rc=\([0-9]*\)---.*/\1/p' | tail -1)"
printf '%s\n' "$RAW" \
  | awk '/---FLEET-FINAL-BEGIN---/{f=1;next} /---FLEET-FINAL-END/{f=0} f' > "$OUT"

if [ -z "$TURN_RC" ]; then
  printf 'CODEX_TURN label=%s status=FAIL reason=no-final-marker log=%s\n' "$LABEL" "$LOG.transport" >&2
  exit $RC_TURN_FAILED
fi
if [ "$TURN_RC" != "0" ] || [ ! -s "$OUT" ]; then
  printf 'CODEX_TURN label=%s status=FAIL rc=%s log=%s\n' "$LABEL" "$TURN_RC" "$LOG" >&2
  exit $RC_TURN_FAILED
fi

# stdout is ONLY the answer, byte-capped. The full transcript stays in $LOG and
# is never printed: that cap is the whole reason this lane exists.
head -c "$MAX_BYTES" "$OUT"
BYTES="$(wc -c < "$OUT")"
if [ "$BYTES" -gt "$MAX_BYTES" ]; then
  printf '\n[truncated at %s of %s bytes; full answer: %s]\n' "$MAX_BYTES" "$BYTES" "$OUT"
fi
exit 0
