Here are some utilities I use to be able to remotely switch on [my beloved PC, Minerva](https://garylvov.com/projects/minerva/)

I have an Intel Nuc (named Nudge) always on in my home network, which I remotely access via Tailscale. 

I typically turn off Minerva when I'm not working/training models as I want to conserve electricity (Minerva draws far more at idle than Nudge, the Nuc).

Minerva has a ASUS WRX90 SAGE SE mobo, which has a BMC.

I initially tried to set up Wake On LAN with a magic packet with the standard Ethernet connections, but found that the Intel network adapters (X70-TL) seemingly don't support
this functionality despite it being enabled in BIOS.

Nurdge is connected to a Network Switch, as is Minerva's BMC.

Then, using ``ipmitool`` locally on Minerva, I added a rescue admin user to the BMC:

```bash
# List existing BMC users
sudo ipmitool user list 1

# Create a new user in an unused slot (e.g., ID 3)
sudo ipmitool user set name 3 rescueadmin
sudo ipmitool user set password 3 StrongRescuePass
sudo ipmitool user enable 3
sudo ipmitool channel setaccess 1 3 callin=on ipmi=on link=on privilege=4
```

Note: BIOS/firmware updates can wipe BMC user accounts. If ``rescueadmin`` disappears, re-run the commands above from Minerva locally.

Finally, the ``boom.sh`` script in this directory can be used from Nudge to remotely turn on Minerva from a complete shutdown ;)

From my laptops, Aslan or Thunder, or my phone, I run the following.
```
alias wake="ssh nudge '~/boom.sh'"
```

## Troubleshooting: BMC Network Config Reset

After BIOS/firmware updates, the BMC network config resets to DHCP with IP `0.0.0.0`, so it disappears from the network.

### Recommended: DHCP reservation (self-healing)

Rather than fighting the reset, work with it. The BMC always falls back to DHCP after a firmware wipe, and it runs on standby power independently of the host OS, so it does DHCP whether Minerva is on, off, or mid-update. Add a **DHCP reservation on the router** (`192.168.1.1`) that maps the BMC's fixed MAC to its address:

```
cc:28:aa:d1:92:56  ->  192.168.1.162
```

With that in place, every future firmware reset self-corrects: the BMC asks for an address and the router always hands back `192.168.1.162`. No in-band action needed, and it works while Minerva is powered off.

To kick the BMC into re-acquiring its lease (e.g. right after a wipe), run `bmc_net_reset.sh` locally on Minerva. It flips the BMC to DHCP, cold-resets it, then verifies it landed on the reserved IP (a working check that the reservation is set up correctly). If DHCP yields no lease, it automatically falls back to static `192.168.1.162` so you are never stranded:

```bash
./bmc_net_reset.sh          # prompts for confirmation
./bmc_net_reset.sh -y       # non-interactive
```

### Manual static config

If you prefer a router-independent static config (survives reboots, but gets wiped by firmware updates), set it directly:

```bash
sudo ipmitool lan print 1                          # check current config
sudo ipmitool lan set 1 ipsrc static
sudo ipmitool lan set 1 ipaddr 192.168.1.162
sudo ipmitool lan set 1 netmask 255.255.255.0
sudo ipmitool lan set 1 defgw ipaddr 192.168.1.1
sudo ipmitool lan print 1                          # verify it saved
sudo ipmitool mc reset cold                         # required to apply
# wait ~60s for the BMC to reinitialize
```

The BMC MAC address is `cc:28:aa:d1:92:56` - useful for finding it (and for the reservation above).

### Verify from Nudge

After fixing the BMC config, verify from Nudge (or any machine on the LAN):

```bash
# Test connectivity
ping -c 3 192.168.1.162

# Test Redfish API
curl -ks https://192.168.1.162/redfish/v1/

# Test authentication and system status
curl -ksu rescueadmin:StrongRescuePass https://192.168.1.162/redfish/v1/Systems/Self
```

## Reaching the BMC web UI remotely

`bmc-tunnel.sh` runs on Nudge and forwards the BMC's web UI (443, 80) and KVM console (5900) over Tailscale with socat, so no IP forwarding is needed. Open `https://<nudge-tailscale-ip>` afterwards.

```bash
./bmc-tunnel.sh             # start forwarding to 192.168.1.162
./bmc-tunnel.sh -b <ip>     # different BMC address
./bmc-tunnel.sh --stop      # stop all forwarding
```
