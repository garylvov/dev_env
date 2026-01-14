#!/bin/bash
set -e

echo "Disabling Tailscale SSH..."
sudo tailscale set --ssh=false

echo "Re-enabling OpenSSH (service and socket)..."
sudo systemctl enable --now ssh.service ssh.socket

echo ""
echo "Done! OpenSSH is back to normal."
echo "Warning: Your machine may now be accessible via public IP. Consider firewall rules."
