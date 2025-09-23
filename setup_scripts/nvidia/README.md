# This is how I make my Ubuntu NVIDIA drivers work

I'd estimate that I've installed NVIDIA drivers 100+ times. Here is the definitive guide of how to do it the right way (in my opinion).

# Driver / CUDA install 

Make sure to disable secure boot in your computer's BIOS!

From what I've experienced, it's best if you...

**Don't do any of the following:**

``sudo apt-get install -y nvidia-open`` 

``sudo apt install nvidia-driver-<VERSION> nvidia-dkms-<VERSION>``

**Also, don't download the driver from the additional drivers tab.**

**Instead, I highly recommend wiping all drivers from your system, and installing the drivers alongside CUDA from the runfile**
so that the the NVIDIA docker toolkit will interface correctly with the GPU. 

People often ask me why I install using the runfile instead of other methods. 
Honestly, I don't have a deep technical reason, but in my experience, it’s been by far the most reliable approach.
I suspect using the runfile installs the driver and CUDA together more carefully for your specific system, without relying on any pre-packaged versions that may have conflicts.


I've found it's really hard to resolve the dependencies without using Docker for many more complex workloads, 
like deep learning training, or stuff to do with the NVIDIA high-performance computing toolkit. 
Even for things like training a PyTorch or Keras model I recommend using an image from the [NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch) registry.

This config is confirmed to work on **Ubuntu Versions 24.04, 22.04 | Kernel versions: 6.14.0-28-generic,  6.80-45-generic, 6.8.0-51-generic, 6.8.0-52-generic**, but also works for many other configs.

**If you already have NVIDIA Drivers or CUDA installed, I highly recommend purging them entirely from your system using the instructions provided in [this StackOverflow answer](https://stackoverflow.com/a/62276101) before starting a fresh installation.** Then, get started on the actual install below.

**Install build tools.**

```
sudo apt-get install build-essential
```

Set GCC to the right version. I recommend 13. For Ubuntu versions newer than 22.04, you likely can omit this step, unless you get errors related to ``ftrivial``.
```
wget -qO- https://raw.githubusercontent.com/garylvov/dev_env/main/setup_scripts/nvidia/update_gcc_patch.sh | bash -s -- --default_version 13
```

Download the runfile [here](https://developer.nvidia.com/cuda-downloads)

For example,
```
wget https://developer.download.nvidia.com/compute/cuda/12.6.1/local_installers/cuda_12.6.1_560.35.03_linux.run
```

If you are using an OS with a GUI (you are not connected via SSH):
do ONE of the following PRIOR to running the installer as otherwise the installation will fail.

- Reboot your computer and enter shell from GNU Grub -> Advanced Options for Ubuntu -> Recovery Mode -> Shell -> run the runfile -> boot normally (ctrl + D, then press enter to continue.)
- Stop your display manager, and enter shell (`sudo service gdm3 stop` -> `ctrl + ALT + (F2 or F3 or F4)` -> login to your user -> run the runfile -> `sudo service gdm3 restart`)

Run the installer. Select the driver, and the toolkit. I would not select kernel objects unless you are confident that they are absolutely needed. 

    sudo sh cuda_12.6.1_560.35.03_linux.run # or replace the runfile version to match yours

If everything installed correclty, then ```nvidia-smi``` should show your GPUs.

# Container Toolkit Install

First, install [Docker](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository). 

Also, install [Docker Compose](https://docs.docker.com/compose/install/linux/#install-using-the-repository). 

Make sure to follow the [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/). 

Then follow these steps to install the [NVIDIA container toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Make sure to follow the configuration steps.

More info about: [how to get around the NVML unknown error without rebooting, highly recommended for production environments](https://github.com/NVIDIA/nvidia-container-toolkit/issues/48)

More info about NVIDIA Contailer Toolkit subtleties see [distinctions between nvidia toolkit and docker setups explained](https://github.com/NVIDIA/nvidia-docker/issues/1268)



To check that the NVIDIA container toolkit is functioning, run
```
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

In some cases, you may also need some additional flags as follows, although ideally these would be configured by the ``sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`` .

```
# Need to add each device in /dev/nvidia*
docker run --rm --gpus all \
 --runtime=nvidia \
--device=/dev/nvidia-uvm \
--device=/dev/nvidia-uvm-tools \
--device=/dev/nvidia-modeset \
--device=/dev/nvidiactl \
--device=/dev/nvidia0 \
nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

# Power and Clock Limiting for Multi-GPU Stability

If you have a multi-GPU rig, you may want to limit your GPU wattage and clock speeds. I find that without limiting these parameters, multi-GPU rigs may attempt to draw more power than the power supply can provide, leading to crashes while attempting to run training. This is mostly for local rigs, as a rented node from a cloud provider is probably already configured properly. Laptops with dedicated GPUs are also probably already configured properly.

You can check power and clock information with
```
nvidia-smi -q -d CLOCK,POWER
```

Then, the power and clocks can be configured with similar to the following. If persistence mode is disabled, you should do this after every time your computer turns on. The example below is what works well for my personal quad 3090 rig with a 1600W power supply and a Threadripper Pro CPU (~350W TDP), but you should adjust these values with some experimentation/the output of the above power/clock information to optimize performance for your rig.
```
sudo nvidia-smi -pl 300 -i 0,1,2,3 # power limit to 300W each for 4 gpus
sudo nvidia-smi -lgc 0,1800 -i 0,1,2,3 # limit GPU clock frequency from 0 - 1800 MHz each for 4 gpus
sudo nvidia-smi -lmc 0,1000 -i 0,1,2,3 # limit GPU memory frequency from 0 - 1000 MHz each for 4 gpus
```

For more information on power limiting, see [Tim Dettmers' awesome hardware blog](https://timdettmers.com/2023/01/30/which-gpu-for-deep-learning/#Power_Limiting_An_Elegant_Solution_to_Solve_the_Power_Problem).


# Using Nvidia Containers

If you'd like to use NVIDIA containers from [NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch) such as those which include PyTorch, high-performance computing kit, robotics simulation, etc, create [an account with NGC](https://ngc.nvidia.com/signin)
and generate a developer token, then login

```
docker login nvcr.io
#username: $oauthtoken
#password: <YOUR_DEVELOPER_TOKEN_HERE>
```

# If ```nvidia-smi``` works but ``nvcc --version`` isn't found

In this case, the driver is installed, but the CUDA toolkit is either not found on path or is not installed.

Make sure to add the following to your ```~/.bashrc```.
```
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
```

If after ```source ~/.bashrc```, if ```nvcc --version``` still is not found, then CUDA is not installed.

In this case, I recommend wiping the drivers and installing the driver alongside CUDA with the runfile as detailed previously in this guide. 
However, if you're feeling lucky, you can try to install CUDA on top of your existing drivers. In this case, check what CUDA version ```nvidia-smi``` mentions, then
download the CUDA runfile with the matching CUDA version and run it. In the install menu, make sure to deselect the driver as part of the install. There will be a warning an existing driver installation being found, but you can
try disregarding the warning and installing CUDA regardless.

# Enabling Wayland

I personally don't like to use ``x11``. 
This is due to an [annoying bug](https://askubuntu.com/questions/1044985/using-2-keyboards-at-the-same-time-create-annoying-input-lag) related to weilding dual keyboards like I do.
The workaround I currently do is to use Wayland. 
When logging into my user, I select the gear on the bottom right, and select "Ubuntu on Wayland". 
However, Wayland may not be correctly configured, and at first, this gear may not even be visible.
I found that the following works well for enabling Wayland on NVIDIA drivers.
```
echo 'options nvidia-drm modeset=1' | sudo tee /etc/modprobe.d/nvidia-drm.conf >/dev/null
sudo update-initramfs -u -k "$(uname -r)"
reboot
```

Finally, check that Wayland is enabled with ``echo $XDG_SESSION_TYPE`` after logging in.

# Extra Resources

Some resources I personally find helpful:

- [Test GPU Communication Bandwidth](https://github.com/nvidia/nvbandwidth)

- [Human readable CUDA documentation by Modal](https://modal.com/gpu-glossary) 

- [Which GPU(s) to Get for Deep Learning by Tim Dettmers](https://timdettmers.com/2023/01/30/which-gpu-for-deep-learning/)

- [A full hardware guide to deep learning by Tim Dettmers](https://timdettmers.com/2018/12/16/deep-learning-hardware-guide/)



# Troubleshooting

---
If CUDA magically disappears after some time in Docker, try supplying the following flags in the docker run command.


```
docker run --rm --gpus all \
 --runtime=nvidia \
--device=/dev/nvidia-uvm \
--device=/dev/nvidia-uvm-tools \
--device=/dev/nvidia-modeset \
--device=/dev/nvidiactl \
--device=/dev/nvidia0 \
<IMAGE> <COMMAND> #Example: nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

---
On a failed CUDA/driver install, ``cat`` the suggested logs for CUDA, and the suggested log for the driver (path is in the suggested CUDA logs) . 


If your computer mentions Nouveau after a failed, try the following and reinstall.

```
sudo bash -c "echo blacklist nouveau > /etc/modprobe.d/blacklist-nvidia-nouveau.conf"
sudo bash -c "echo options nouveau modeset=0 >> /etc/modprobe.d/blacklist-nvidia-nouveau.conf"
sudo update-initramfs -u
sudo reboot
```

If your computer mentions ``ftrivial``, try the following and reinstall.

```
wget -qO- https://raw.githubusercontent.com/garylvov/dev_env/main/setup_scripts/nvidia/update_gcc_patch.sh | bash -s -- --default_version 13
```

For other cases, Google the error from the logs and pray that someone has encountered it before lol ;)

---

If your computer previously had ``nvidia-smi`` working, but then it mysteriously stopped, re-run the CUDA 
runfile as shown previously in this guide (this is likely because of a kernel update). After selecting the installation components, you will be prompted with a menu that mentions
that an existing CUDA installation was found. In this case, select the ``upgrade all`` option.

--- 

For the following case,
```
$ docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
Unable to find image 'nvidia/cuda:11.8.0-base-ubuntu22.04' locally
11.8.0-base-ubuntu22.04: Pulling from nvidia/cuda
aece8493d397: Pull complete 
5e3b7ee77381: Pull complete 
5bd037f007fd: Pull complete 
4cda774ad2ec: Pull complete 
775f22adee62: Pull complete 
Digest: sha256:f895871972c1c91eb6a896eee68468f40289395a1e58c492e1be7929d0f8703b
Status: Downloaded newer image for nvidia/cuda:11.8.0-base-ubuntu22.04
docker: Error response from daemon: could not select device driver "" with capabilities: [[gpu]].
```

First check that ``nvidia-smi`` works on the host machine. If it does, reinstall the NVIDIA Container Toolkit as shown previously in this guide. Otherwise,
reinstall CUDA/the driver.

---

For unknown NVML issues:
- [How to get around the NVML unknown error without rebooting](https://github.com/NVIDIA/nvidia-container-toolkit/issues/48)
- [Related Stack Overflow issue](https://stackoverflow.com/questions/72932940/failed-to-initialize-nvml-unknown-error-in-docker-after-few-hours)



