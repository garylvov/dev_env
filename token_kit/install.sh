#!/usr/bin/env bash
# install.sh -- the ONE command. It is a bootstrap and nothing else: make sure
# `uv` and a modern Python exist, then hand every decision to the Python CLI.
#
#   bash token_kit/install.sh [--dry-run] [--uninstall] [--probe] [--census]
#                             [--config] [--prompts [--out FILE] [--since D]]
#
# There is no profile to pick: every machine fact is detected at run time.
# `--config` prints what this machine decided and where each value came from.
#
# Why uv: the system Python on some machines is 3.9, which has no tomllib. uv
# supplies 3.11+ without touching the system Python and without building an
# environment -- the kit imports nothing but the standard library.
#
# Everything this installs, and how to remove it, is documented in the CLI:
#   bash install.sh --help-full
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$HERE/src/token_kit/cli.py"
PY_REQ='>=3.11'

SUB=install
ARGS=()
for a in "$@"; do
  case "$a" in
    --uninstall)  SUB=uninstall ;;
    --probe)      SUB=probe ;;
    --census)     SUB=census ;;
    --config)     SUB=config ;;
    --prompts)    SUB=prompts ;;
    --help-full)  SUB=""; ;;
    *)            ARGS+=("$a") ;;
  esac
done

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
  for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$cand" ] && { PATH="$(dirname "$cand"):$PATH"; export PATH; break; }
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "install: uv not found -- installing it (astral.sh standard installer)"
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    echo "install: FAILED to install uv. Install it yourself and re-run:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  PATH="$HOME/.local/bin:$PATH"; export PATH
fi
command -v uv >/dev/null 2>&1 || { echo "install: uv still not on PATH" >&2; exit 1; }

# 2. a Python that has tomllib, fetched ONCE here so that the hook, which runs
#    on every tool call, never pays for an interpreter download.
uv python find "$PY_REQ" >/dev/null 2>&1 || uv python install 3.11 || {
  echo "install: could not provision Python $PY_REQ" >&2; exit 1; }

# 3. hand over.
if [ -z "$SUB" ]; then exec uv run --python "$PY_REQ" --no-project "$CLI" --help; fi
exec uv run --python "$PY_REQ" --no-project "$CLI" "$SUB" "${ARGS[@]+"${ARGS[@]}"}"
