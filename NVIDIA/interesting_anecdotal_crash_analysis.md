My system kept crashing for a reason unclear to me, Claude helped me figure out why

# GPU Crash Analysis -- minerva
## 2026-02-04

## Troubleshooting Commands

Commands used to collect diagnostics. Many require root (`sudo`).

```bash
# --- GPU state ---
nvidia-smi
nvidia-smi -q                                          # full query (all GPUs)
nvidia-smi -q -i 0                                     # per-GPU query (repeat for -i 1,2,3)
nvidia-smi -q -d UTILIZATION
nvidia-smi -q -d POWER
nvidia-smi -q -d TEMPERATURE
nvidia-smi -q -d CLOCK
nvidia-smi -q -d ECC
nvidia-smi -q -d VOLTAGE
nvidia-smi -q -d PERFORMANCE
nvidia-smi -q -d PAGE_RETIREMENT
nvidia-smi -q -d ROW_REMAPPER
nvidia-smi -q -d SUPPORTED_CLOCKS
nvidia-smi -q -d ACCOUNTING
nvidia-smi --query-gpu=index,name,pci.bus_id,utilization.gpu,utilization.memory,temperature.gpu,power.draw,clocks.gr,clocks.mem,pstate --format=csv
nvidia-smi topo -m                                     # topology matrix
nvidia-smi nvlink -s                                   # nvlink status
nvidia-smi nvlink -e                                   # nvlink errors
nvidia-smi pmon -c 1                                   # process monitor snapshot
nvidia-smi -pm 1                                       # enable persistence mode (fix)

# --- GPU debug dumps ---
sudo nvidia-debugdump --list
sudo nvidia-debugdump -D                               # full debug dump (binary)
sudo nvidia-bug-report.sh                              # comprehensive NVIDIA bug report

# --- Driver info ---
modinfo nvidia
cat /proc/driver/nvidia/version
cat /proc/driver/nvidia/params
cat /proc/driver/nvidia/gpus/*/information              # per-GPU sysfs info

# --- Kernel logs ---
dmesg                                                  # ring buffer (requires root)
cat /var/log/kern.log
cat /var/log/kern.log.1
cat /var/log/syslog
cat /var/log/syslog.1

# --- Journalctl ---
journalctl -k -b -p err                                # kernel errors this boot
journalctl -b --grep='nvidia|gpu|nvrm|xid' --no-pager
journalctl -b -u gdm --no-pager                        # display manager logs
journalctl -b -u nvidia* --no-pager                     # nvidia services
journalctl -b --no-pager                                # full boot journal

# --- PCIe diagnostics ---
lspci -nn                                              # all PCI devices (compact)
lspci -d 10de: -v                                      # NVIDIA devices (verbose)
lspci -d 10de: -vv                                     # NVIDIA devices (very verbose)
lspci -d 10de: -vvv                                    # NVIDIA devices (max verbose, needs root)
lspci -tv                                              # PCI topology tree
lspci -vvv -s 01:00.0                                  # per-GPU detail (repeat per slot)

# PCIe link state
cat /sys/bus/pci/devices/0000:XX:00.0/current_link_speed
cat /sys/bus/pci/devices/0000:XX:00.0/current_link_width
cat /sys/bus/pci/devices/0000:XX:00.0/max_link_speed
cat /sys/bus/pci/devices/0000:XX:00.0/max_link_width

# PCIe AER error counters
cat /sys/bus/pci/devices/0000:XX:00.0/aer_dev_correctable
cat /sys/bus/pci/devices/0000:XX:00.0/aer_dev_fatal
cat /sys/bus/pci/devices/0000:XX:00.0/aer_dev_nonfatal

# --- Kernel module state ---
lsmod | grep nvidia
cat /proc/sys/kernel/tainted                            # kernel taint flags

# --- Process inspection ---
sudo lsof /dev/nvidia*                                 # processes holding GPU devices
sudo fuser -v /dev/nvidia*

# --- System state ---
uname -a
cat /proc/cmdline                                       # boot parameters
cat /etc/default/grub                                   # GRUB config
free -h                                                 # memory
uptime
sensors                                                 # CPU/board temps (needs lm-sensors)
cat /sys/class/drm/card*/device/vendor                  # DRM device info

# --- Display ---
xrandr --verbose
cat /var/log/Xorg.0.log

# --- Coredumps ---
coredumpctl list

# --- GPU VBIOS ---
nvidia-smi --query-gpu=index,vbios_version --format=csv

# --- IOMMU ---
find /sys/kernel/iommu_groups/ -type l                  # IOMMU group assignments

# --- Recovery commands ---
sudo nvidia-smi --gpu-reset -i <GPU_ID>                 # attempt GPU reset
echo 1 | sudo tee /sys/bus/pci/devices/0000:XX:00.0/remove   # PCI hot-remove
echo 1 | sudo tee /sys/bus/pci/rescan                         # PCI rescan

# --- Monitoring (post-fix) ---
watch -n1 'cat /sys/bus/pci/devices/0000:21:00.0/aer_dev_correctable'
nvidia-smi dmon -d 5                                   # continuous GPU monitoring
```

## System Overview

| Component | Detail |
|-----------|--------|
| Host | minerva |
| CPU | AMD Ryzen Threadripper PRO 7955WX 16-Core |
| RAM | 128 GB (no swap) |
| Motherboard | ASUS Pro WS WRX90E-SAGE SE (BIOS 0404, 2023-12-20) |
| OS | Ubuntu 24.04, kernel 6.14.0-37-generic |
| Driver | 580.65.06 (NVIDIA Open Kernel Module), CUDA 13.0 |
| GRUB | `pcie_aspm=off rcutree.rcu_idle_gp_delay=1 acpi_osi=! acpi_osi=Linux` |

## GPU State at Time of Log Collection

| Slot | GPU | Status | PCIe Speed | BAR1 | Notes |
|------|-----|--------|------------|------|-------|
| 01:00.0 | RTX 3090 Ti | ERR! / Requires Reset | 16.0 GT/s (Gen4) | N/A | Xid 120/154 cascade victim |
| 21:00.0 | RTX 3090 | Working (28C, 22W, P8) | 2.5 GT/s (Gen1) | 256 MiB (4 MiB used) | 53,923 PCIe replays, 2 rollovers |
| C1:00.0 | RTX 3090 | ERR! / Requires Reset | 2.5 GT/s (Gen1) | N/A | Xid 120/154 cascade victim |
| E1:00.0 | RTX 3090 Ti | ERR! / Requires Reset | 16.0 GT/s (Gen4) | N/A | Primary crash origin |

## Crash Timeline

There are at least 3 crash events with the same pattern:

1. **Jan 26 15:01** -- `dmaAllocMapping_GM107` errors begin (kern.log.1)
2. **Feb 3 22:35** -- Full crash cascade on GPU 3 (e1:00.0)
3. **Feb 4 16:36** -- Identical crash cascade, detailed below:

```
16:36:05  BAR1 VA space exhaustion on e1:00.0 (hundreds of dmaAllocMapping_GM107 failures)
16:36:05  x86/PAT conflicting memory types, ioremap failures
16:36:05  Failed to map NvKmsKapiMemory on GPU e1:00
16:36:05  Xid 56 (display engine command error) on e1:00
16:36:xx  Chrome GPU process threads crash (invalid opcode traps)
16:36:54  Xid 119 -- GSP RPC timeout (45 seconds) on e1:00
16:37:39  Xid 119 repeats on e1:00
16:38:24  Xid 119 repeats on e1:00
16:38:32  Xid 120/154 on c1:00 -- GSP task exception, GPU Reset Required
16:38:38  Xid 120/154 on 01:00 -- GSP task exception, GPU Reset Required
16:38+    nvidia-modeset kernel thread blocked >245 seconds
16:44:34  gnome-remote-desktop.service fails with timeout (unkillable due to GPU hang)
```

## Root Cause Analysis

The primary failure is BAR1 Virtual Address space exhaustion on GPU 3 (e1:00.0, RTX 3090 Ti).

The causal chain:

1. **BAR1 is only 256 MiB** -- `EnableResizableBar: 0` in the driver parameters. Resizable BAR is disabled in BIOS or unsupported in current config. On a 24 GB GPU, 256 MiB of BAR1 address space is very tight when multiple clients map GPU memory.

2. **gnome-remote-desktop holds all 4 GPUs open** -- PID 3422 has ~80+ file descriptors open across all 4 GPU devices. This service uses GPU resources for remote desktop encoding and creates persistent BAR1 mappings that are never released. This is the likely dominant consumer of BAR1 space.

3. **Chrome GPU acceleration** adds additional BAR1 pressure. Chrome GPU process threads are visible in the crash logs dying with invalid opcode traps as the GPU becomes unresponsive.

4. **When BAR1 VA space fills up:**
   - Display framebuffer surfaces can't be mapped
   - Display engine commands fail (Xid 56)
   - Flip event timeouts occur
   - The GPU's GSP firmware becomes unresponsive (Xid 119, 45-second timeout)

5. **Cascade to other GPUs** -- The GSP hang on e1:00.0 causes the driver's internal state to corrupt, triggering Xid 120 (GSP task exception) and Xid 154 (GPU Reset Required) on GPUs at c1:00 and 01:00. GPU 1 at 21:00.0 survives only because it was idle and in P8 power state.

### Xid Error Reference

| Xid | Meaning | Context in This Crash |
|-----|---------|----------------------|
| 8 | GPU lockup / channel timeout | GPU watchdog detected unresponsive engine after BAR1 exhaustion |
| 16 | Display head error | Display pipeline stuck due to unmappable framebuffer surfaces |
| 56 | Display engine command error (CMDre) | Display engine commands reference unmapped BAR1 surfaces |
| 119 | GSP RPC timeout (45s) | GSP firmware on e1:00.0 stopped responding to driver RPCs |
| 120 | GSP task exception | GSP firmware on cascade-victim GPUs (c1:00, 01:00) hit exceptions |
| 154 | GPU Reset Required | Terminal state -- GPU cannot recover without reset/reboot |

### x86/PAT Conflict

The `x86/PAT: conflicting memory types` errors are a **symptom**, not a cause. They occur 3 seconds after BAR1 exhaustion begins, when Chrome's GPU process tries to `ioremap_wc` a BAR1 region that was previously mapped as uncached-minus. The PAT tracking system rejects the conflicting mapping request. Fix BAR1 exhaustion and these errors disappear.

## Contributing Factors

### NVIDIA Open Kernel Module on Consumer Ampere GPUs

The open kernel module (`nvidia-open`) relies heavily on GSP firmware for GPU management. On consumer-class GPUs (RTX 3090/3090 Ti), the GSP path is less mature and less tested than on datacenter GPUs (A100, H100) where it was originally developed. The `OpenRmEnableUnsupportedGpus: 1` parameter in the config confirms the driver is being forced to run on GPUs it doesn't officially support with the open module.

The proprietary module has a full in-kernel RM implementation and can run with `NVreg_EnableGpuFirmware=0` on Ampere, bypassing GSP entirely. The open module cannot -- it requires GSP.

### Kernel and Driver Versions

Kernel 6.14 is not an LTS release, and driver 580.x is very new. This combination has less real-world testing. The open module for 580.x is still relatively immature compared to 550.x/535.x.

### PCIe Signal Integrity on GPU 1 (21:00.0)

53,923 PCIe replay events with 2 rollovers is abnormal. While GPU 1 is the only one still working, this high replay count suggests a physical layer issue (bad riser cable, poor slot contact, or marginal signal integrity). This corresponds to the previously-observed PCIe AER RxErr correctable errors on this GPU. The AER counters currently show zeros because they were reset during the crash/recovery cycle.

### Other Factors

- **Persistence Mode disabled** on all GPUs -- increases BAR1 fragmentation from repeated init/teardown cycles
- **GPU 1 power limited** to 275W (default 420W, max 450W) -- not crash-related but notable
- **nvidia-cuda-mps-server** (PID 242488) also holding all 4 GPUs open
- **d3cold_allowed=1** on all GPUs -- allows deep power states that may interact poorly with GSP

## Recommended Fixes (Priority Order)

### 1. Switch from nvidia-open to the proprietary nvidia kernel module

This is the single highest-impact change. The proprietary module doesn't depend on GSP firmware for consumer GPUs and has a much longer stability track record on Ampere.

The driver was installed via NVIDIA's runfile installer, not apt packages (`dpkg -l | grep nvidia.*kernel` returns nothing). Despite intending to install the proprietary module, the system is running the open module -- confirmed by `proc_driver_nvidia.log` showing `NVIDIA UNIX Open Kernel Module`, license `Dual MIT/GPL`, and `OpenRmEnableUnsupportedGpus: 1`. The runfile installer may have defaulted to the open module or it was selected inadvertently.

Re-run the runfile installer with the `--no-openrm` flag to force the proprietary kernel module:

```bash
sudo sh NVIDIA-Linux-x86_64-580.65.06.run --no-openrm
```

Disabling GSP is optional and only recommended as a fallback if Xid 119 timeouts persist after switching to the proprietary module. With the proprietary module, GSP is better tested and the driver can recover more gracefully. Only if problems continue:

```bash
echo 'options nvidia NVreg_EnableGpuFirmware=0' | sudo tee /etc/modprobe.d/nvidia-gsp.conf
sudo update-initramfs -u
sudo reboot
```

### 2. Enable Resizable BAR in BIOS

This expands BAR1 from 256 MiB to potentially the full 24 GB VRAM, eliminating BAR1 VA exhaustion entirely. On the WRX90E-SAGE SE:

- Enter BIOS Setup
- Enable **Above 4G Decoding** (required prerequisite)
- Enable **Resizable BAR** / **ReBAR**
- **Disable CSM** (Compatibility Support Module) -- ReBAR requires UEFI-only boot
- Save and reboot

After reboot, verify with:

```bash
nvidia-smi -q | grep -i "bar1\|resize"
```

Also set the driver parameter:

```bash
# /etc/modprobe.d/nvidia.conf
options nvidia NVreg_EnableResizableBar=1
```

Note: With 4 GPUs each requesting 24 GiB of BAR space, the platform needs to support 96+ GiB of MMIO space above 4G. The WRX90E-SAGE SE on Threadripper PRO should support this, but verify after enabling.

### 3. Disable gnome-remote-desktop

This service holds all 4 GPUs open with dozens of file descriptors and is a primary contributor to BAR1 exhaustion. It cannot even be killed during a GPU hang (stuck in uninterruptible driver calls).

```bash
sudo systemctl disable --now gnome-remote-desktop.service
sudo systemctl mask gnome-remote-desktop.service
```

If you need remote desktop, use SSH + X forwarding, or a solution that doesn't probe all GPUs.

### 4. Enable persistence mode

```bash
sudo nvidia-smi -pm 1

# For persistence across reboots:
sudo systemctl enable nvidia-persistenced
```

This keeps the driver loaded and reduces BAR1 fragmentation from repeated init/teardown cycles.

### 5. Investigate GPU 1 PCIe replays

53,923 replays with 2 rollovers is a hardware-level concern:

- Reseat GPU 1 in its slot
- If using a riser cable, replace it
- Try GPU 1 in a different PCIe slot
- Check that the slot isn't physically damaged

After reboot, monitor:

```bash
watch -n1 'cat /sys/bus/pci/devices/0000:21:00.0/aer_dev_correctable'
```

### 6. Consider downgrading kernel and driver

For a production/workstation system with 4 GPUs:

- Kernel 6.8.x (Ubuntu 24.04 HWE) or 6.5.x (GA kernel) would be more stable
- Driver 550.x (LTS branch) has longer soak time than 580.x

### 7. BIOS updates and settings

- **Update BIOS** -- ASUS has released WRX90E-SAGE SE updates with AMD AGESA fixes for PCIe link training stability. Current BIOS 0404 is from December 2023.
- **PCIe Speed** -- Advanced > AMD CBS > NBIO Common Options > Set to Gen4 (not Auto)
- **IOMMU** -- If not using VFIO/passthrough, try disabling to rule out IOMMU-related PCIe issues

### 8. Immediate recovery

To get the 3 hung GPUs working again right now:

```bash
# Try nvidia-smi reset first (often fails on consumer GPUs in this state)
sudo nvidia-smi --gpu-reset -i 0
sudo nvidia-smi --gpu-reset -i 2
sudo nvidia-smi --gpu-reset -i 3

# If that fails, try PCI remove/rescan
echo 1 | sudo tee /sys/bus/pci/devices/0000:01:00.0/remove
echo 1 | sudo tee /sys/bus/pci/devices/0000:c1:00.0/remove
echo 1 | sudo tee /sys/bus/pci/devices/0000:e1:00.0/remove
sleep 2
echo 1 | sudo tee /sys/bus/pci/rescan

# If neither works, a full reboot is required
```

## Summary

The crashes follow a repeatable pattern: BAR1 VA space exhaustion on GPU e1:00.0 triggers a cascade that takes out 3 of 4 GPUs. The combination of a constrained 256 MiB BAR1 aperture, the open kernel module's GSP dependency on unsupported consumer GPUs, and gnome-remote-desktop's greedy GPU resource allocation creates the conditions for this failure. Switching to the proprietary module and enabling ReBAR should resolve this.

## Log Files Referenced

| File | Contents |
|------|----------|
| `kern.log` | Full crash timeline from Feb 3-4, BAR1 exhaustion cascade |
| `kern.log.1.log` | Earlier Jan 26 crash with identical dmaAllocMapping_GM107 pattern |
| `journalctl_kernel_gpu_errors.log` | Feb 4 Xid 119/120/154 cascade, gnome-remote-desktop timeout |
| `nvidia_smi_summary.log` | Post-crash GPU states showing 3 GPUs in ERR! |
| `nvidia_smi_full_query.log` | BAR1 256 MiB, PCIe replays 53923, recovery action "Reset" |
| `nvidia_smi_ecc.log` | Channel/TPC Repair Pending: "GPU requires reset" |
| `nvidia_smi_row_remapper.log` | Remapped Rows: "GPU requires reset" for GPUs 0, 2, 3 |
| `pcie_link_state.log` | Gen1 speed on GPUs 1/2, Gen4 on GPUs 0/3 |
| `pcie_aer_errors.log` | All zeros (reset since crash) |
| `proc_driver_nvidia.log` | Open module confirmed, EnableResizableBar=0, EnableGpuFirmware=18 |
| `nvidia_processes.log` | gnome-remote-desktop 80+ FDs, nvidia-cuda-mps-server |
| `system_state.log` | GRUB cmdline, CPU/RAM/motherboard info |
| `gpu_vbios_versions.log` | VBIOS versions for all 4 GPUs |
| `coredumps.log` | Chrome crash, Nautilus segfault, Xorg crash |
