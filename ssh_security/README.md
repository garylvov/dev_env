# SSH Security with Tailscale

I wanted to reduce my attack surface by not having my machines exposed to arbitrary SSH connections from the public internet, especially over IPv6 (which is globally routable by default). This guide covers how I lock down SSH to only be accessible via [Tailscale](https://tailscale.com/).

## What is Tailscale?

[Tailscale](https://tailscale.com/) is a mesh VPN built on WireGuard that makes it dead simple to connect your devices securely. The free plan supports up to 100 personal devices. I tried to use WireGuard alone once and quickly retreated to the comfort of Tailscale.

Key benefits:
- Zero-config mesh networking between your devices
- No need to open ports or deal with NAT traversal
- Built-in SSH server that only accepts connections from your Tailnet

## The Problem

If you have a public IPv6 address (check with `ip addr show | grep "inet6.*scope global"`), anyone on the internet can attempt to SSH into your machine. Even with strong passwords or key-only auth, I'd rather not have that attack surface exposed at all.

## The Solution: Tailscale SSH

Tailscale has a built-in SSH server that only accepts connections from your Tailnet. This means:
- No exposed ports on your public IP
- Authentication handled by Tailscale (uses your identity provider)
- No need to manage SSH keys across machines

## Prerequisites

Make sure Tailscale is installed and connected:
```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Verify your Tailscale IPs:
```
tailscale ip -4
tailscale ip -6
```

## Setup

Enable Tailscale SSH:
```
sudo tailscale set --ssh
```

Then disable OpenSSH entirely (most secure). Note: Ubuntu uses socket activation, so you need to disable both:
```
sudo systemctl disable --now ssh.service ssh.socket
```

Or run the script:
```
./setup.sh
```

## What About Outbound SSH?

This setup only affects **incoming** SSH connections to your machine. You can still:
- `ssh user@remote-server.com` - works normally
- `scp file.txt user@remote-server.com:~/` - works normally
- `rsync` to remote servers - works normally

If you need to push files from a remote server to your machine (instead of pulling), just initiate the transfer from your local machine instead:
```
# Instead of running this on the remote:
#   scp file.txt user@your-machine:~/

# Run this from your local machine:
scp user@remote-server.com:~/file.txt ./
```

## Connecting to Your Machine

From any device on your Tailnet:
```
ssh your-machine-name
# or
ssh your-tailscale-ip
```

No need to specify a user if you're using Tailscale SSH - it maps your Tailscale identity to local users via ACLs.

## Troubleshooting

### Can't SSH after enabling Tailscale SSH

Check that Tailscale SSH is actually enabled:
```
tailscale status
```

Look for `; ssh` in the output for your machine.

### Want to keep OpenSSH for local access

If you want OpenSSH available on localhost only (for local scripts, etc):
```
sudo tee /etc/ssh/sshd_config.d/localhost-only.conf << 'EOF'
ListenAddress 127.0.0.1
ListenAddress ::1
EOF
sudo systemctl restart ssh
```

### Need to re-enable OpenSSH temporarily

```
sudo systemctl start ssh
# Do your thing, then:
sudo systemctl stop ssh
```

### Tailscale ACLs blocking SSH

If SSH still doesn't work, check your Tailscale ACL policy at https://login.tailscale.com/admin/acls. You need to allow SSH access, something like:
```json
{
  "ssh": [
    {
      "action": "accept",
      "src": ["autogroup:members"],
      "dst": ["autogroup:self"],
      "users": ["autogroup:nonroot"]
    }
  ]
}
```

## Reverting

To go back to normal OpenSSH:
```
sudo tailscale set --ssh=false
sudo systemctl enable --now ssh.service ssh.socket
```

Or run:
```
./revert.sh
```
