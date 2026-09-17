#!/usr/bin/env bash
# Install a rendered ANSI banner as the SSH message-of-the-day, and mute the
# stock Ubuntu noise (help links, ESM nags, update counts) that ships with it.
set -Eeuo pipefail

# ===== Config =====
BANNER="${1:-$(dirname "$(readlink -f "$0")")/minerva.ans}"
DEST=/etc/motd.ans
SCRIPT=/etc/update-motd.d/01-banner
# Stock scripts to disable. Kept as chmod -x rather than rm so that an apt
# upgrade restoring the file does not silently bring the noise back.
MUTE=(10-help-text 50-motd-news 88-esm-announce 91-contract-ua-esm-status
      91-release-upgrade 90-updates-available 00-header)

# ===== Usage =====
usage() {
  cat <<'USAGE'
Usage: sudo ./install.sh [banner.ans]

Installs banner.ans (default: ./minerva.ans) as the MOTD shown on SSH login.
Generate a banner first with ./taag2ansi.py '<patorjk taag url>' -o banner.ans

Reverting: sudo rm /etc/update-motd.d/01-banner /etc/motd.ans
           sudo chmod +x /etc/update-motd.d/<whatever you want back>
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }
[[ $EUID -eq 0 ]] || { echo "needs root -- rerun with sudo" >&2; exit 1; }
[[ -f "$BANNER" ]] || { echo "no such banner: $BANNER" >&2; exit 1; }

install -m 0644 "$BANNER" "$DEST"

for f in "${MUTE[@]}"; do
  [[ -f "/etc/update-motd.d/$f" ]] && chmod -x "/etc/update-motd.d/$f"
done

cat > "$SCRIPT" <<EOS
#!/bin/sh
cat $DEST
EOS
chmod +x "$SCRIPT"

echo "installed. preview with: run-parts /etc/update-motd.d/"
