#!/usr/bin/env bash
set -Eeuo pipefail

# Recovers the BMC network after a BIOS/firmware update wipes it back to
# DHCP 0.0.0.0.
#
# Strategy: pair this with a DHCP *reservation* on the router (192.168.1.1)
# that maps the BMC's fixed MAC to EXPECTED_IP. Because the BMC always falls
# back to DHCP after a firmware reset, the reservation makes it self-heal to a
# known address with no in-band action -- even while Minerva is powered off.
#
#   Router-side setup (one time):
#     add DHCP reservation  cc:28:aa:d1:92:56  ->  192.168.1.162
#
# This script makes the BMC side repeatable: flip to DHCP, cold-reset to force
# a fresh lease, and verify it lands on the reserved IP (a working check for
# the reservation itself). If DHCP yields no lease, it falls back to static so
# you are never left stranded. Run locally on Minerva with sudo.
#
# Usage: bmc_net_reset.sh [-y|--yes]   (-y skips the confirmation prompt)

BMC_MAC="cc:28:aa:d1:92:56"
EXPECTED_IP="192.168.1.162"
BMC_NETMASK="255.255.255.0"
BMC_GATEWAY="192.168.1.1"
BMC_CHANNEL=1
POLL_INTERVAL=5
POLL_TRIES=36   # ~180s

ASSUME_YES=0
[[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]] && ASSUME_YES=1

# Prints the BMC's current IPv4 address, or nothing if it can't be read.
get_bmc_ip() {
    sudo ipmitool lan print "$BMC_CHANNEL" 2>/dev/null \
        | awk -F: '/^IP Address[[:space:]]+:/ {gsub(/[[:space:]]/,"",$2); print $2; exit}'
}

# Polls until the BMC reports a non-zero IP; echoes it (stdout) or returns 1.
# Progress goes to stderr so callers can capture the IP cleanly.
wait_for_lease() {
    local i ip
    for i in $(seq 1 "$POLL_TRIES"); do
        sleep "$POLL_INTERVAL"
        ip="$(get_bmc_ip || true)"
        if [[ -n "$ip" && "$ip" != "0.0.0.0" ]]; then
            echo "$ip"
            return 0
        fi
        printf "  %ds (IP: %s)...\r" $((i * POLL_INTERVAL)) "${ip:-resetting}" >&2
    done
    return 1
}

echo "Current BMC LAN config:"
sudo ipmitool lan print "$BMC_CHANNEL"
echo ""

if [[ "$ASSUME_YES" -ne 1 ]]; then
    read -rp "Kick BMC to re-acquire its DHCP reservation (${BMC_MAC} -> ${EXPECTED_IP})? [y/N] " confirm
    [[ "${confirm,,}" == "y" ]] || { echo "Aborted."; exit 0; }
fi

echo "Setting IP source to DHCP..."
sudo ipmitool lan set "$BMC_CHANNEL" ipsrc dhcp

echo "Cold resetting BMC to force a fresh DHCP lease..."
sudo ipmitool mc reset cold

echo "Waiting for BMC to obtain a lease (polling every ${POLL_INTERVAL}s)..."
if ip="$(wait_for_lease)"; then
    echo ""
    echo "BMC came up at ${ip}."
    if [[ "$ip" == "$EXPECTED_IP" ]]; then
        echo "Matches the reserved IP -- DHCP reservation is working."
        echo ""
        sudo ipmitool lan print "$BMC_CHANNEL"
        echo ""
        echo "Done. Test from Nudge with: ping -c 3 ${EXPECTED_IP}"
        exit 0
    fi
    echo "WARNING: expected ${EXPECTED_IP} but got ${ip}."
    echo "Add or fix the router DHCP reservation: ${BMC_MAC} -> ${EXPECTED_IP}"
    echo "(BMC is reachable at ${ip} in the meantime.)"
    exit 0
fi

echo ""
echo "No DHCP lease after $((POLL_TRIES * POLL_INTERVAL))s. Falling back to static ${EXPECTED_IP}..."
sudo ipmitool lan set "$BMC_CHANNEL" ipsrc static
sudo ipmitool lan set "$BMC_CHANNEL" ipaddr "$EXPECTED_IP"
sudo ipmitool lan set "$BMC_CHANNEL" netmask "$BMC_NETMASK"
sudo ipmitool lan set "$BMC_CHANNEL" defgw ipaddr "$BMC_GATEWAY"

echo "Cold resetting BMC to apply static config..."
sudo ipmitool mc reset cold

echo "Waiting for BMC to come back (polling every ${POLL_INTERVAL}s)..."
if ip="$(wait_for_lease)"; then
    echo ""
    echo "BMC back at ${ip} (static fallback)."
    echo ""
    sudo ipmitool lan print "$BMC_CHANNEL"
    echo ""
    echo "Done. Test from Nudge with: ping -c 3 ${EXPECTED_IP}"
    exit 0
fi

echo ""
echo "BMC still unreachable after both DHCP and static attempts."
echo "Inspect manually with: sudo ipmitool lan print ${BMC_CHANNEL}"
exit 1
