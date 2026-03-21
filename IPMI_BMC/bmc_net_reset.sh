#!/usr/bin/env bash
set -Eeuo pipefail

# Resets the BMC network config to static IP after a BIOS/firmware update
# wipes it back to DHCP 0.0.0.0. Must be run locally on Minerva with sudo.

BMC_IP="192.168.1.162"
BMC_NETMASK="255.255.255.0"
BMC_GATEWAY="192.168.1.1"
BMC_CHANNEL=1

echo "Current BMC LAN config:"
sudo ipmitool lan print "$BMC_CHANNEL"
echo ""

read -rp "Reset BMC network to static ${BMC_IP}? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
    echo "Aborted."
    exit 0
fi

echo "Setting static IP..."
sudo ipmitool lan set "$BMC_CHANNEL" ipsrc static
sudo ipmitool lan set "$BMC_CHANNEL" ipaddr "$BMC_IP"
sudo ipmitool lan set "$BMC_CHANNEL" netmask "$BMC_NETMASK"
sudo ipmitool lan set "$BMC_CHANNEL" defgw ipaddr "$BMC_GATEWAY"

echo "Cold resetting BMC (takes ~60s to come back)..."
sudo ipmitool mc reset cold

echo "Waiting 60s for BMC to reinitialize..."
sleep 60

echo "Verifying new config:"
sudo ipmitool lan print "$BMC_CHANNEL"

echo ""
echo "Done. Test from Nudge with: ping -c 3 ${BMC_IP}"
