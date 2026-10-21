#!/usr/bin/env bash
set -euo pipefail

# ===== Config =====
BMC_IP="192.168.1.162"
TS_IP=""  # Auto-detected below

# Ports to forward: local_port -> BMC_IP:bmc_port
# 443  = HTTPS web UI
# 80   = HTTP web UI
# 5900 = KVM/VNC console
PORTS=(443 80 5900)

PIDFILE="/tmp/bmc-tunnel.pids"

# ===== Usage =====
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Forward BMC ports over Tailscale using socat (no IP forwarding needed).

After running, open https://<tailscale-ip> in your browser.

Options:
  -b, --bmc IP       BMC IP address (default: ${BMC_IP})
  --stop             Stop all forwarding
  --help             Show this help message

Examples:
  $(basename "$0")
  $(basename "$0") --stop
EOF
    exit 0
}

STOP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--bmc)  BMC_IP="$2"; shift 2 ;;
        --stop)    STOP=true; shift ;;
        --help)    usage ;;
        *)         echo "Unknown option: $1" >&2; usage ;;
    esac
done

stop_tunnel() {
    if [[ -f "$PIDFILE" ]]; then
        echo "Stopping BMC tunnel..."
        while read -r pid; do
            kill "$pid" 2>/dev/null && echo "  Stopped socat (PID $pid)" || true
        done < "$PIDFILE"
        rm -f "$PIDFILE"
        echo "Done."
    else
        echo "No tunnel running (no PID file found)."
    fi
}

if $STOP; then
    stop_tunnel
    exit 0
fi

# Stop any existing tunnel first
[[ -f "$PIDFILE" ]] && stop_tunnel

# Get Tailscale IP
TS_IP="$(tailscale ip -4 2>/dev/null || true)"
if [[ -z "$TS_IP" ]]; then
    echo "Error: Could not detect Tailscale IPv4 address. Is Tailscale running?" >&2
    exit 1
fi

# Check socat is installed
if ! command -v socat &>/dev/null; then
    echo "Error: socat is not installed. Install with: sudo apt install socat" >&2
    exit 1
fi

echo "=========================================="
echo "  BMC Tailscale Tunnel (socat)"
echo "=========================================="
echo ""
echo "  BMC address:      ${BMC_IP}"
echo "  Tailscale IP:     ${TS_IP}"
echo "  Forwarding ports: ${PORTS[*]}"
echo ""

> "$PIDFILE"

for port in "${PORTS[@]}"; do
    socat TCP-LISTEN:"${port}",bind="${TS_IP}",reuseaddr,fork TCP:"${BMC_IP}":"${port}" &
    pid=$!
    echo "$pid" >> "$PIDFILE"
    echo "  Port ${port} -> ${BMC_IP}:${port}  (PID ${pid})"
done

echo ""
echo "Tunnel is running. Access BMC at:"
echo "  https://${TS_IP}  (web UI)"
echo "  vnc://${TS_IP}:5900  (KVM console, if supported)"
echo ""
echo "To stop:  $0 --stop"
