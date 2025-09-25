Here are some utilities I use to be able to remotely switch on [my beloved PC, Minerva](https://garylvov.com/projects/minerva/)

I have an Intel Nuc (named Parallelepid) always on in my home network, which I remotely access via Tailscale. 

I typically turn off Minerva when I'm not working/training models as I want to conserve electricity (Minerva draws far more at idle than Parallelepid, the Nuc).

Minerva has a ASUS WRX90 SAGE SE mobo, which has a BMC.

I initially tried to set up Wake On LAN with a magic packet with the standard Ethernet connections, but found that the Intel network adapters (X70-TL) seemingly don't support
this functionality despite it being enabled in BIOS.

My Intel Nuc is connected to a Network Switch, as is Minerva's BMC.

Then, using ``ipmitool`` from Minerva I added some user perms to the BMC:

```
# List existing BMC users
ipmitool -I lanplus -H 192.168.1.162 -U ADMIN -P ADMIN user list

# Create a new user in an unused slot (e.g., ID 3)
ipmitool -I lanplus -H 192.168.1.162 -U ADMIN -P ADMIN user set name 3 rescueadmin
ipmitool -I lanplus -H 192.168.1.162 -U ADMIN -P ADMIN user set password 3 StrongRescuePass
ipmitool -I lanplus -H 192.168.1.162 -U ADMIN -P ADMIN user enable 3
ipmitool -I lanplus -H 192.168.1.162 -U ADMIN -P ADMIN channel setaccess 1 3 callin=on ipmi=on link=on privilege=4
```

Finally, the ``boom.sh`` script in this directory can be used from Parallelepid to remotely turn on Minerva from a complete shutdown ;)
