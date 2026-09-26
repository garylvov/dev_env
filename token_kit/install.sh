#!/usr/bin/env bash
# Install the simplified launcher. Historical hook setup is in install-legacy.sh.
# Usage: bash token_kit/install.sh --legacy-root /path/to/previous/checkout [--dry-run]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
if command -v python3.11 >/dev/null 2>&1; then
  exec python3.11 -m token_kit.simple_release "$@"
fi
exec uv run --python '>=3.11' --no-project python -m token_kit.simple_release "$@"
