#!/usr/bin/env bash
# measure_spend.sh — regenerates every number in the TOKENNOMICS header of
# agent_trigger_matrix.toml. Measure, don't quote: run this before citing any
# of them. Read-only; touches nothing but its own temp file.
#
#   ./measure_spend.sh            # the whole corpus + the persona table
#   ./measure_spend.sh job-runner # one agent type
#
# "billable context" = input_tokens + cache_read_input_tokens +
# cache_creation_input_tokens, i.e. what every call re-pays. Output tokens are
# reported separately and are ~0.1% of the total here.
set -uo pipefail
P="${LANE_PROJECTS_ROOT:-$HOME/.claude/projects}"
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT

sum_of() {  # $1 = newline-separated jsonl paths
  xargs cat 2>/dev/null <<< "$1" | awk '
  { t=0
    if (match($0,/"input_tokens":[0-9]+/))              t+=substr($0,RSTART+15,RLENGTH-15)+0
    if (match($0,/"cache_read_input_tokens":[0-9]+/))   t+=substr($0,RSTART+26,RLENGTH-26)+0
    if (match($0,/"cache_creation_input_tokens":[0-9]+/)) t+=substr($0,RSTART+30,RLENGTH-30)+0
    if (match($0,/"output_tokens":[0-9]+/))             o =substr($0,RSTART+16,RLENGTH-16)+0; else o=0
    if (t>0) { c++; tot+=t; out+=o } }
  END { printf "calls=%d billable_ctx=%d out=%d\n", c, tot, out }'
}

if [ $# -ge 1 ]; then
  t="$1"
  metas=$(ls -1 "$P"/*/*/subagents/*.meta.json 2>/dev/null | xargs grep -l "\"agentType\":\"$t\"" 2>/dev/null)
  n=$(grep -c . <<< "$metas")
  printf '%-18s spawns=%-6s %s\n' "$t" "$n" "$(sum_of "$(sed 's/\.meta\.json$/.jsonl/' <<< "$metas")")"
  exit 0
fi

echo "== spawn census by agentType"
cat "$P"/*/*/subagents/*.meta.json 2>/dev/null | jq -r '.agentType // "UNSET"' | sort | uniq -c | sort -rn

echo "== call distribution (one row per agent)"
ls -1 "$P"/*/*/subagents/*.jsonl 2>/dev/null | xargs grep -c '"usage"' 2>/dev/null \
  | awk -F: '{print $NF}' | sort -n > "$TMP"
awk '{a[NR]=$1; s+=$1} END {printf "agents=%d calls=%d mean=%.1f median=%d p90=%d p99=%d max=%d\n",
      NR,s,s/NR,a[int(NR*0.5)],a[int(NR*0.9)],a[int(NR*0.99)],a[NR]}' "$TMP"
awk '$1>200 {n++} END {printf "agents_over_200_calls=%d\n", n}' "$TMP"

echo "== per-persona spend (the T3 table)"
for t in grizzly-veteran job-runner architect bloat-killer; do "$0" "$t"; done

echo "== share of spend held by agents over 200 calls (the T2 number)"
ls -1 "$P"/*/*/subagents/*.jsonl 2>/dev/null | xargs awk '
FILENAME!=prev { if(prev!="") print tot"\t"cnt; prev=FILENAME; tot=0; cnt=0 }
{ t=0
  if (match($0,/"input_tokens":[0-9]+/))                t+=substr($0,RSTART+15,RLENGTH-15)+0
  if (match($0,/"cache_read_input_tokens":[0-9]+/))     t+=substr($0,RSTART+26,RLENGTH-26)+0
  if (match($0,/"cache_creation_input_tokens":[0-9]+/)) t+=substr($0,RSTART+30,RLENGTH-30)+0
  if (t>0) {tot+=t; cnt++} }
END { if(prev!="") print tot"\t"cnt }' 2>/dev/null \
| awk -F'\t' '{T+=$1; N++; if($2>200){t2+=$1; n2++}}
   END {printf "agents=%d total_billable_ctx=%d  over200: n=%d tokens=%d share=%.1f%%\n", N,T,n2,t2,100*t2/T}'

echo "== persona body bytes (installed vs compressed)"
for b in architect grizzly-veteran job-runner bloat-killer; do
  o=$(stat -c%s "$HOME/.claude/agents/$b.md" 2>/dev/null || echo 0)
  n=$(stat -c%s "$(dirname "$0")/agents/$b.md" 2>/dev/null || echo 0)
  d=$(grep -m1 '^description:' "$HOME/.claude/agents/$b.md" 2>/dev/null | wc -c)
  printf '%-16s installed=%6d compressed=%5d description_field=%5d\n' "$b" "$o" "$n" "$d"
done
