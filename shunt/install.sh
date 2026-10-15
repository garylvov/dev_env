#!/usr/bin/env bash
# Install shunt (user-level), config, systemd user service, and Claude Code agents. Reversible; backs up existing files.
set -euo pipefail
VER="${SHUNT_VERSION:-v0.55.1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
case "$(uname -m)" in x86_64) A=x64 ;; aarch64) A=arm64 ;; *) echo "unsupported arch"; exit 1 ;; esac
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
curl -fsSL -o "$T/shunt" "https://github.com/pleaseai/shunt/releases/download/$VER/shunt-linux-$A"
curl -fsSL -o "$T/SHA256SUMS" "https://github.com/pleaseai/shunt/releases/download/$VER/SHA256SUMS"
(cd "$T" && grep " shunt-linux-$A\$" SHA256SUMS | sed "s/shunt-linux-$A/shunt/" | sha256sum -c -)
mkdir -p ~/.local/bin ~/.config/shunt ~/.config/systemd/user ~/.claude/agents
install -m755 "$T/shunt" ~/.local/bin/shunt
backup() { [ -e "$1" ] && cp -p "$1" "$1.bak-$TS" || true; }
backup ~/.config/shunt/shunt.toml; cp "$HERE/shunt.toml" ~/.config/shunt/shunt.toml
backup ~/.config/systemd/user/shunt.service; cp "$HERE/shunt.service" ~/.config/systemd/user/shunt.service
for f in astra sol luna; do backup ~/.claude/agents/$f.md; cp "$HERE/agents/$f.md" ~/.claude/agents/$f.md; done
~/.local/bin/shunt check --config ~/.config/shunt/shunt.toml
systemctl --user daemon-reload && systemctl --user enable --now shunt.service
echo "Done. Run 'codex login' if ~/.codex/auth.json is missing, then merge the settings.json snippet from shunt/README.md."
