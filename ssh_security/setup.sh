#!/bin/bash
set -e

echo "Checking Tailscale status..."
if ! tailscale status &>/dev/null; then
    echo "Error: Tailscale is not running. Please run 'sudo tailscale up' first."
    exit 1
fi

echo "Your Tailscale IPs:"
echo "  IPv4: $(tailscale ip -4)"
echo "  IPv6: $(tailscale ip -6)"

echo ""
echo "Enabling Tailscale SSH..."
sudo tailscale set --ssh

echo "Disabling OpenSSH (service and socket)..."
sudo systemctl disable --now ssh.service ssh.socket

echo ""
echo "Done! SSH is now only accessible via Tailscale."
echo "To connect: ssh $(hostname) (from any device on your Tailnet)"
echo ""
echo "To revert: ./revert.sh"
