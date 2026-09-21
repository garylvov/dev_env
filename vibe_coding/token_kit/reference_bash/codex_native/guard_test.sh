#!/usr/bin/env bash
# guard_test.sh — runnable on the LOGIN node. Proves the agent files parse, the
# wrappers are free of the plugin path, the dispatcher refuses FAST on all three
# endpoint failures with the one fallback exit code, and that model, effort and
# the byte cap all survive to the engine call.
#
# It deliberately proves NOTHING about a real codex turn: codex may not run on a
# login node, so end-to-end is out of scope HERE by design, not by omission.
# The end-to-end proof is the CPU-only Slurm job in the lane's proof/ directory.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
A="$D/agents"
S="$D/codex_lane_dispatch.sh"
FAIL=0
ok()   { printf 'ok   %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; FAIL=1; }

# Keys the harness actually reads, taken from files it is already running:
#   /users/glvov/.claude/agents/architect.md            -> name description model color
#   .../plugins/cache/openai-codex/codex/*/agents/codex-rescue.md -> name description model tools skills
ALLOWED='name|description|model|color|tools|skills'

shopt -s nullglob
FILES=("$A"/*.md)
[ "${#FILES[@]}" -gt 0 ] || bad "no agent files under $A"

for f in "${FILES[@]}"; do
  b="$(basename "$f" .md)"
  [ "$(head -n1 "$f")" = "---" ] || { bad "$b: first line is not ---"; continue; }
  close=$(awk 'NR>1 && $0=="---"{print NR; exit}' "$f")
  [ -n "$close" ] || { bad "$b: frontmatter never closes"; continue; }
  fm=$(sed -n "2,$((close-1))p" "$f")

  keys=$(printf '%s\n' "$fm" | grep -E '^[a-zA-Z_-]+:' | cut -d: -f1)
  for k in $keys; do
    printf '%s\n' "$k" | grep -qE "^($ALLOWED)$" || bad "$b: frontmatter key '$k' is not one the harness reads"
  done
  for req in name description model tools; do
    printf '%s\n' "$keys" | grep -qx "$req" || bad "$b: missing required key '$req'"
  done

  n=$(printf '%s\n' "$fm" | sed -n 's/^name: *//p')
  [ "$n" = "$b" ] || bad "$b: name '$n' does not match filename"

  m=$(printf '%s\n' "$fm" | sed -n 's/^model: *//p')
  case "$m" in
    haiku) bad "$b: model haiku is banned here" ;;
    sonnet|opus|fable|inherit) : ;;
    *) bad "$b: unknown model '$m'" ;;
  esac

  t=$(printf '%s\n' "$fm" | sed -n 's/^tools: *//p')
  [ "$t" = "Bash" ] || bad "$b: tools must be exactly Bash (a relay reads nothing), got '$t'"

  # description must be ONE physical line with literal \n separators, which is
  # the shape the live agent files use.
  dcount=$(printf '%s\n' "$fm" | grep -c '^description:')
  [ "$dcount" = "1" ] || bad "$b: description is not a single line"
  printf '%s\n' "$fm" | grep -q '^description:.*<example>' || bad "$b: description carries no <example> block, so auto-selection has nothing to match"

  # every lane must name model AND effort in its dispatch instructions
  grep -q -- '--model gpt-5.6-luna' "$f" || bad "$b: body does not pin --model"
  grep -q -- '--effort high'        "$f" || bad "$b: body does not pin --effort"
  grep -q 'NEVER `cat` the codex log' "$f" || bad "$b: body does not forbid reading the log wholesale"

  # THE PLUGIN IS OUT OF THE PATH. A wrapper that still mentions it is a wrapper
  # that will one day be repaired by re-patching a plugin cache.
  for needle in 'resume-last' 'codex-companion' 'plugins/cache' '--jobid' 'srun --overlap'; do
    grep -qF -- "$needle" "$f" && bad "$b: body still references the plugin path: $needle"
  done
  # and it must teach the fallback contract, or rc 42 becomes a mystery
  grep -q 'rc 42' "$f" || bad "$b: body does not tell the relay what rc 42 means"
  ok "$b: frontmatter + body contract, plugin-free"
done

bash -n "$S" && ok "dispatch script parses" || bad "dispatch script does not parse"

# the ENGINE the dispatch calls must exist and carry the verbs it uses
FLEET=/oscar/data/stellex/glvov/agrescap/canonical/tools/codex_surface/codex_fleet.sh
if [ -x "$FLEET" ]; then ok "engine present: $FLEET"; else bad "engine missing: $FLEET"; fi
for v in 'turn)' 'resume)'; do
  grep -qF -- "$v" "$FLEET" || bad "engine has no '$v' verb"
done
grep -qF -- '--dangerously-bypass-approvals-and-sandbox' "$FLEET" || bad "engine turn is not non-interactive"
grep -qF -- 'model_reasoning_effort' "$FLEET" || bad "engine does not pass effort to codex"
ok "engine verbs turn/resume present with model+effort"

# The dispatcher must never REACH for the plugin again. Comments are exempt on
# purpose: the header explains why the plugin was dropped, and deleting that
# explanation is how a removed dependency quietly comes back.
CODE="$(grep -v '^[[:space:]]*#' "$S")"
for needle in 'codex-companion' 'plugins/cache' 'resume-last' 'CODEX_PLUGIN_ROOT'; do
  printf '%s\n' "$CODE" | grep -qF -- "$needle" && bad "dispatcher still EXECUTES the plugin path: $needle"
done
ok "dispatcher is plugin-free outside its comments"

# --- dry-run must emit a well-formed engine call ------------------------------
DRY=$("$S" --row guard --lane-dir /tmp/guard-lane \
        --model gpt-5.6-luna --effort high --resume --dry-run 2>&1)
for needle in 'codex_fleet.sh resume' '--model gpt-5.6-luna' '--effort high' \
              '--cwd /tmp/guard-lane' '--node <endpoint.host>'; do
  printf '%s\n' "$DRY" | grep -qF -- "$needle" || bad "dry-run command is missing: $needle"
done
ok "dry-run carries model, effort, cwd and the endpoint host"

# direct mode (the one-line shape a Claude subagent uses mid-task)
DRY2=$("$S" --model gpt-5.6-luna --effort high --cwd /tmp/guard-lane \
        --task-file "$S" --out /tmp/guard-lane/out.md --dry-run 2>&1)
printf '%s\n' "$DRY2" | grep -qF 'label=direct' || bad "direct mode not recognised from --cwd/--task-file/--out"
printf '%s\n' "$DRY2" | grep -qF 'max_bytes=4000' || bad "direct mode lost the default byte cap"
ok "direct mode dry-run: label=direct, default cap 4000"

# --- refusal guards -----------------------------------------------------------
# a missing --effort must be refused, not defaulted
"$S" --row guard --lane-dir /tmp/guard-lane --model gpt-5.6-luna --resume --dry-run >/dev/null 2>&1 \
  && bad "dispatch accepted a run with no --effort" \
  || ok "dispatch refuses a run with no --effort"

# an absurd byte cap must be refused, not silently applied
"$S" --model gpt-5.6-luna --effort high --cwd /tmp --task-file "$S" --out /tmp/o.md \
     --max-bytes 5 --dry-run >/dev/null 2>&1 \
  && bad "dispatch accepted --max-bytes 5, which truncates every real answer" \
  || ok "dispatch refuses a byte cap below 200"

# THE THREE ENDPOINT CASES. One exit code (42) so a router needs one rule; a
# distinct reason word so a human knows which one bit. Bounded, never a hang.
G=$(mktemp -d /tmp/guard-endpoint.XXXXXX)
expect42() { # expect42 <label> <reason-substring> ; endpoint via $EP
  local label="$1" reason="$2" out rc t0 t1
  t0=$(date +%s)
  out=$(CODEX_FLEET_ENDPOINT="$EP" "$S" --model gpt-5.6-luna --effort high \
          --cwd /tmp --task-file "$S" --out "$G/out.md" 2>&1)
  rc=$?
  t1=$(date +%s)
  [ "$rc" = "42" ] || { bad "$label: exit code $rc, expected 42"; return; }
  printf '%s\n' "$out" | grep -qF "$reason" || { bad "$label: reason line does not say '$reason': $out"; return; }
  [ $((t1-t0)) -le 10 ] || { bad "$label: took $((t1-t0))s, a fallback signal must be fast"; return; }
  ok "$label: rc=42 in $((t1-t0))s, reason names '$reason'"
}
EP="$G/absent.json"; expect42 "endpoint absent" "reason=absent"
EP="$G/stale.json"
printf '{"host":"127.0.0.1","port":8401,"codex_home":"/tmp/h","slurm_jobid":"1"}\n' > "$EP"
touch -d '2001-01-01' "$EP"; expect42 "endpoint stale" "reason=stale"
EP="$G/dead.json"
# A host that cannot resolve stands in for a node whose server is gone: the ssh
# probe fails the same way, which is the point -- no socket answers.
printf '{"host":"guard-no-such-host","port":8401,"codex_home":"/tmp/h","slurm_jobid":"1"}\n' > "$EP"
expect42 "endpoint dead" "reason=dead"
rm -rf "$G"

[ "$FAIL" = "0" ] && { echo "GUARD: PASS"; exit 0; } || { echo "GUARD: FAIL"; exit 1; }
