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

After BIOS/firmware updates, the BMC network config may reset to DHCP with IP `0.0.0.0`. To fix this, run from Minerva locally:

```bash
# Check current BMC LAN config
sudo ipmitool lan print 1

# If IP shows 0.0.0.0 or wrong address, reset to static:
sudo ipmitool lan set 1 ipsrc static
sudo ipmitool lan set 1 ipaddr 192.168.1.162
sudo ipmitool lan set 1 netmask 255.255.255.0
sudo ipmitool lan set 1 defgw ipaddr 192.168.1.1

# Verify config was saved
sudo ipmitool lan print 1

# Reset the BMC to apply network changes (required!)
sudo ipmitool mc reset cold

# Wait 60 seconds for BMC to fully reinitialize
```

The BMC MAC address is `cc:28:aa:d1:92:56` - useful for finding it if DHCP is used.

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
