#!/usr/bin/env bash
# selftest.sh -- the guard test for `canary`. Builds four fixtures in a scratch
# dir and asserts the three outcomes are distinguished. Costs 4 model calls
# (~$0.18 measured at sonnet); the PROBE_BROKEN case costs nothing.
#
# The case it proves hardest: a probe that FAILED must come back PROBE_BROKEN,
# never NOT_LOADED -- fixture D reuses the exact manifest that fixture A just
# graded LOADED, so the only difference is the health of the probe itself.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
C="$HERE/canary"
S=${1:-}
case "${1:-}" in --scratch) S=${2:-};; esac
[ -n "$S" ] || { echo "usage: $0 --scratch DIR" >&2; exit 2; }
mkdir -p "$S/fix/loaded" "$S/fix/shadow" "$S/fix/absent" "$S/ev" || exit 2
echo '{ "hooks": {}, "enabledPlugins": {}, "includeCoAuthoredBy": false }' > "$S/minimal.json"

# NOTE: the scratch dir must have NO CLAUDE.md and NO AGENTS.md on its ancestor
# walk, or every fixture inherits one. Refuse loudly rather than lie.
d=$S
while [ "$d" != "/" ]; do
  for f in "$d/CLAUDE.md" "$d/AGENTS.md"; do
    [ -e "$f" ] && { echo "REFUSE: ancestor instruction file $f would contaminate fixtures" >&2; exit 2; }
  done
  d=$(dirname "$d")
done

tok() { tail -n 1 "$1" | sed -n 's/.*token=\([0-9A-F]*\).*/\1/p'; }
mk()  { printf '# %s\nKeep answers short.\n\n' "$2" > "$1"; "$C" emit "$3" >> "$1"; }

mk "$S/fix/loaded/CLAUDE.md" "fixture A" fixA-claude
mk "$S/fix/shadow/CLAUDE.md" "fixture B CLAUDE" fixB-claude
mk "$S/fix/shadow/AGENTS.md" "fixture B AGENTS" fixB-agents
rm -f "$S/fix/absent/CLAUDE.md"

printf '%s\ttoken\t%s\t%s\t-\n' fixA-claude "$S/fix/loaded/CLAUDE.md" "$(tok "$S/fix/loaded/CLAUDE.md")" > "$S/m_loaded.tsv"
{ printf '%s\ttoken\t%s\t%s\t-\n' fixB-claude "$S/fix/shadow/CLAUDE.md" "$(tok "$S/fix/shadow/CLAUDE.md")"
  printf '%s\ttoken\t%s\t%s\t-\n' fixB-agents "$S/fix/shadow/AGENTS.md" "$(tok "$S/fix/shadow/AGENTS.md")"; } > "$S/m_shadow.tsv"
printf '%s\ttoken\t%s\tDEADBEEFDEADBEEF\t-\n' fixC-claude "$S/fix/absent/CLAUDE.md" > "$S/m_absent.tsv"

R="$S/selftest.rows"; : > "$R"
run() { "$C" probe --manifest "$1" --dir "$2" --settings "${3:-$S/minimal.json}" --rows "$R" --evidence "$S/ev"; }
run "$S/m_loaded.tsv" "$S/fix/loaded"
run "$S/m_shadow.tsv" "$S/fix/shadow"
run "$S/m_absent.tsv" "$S/fix/absent"
run "$S/m_loaded.tsv" "$S/fix/loaded" "$S/NO_SUCH_SETTINGS.json"   # D: broken on purpose

fail=0
assert() { # slot, expected status, nth occurrence
  local got; got=$(grep -P "\t$1\t" "$R" | sed -n "$3p" | cut -f5)
  if [ "$got" = "$2" ]; then echo "ok    $1[$3] = $2"
  else echo "FAIL  $1[$3] expected $2 got ${got:-<no row>}"; fail=1; fi
}
echo "--- assertions ---"
assert fixA-claude LOADED       1   # A: canary is in a loaded CLAUDE.md
assert fixB-claude LOADED       1   # B: CLAUDE.md wins
assert fixB-agents NOT_LOADED   1   # B: AGENTS.md silently shadowed (the footgun)
assert fixC-claude NOT_LOADED   1   # C: file absent, probe clean
assert fixA-claude PROBE_BROKEN 2   # D: SAME manifest as A, broken probe
echo "--- cost ---"; grep '^# COST' "$R"
[ $fail -eq 0 ] && echo "SELFTEST PASS" || echo "SELFTEST FAIL"
exit $fail
