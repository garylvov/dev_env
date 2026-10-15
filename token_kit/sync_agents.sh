#!/usr/bin/env bash
# Copy token_kit/AGENTS.md (or ~/.config/agents/AGENTS.md) into ~/.codex/AGENTS.md as a marked block (idempotent).
# Re-run after `omx setup`, which may regenerate that file.
set -euo pipefail
SRC=token_kit/AGENTS.md (or ~/.config/agents/AGENTS.md); DST=~/.codex/AGENTS.md
BEGIN='<!-- BEGIN personal-rules (source: token_kit/AGENTS.md (or ~/.config/agents/AGENTS.md)) -->'; END='<!-- END personal-rules -->'
python3 -I - "$SRC" "$DST" "$BEGIN" "$END" <<'PY'
import sys,re
src,dst,b,e=sys.argv[1:]
rules=open(src).read().split('\n## Claude Code only')[0].strip()  # Claude-only section stays out of Codex
s=open(dst).read()
block=f"{b}\n## Personal rules\n{rules}\n{e}\n"
if b in s and e in s:
    s=s[:s.index(b)]+block+s[s.index(e)+len(e)+1:]
else:
    s=s.rstrip("\n")+"\n\n"+block
open(dst,"w").write(s)
PY
echo "synced personal rules into $DST"
