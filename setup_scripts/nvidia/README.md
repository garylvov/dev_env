# This is how I make my Ubuntu NVIDIA drivers work

I've installed many NVIDIA drivers. From what I've experienced, it's best if you...

DON'T DO ANY OF THE FOLLOWING:``sudo apt-get install -y nvidia-open`` ``sudo apt install nvidia-driver-<VERSION> nvidia-dkms-<VERSION>``

ALSO DON'T DOWNLOAD THE DRIVER FROM THE ADDITIONAL DRIVERS

Instead, I highly recommend wiping all drivers from your system, and installing the drivers alongside cuda from the runfile
so that the the NVIDIA docker toolkit will interface correctly with the GPU. I've found it's really hard to resolve the dependencies without using docker for many more complex workloads, 
like deep learning training, or stuff to do with the NVIDIA high-performance computing toolkit. 
Even for things like training a PyTorch or Keras model I recommend using an image from the [NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch) registry.

This config currently works for **Ubuntu 22.04 Kernel 6.80-45-generic** , but also works for many kernels compatible with 22.04


# Driver / CUDA install 

Purge existing NVIDIA install:
```
sudo apt-get remove --purge '^nvidia-.*'
```

Get GCC, and set it to the right version. I recommend 13.

```
sudo apt-get install build-essential
wget -qO- https://raw.githubusercontent.com/garylvov/dev_env/main/setup_scripts/nvidia/update_gcc_patch.sh | bash -s -- --default_version 13
```

Download the runfile [here](https://developer.nvidia.com/cuda-downloads)

For example,
``wget https://developer.download.nvidia.com/compute/cuda/12.6.1/local_installers/cuda_12.6.1_560.35.03_linux.run``

If you are using an OS with a GUI (you are not connected via SSH):
do ONE of the following PRIOR to running the installer as otherwise the installation will fail.

- Reboot your computer and enter shell from GNU Grub -> Advanced Options for Ubuntu -> Recovery Mode -> Shell -> run the runfile -> boot normally (ctrl + D, then press enter to continue.)
- Stop your display manager, and enter shell (`sudo service gdm3 stop` -> `ctrl + ALT + (F2 or F3 or F4)` -> login to your user -> run the runfile -> `sudo service gdm3 restart`)

Run the installer. Select the driver, and the toolkit. DO NOT SELECT KERNEL OBJECTS!

    sudo sh cuda_12.6.1_560.35.03_linux.run

# Container Toolkit Install

First, install [Docker](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository). 

Also, install [Docker Compose](https://docs.docker.com/compose/install/linux/#install-using-the-repository). 

Make sure to follow the [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/). 

Then follow these steps to install the [NVIDIA container toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Make sure to follow the configuration steps.

More info about: [how to get around the NVML unknown error without rebooting, highly recommended for production environments](https://github.com/NVIDIA/nvidia-container-toolkit/issues/48)

More info about NVIDIA Contailer Toolkit subtleties see [distinctions between nvidia toolkit and docker setups explained](https://github.com/NVIDIA/nvidia-docker/issues/1268)



To check that the NVIDIA container toolkit is functioning, run

    docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi



# Use Nvidia Containers

If you'd like to use NVIDIA containers from [NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch) such as those which include PyTorch, high-performance computing kit, robotics simulation, etc, create [an account with NGC](https://ngc.nvidia.com/signin)
and generate a developer token, then login

```
docker login nvcr.io
#username: $oauthtoken
#password: <YOUR_DEVELOPER_TOKEN_HERE>
```


