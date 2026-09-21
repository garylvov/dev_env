#!/usr/bin/env bash
# guard_matrix.sh — the guard for agent_trigger_matrix.toml.
#
# It can fail. Break any of the five assertions below and it exits 1 naming the
# row. It is a LINE parser, not a TOML parser (no toml tool on this box, and
# Python is banned here) — it understands `[[row]]`, `[budget]`, `key = "value"`
# with an optional trailing `# comment`, and nothing else. A row written with a
# multi-line value or an inline table will be mis-read, and that is a known
# limit, not a pass.
#
# Assertions:
#   A1  exactly one [budget] block
#   A2  every `agent` value resolves: a built-in type, a file in the installed
#       agents dir, a plugin agent, or the sentinel NONE
#   A3  every `reviewer` value resolves the same way (empty is allowed)
#   A4  no row names haiku, and no persona body this matrix points at names
#       haiku in its frontmatter either (operator ruling R1)
#   A5  every engine = "codex" row names BOTH codex_model and codex_effort
#
# Usage: guard_matrix.sh [matrix.toml] [installed_agents_dir]
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MATRIX="${1:-$HERE/../../agent_trigger_matrix.toml}"
AGENTS_DIR="${2:-/users/glvov/.claude/agents}"
PERSONA_DIR="$HERE/agents"

# Built-in selectable agent types the harness ships. NOT ours to deprecate.
BUILTINS="general-purpose Explore Plan statusline-setup claude-code-guide fork claude NONE"

fail=0
note() { printf '%s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; fail=1; }

[ -r "$MATRIX" ] || { bad "A0 matrix unreadable: $MATRIX"; exit 1; }

# ---- installed + plugin agent inventory -----------------------------------
installed=""
for f in "$AGENTS_DIR"/*.md; do
  [ -e "$f" ] || continue
  b="$(basename "$f" .md)"
  installed="$installed $b"
done
for f in /users/glvov/.claude/plugins/synced/*/agents/*.md \
         /users/glvov/.claude/plugins/marketplaces/*/*/agents/*.md; do
  [ -e "$f" ] || continue
  installed="$installed $(basename "$f" .md)"
done

resolves() {
  local want="$1" t
  for t in $BUILTINS $installed; do [ "$t" = "$want" ] && return 0; done
  # a plugin agent is written plugin:name; accept if the plugin dir exists
  case "$want" in
    *:*) [ -d "/users/glvov/.claude/plugins/synced/${want%%:*}" ] && return 0 ;;
  esac
  return 1
}

# ---- walk the file ---------------------------------------------------------
# emits one TSV line per row: name<TAB>agent<TAB>reviewer<TAB>engine<TAB>cm<TAB>ce<TAB>models
rows_tsv="$(awk '
function val(line,   s) {
  # first double-quoted string on the line; ignores any trailing # comment
  if (match(line, /"[^"]*"/)) { s = substr(line, RSTART+1, RLENGTH-2); return s }
  return ""
}
/^\[\[row\]\]/ {
  if (inrow) print name "|" agent "|" reviewer "|" engine "|" cm "|" ce "|" models
  inrow=1; name="(unnamed)"; agent=""; reviewer=""; engine=""; cm=""; ce=""; models=""
  next
}
/^\[[a-z_]+\]/ {
  if (inrow) print name "|" agent "|" reviewer "|" engine "|" cm "|" ce "|" models
  inrow=0
  next
}
!inrow { next }
/^name *=/          { name=val($0); next }
/^agent *=/         { agent=val($0); next }
/^reviewer *=/      { reviewer=val($0); next }
/^engine *=/        { engine=val($0); next }
/^codex_model *=/   { cm=val($0); next }
/^codex_effort *=/  { ce=val($0); next }
/^model *=/         { models=models " " val($0); next }
END { if (inrow) print name "|" agent "|" reviewer "|" engine "|" cm "|" ce "|" models }
' "$MATRIX")"

nrows=$(printf '%s\n' "$rows_tsv" | grep -c .)

# ---- A1: exactly one [budget] ---------------------------------------------
nbudget=$(grep -c '^\[budget\]' "$MATRIX")
[ "$nbudget" -eq 1 ] || bad "A1 expected exactly one [budget] block, found $nbudget"

# ---- A2/A3/A4/A5 per row ---------------------------------------------------
while IFS="|" read -r name agent reviewer engine cm ce models; do
  [ -n "${name:-}" ] || continue
  if [ -n "$agent" ]; then
    resolves "$agent" || bad "A2 row '$name': agent '$agent' resolves to nothing (not a built-in, not in $AGENTS_DIR, not a plugin agent)"
  else
    bad "A2 row '$name': no agent named"
  fi
  if [ -n "$reviewer" ]; then
    resolves "$reviewer" || bad "A3 row '$name': reviewer '$reviewer' resolves to nothing"
  fi
  case " $models " in *" haiku "*) bad "A4 row '$name': names haiku (operator ruling R1)";; esac
  if [ "$engine" = "codex" ]; then
    [ -n "$cm" ] || bad "A5 row '$name': engine=codex but no codex_model"
    [ -n "$ce" ] || bad "A5 row '$name': engine=codex but no codex_effort"
  fi
done <<< "$rows_tsv"

# ---- A4 continued: persona bodies this matrix points at --------------------
for b in $(awk -F'"' '/^bodies *=/ {for(i=2;i<=NF;i+=2) print $i}' "$MATRIX"); do
  src="$PERSONA_DIR/$b.md"
  [ -r "$src" ] || { bad "A4 [personas].bodies names '$b' but $src is absent"; continue; }
  if grep -q '^model: *haiku' "$src"; then bad "A4 persona body $src declares model: haiku (R1)"; fi
done
# the INSTALLED bodies are checked too, since those are what actually spawn today
for f in "$AGENTS_DIR"/*.md; do
  [ -e "$f" ] || continue
  grep -q '^model: *haiku' "$f" && note "WARN installed persona $(basename "$f") declares model: haiku — not a matrix row, but R1-adjacent"
done

# ===========================================================================
# A6..A12 — added 2026-09-20 by the matrix_live lane. Each one boards a fact
# that was wrong in this file and must not regress. They use the ROUTER's own
# TOML->JSON compiler (lane_recycler.sh --compile) rather than the line parser
# above, because that compiler is what the live hook reads; if it ever stops
# agreeing with this file, the guard fails here rather than in production.
# ===========================================================================
ROUTER="$HERE/../lane_recycler/lane_recycler.sh"
if [ -x "$ROUTER" ] || [ -r "$ROUTER" ]; then
  MJSON="$(LANE_RECYCLER_MATRIX="$MATRIX" \
           LANE_RECYCLER_STATE="${TMPDIR:-/tmp}/guard_matrix.$$" \
           bash "$ROUTER" --compile 2>/dev/null)"
  if [ -z "$MJSON" ]; then
    bad "A6 the router cannot compile this matrix (lane_recycler.sh --compile produced nothing)"
  else
    # A6 every row carries a shape from the closed set
    n=$(printf '%s' "$MJSON" | jq -r '[.rows[] | select((.shape // "") |
          IN("codex-direct","claude-direct","claude-plans-codex-executes","refuse") | not)
          | .name] | join(", ")')
    [ -z "$n" ] || bad "A6 rows with a missing or unknown shape: $n"
    # A7 every row carries a use_when (this is what the menu shows)
    n=$(printf '%s' "$MJSON" | jq -r '[.rows[] | select((.use_when // "") == "") | .name] | join(", ")')
    [ -z "$n" ] || bad "A7 rows with no use_when (the menu would show nothing): $n"
    # A8 every row names a repo_home (law 7)
    n=$(printf '%s' "$MJSON" | jq -r '[.rows[] | select((.repo_home // "") == "") | .name] | join(", ")')
    [ -z "$n" ] || bad "A8 rows with no repo_home (law 7): $n"
    # A9 max_calls is gone for good (ruling R3: ONE budget, all kinds)
    n=$(printf '%s' "$MJSON" | jq -r '[.rows[] | select(has("max_calls")) | .name] | join(", ")')
    [ -z "$n" ] || bad "A9 rows still carrying max_calls, which R3 removed: $n"
    # A10 the codex block is on-demand; the withdrawn endpoint design stays out
    printf '%s' "$MJSON" | jq -e '.codex.run == "on-demand"' >/dev/null 2>&1 \
      || bad "A10 [codex] must say run = \"on-demand\" (operator ruling 2026-09-20)"
    printf '%s' "$MJSON" | jq -e 'has("codex") and (.codex|has("endpoint_file"))' >/dev/null 2>&1 \
      && bad "A10 [codex].endpoint_file is the WITHDRAWN design; codex runs on demand on this host"
    # A11 the budget block is readable as numbers by the router
    printf '%s' "$MJSON" | jq -e '(.budget.warn|type=="number") and (.budget.floor|type=="number")
                                   and (.budget.hard|type=="number")
                                   and (.budget.warn < .budget.floor)
                                   and (.budget.floor < .budget.hard)' >/dev/null 2>&1 \
      || bad "A11 [budget] must give the router three numbers with warn < floor < hard"
  fi
else
  bad "A6 the router is missing: $ROUTER"
fi

# ---- A12: the reader/writer law. This file HAS a reader; prove it. ---------
if grep -q 'agent_trigger_matrix' "$HERE/../lane_recycler/lane_recycler.sh" 2>/dev/null \
   || grep -q 'MATRIX' "$HERE/../lane_recycler/lane_recycler.sh" 2>/dev/null; then
  grep -q 'tool_name.*Agent\|TOOL:-}" = "Agent"' "$HERE/../lane_recycler/lane_recycler.sh" \
    || bad "A12 the reader has no branch on a spawn (tool_name == Agent)"
else
  bad "A12 no reader names this matrix — the reader/writer-law defect is back"
fi
grep -q 'NOTHING READS THIS FILE' "$MATRIX" \
  && bad "A12 the matrix still says NOTHING READS THIS FILE, which is now false"
# the retracted measurement must not come back
for s in '381 of 3,562' 'median 65' '59.6% of ALL subagent spend'; do
  grep -qF "$s" "$MATRIX" && bad "A12 the retracted T2 number is back in the matrix: '$s'"
done
# the conf must not re-duplicate the budget (that duplication WAS the defect)
if grep -qE '^[[:space:]]*LANE_RECYCLER_(WARN|FLOOR|HARD)=' \
     "$HERE/../lane_recycler/lane_recycler.conf" 2>/dev/null; then
  bad "A12 lane_recycler.conf re-declares a budget number; [budget] must be a READ of the matrix"
fi

# ---- report ----------------------------------------------------------------
note "rows=$nrows budget_blocks=$nbudget matrix=$MATRIX"
if [ "$fail" -eq 0 ]; then note "GUARD PASS"; else note "GUARD FAIL"; fi
exit "$fail"
