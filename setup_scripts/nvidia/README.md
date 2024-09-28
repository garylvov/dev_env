# This is how I make my Ubuntu NVIDIA drivers work

I've installed many NVIDIA drivers. From what I've experienced, it's best if you...

DON'T DO ANY OF THE FOLLOWING:``sudo apt-get install -y nvidia-open`` ``sudo apt install nvidia-driver-<VERSION> nvidia-dkms-<VERSION>``

Instead, I highly recommend wiping all drivers from your system, and installing the drivers alongside cuda from the runfile
so that the the NVIDIA docker toolkit will interface correctly with the GPU. I've found it's really hard to resolve the dependencies without using docker for many more complex workloads, 
like deep learning training, or stuff to do with the NVIDIA high-performance computing toolkit. 
Even for things like training a PyTorch or Keras model I recommend using an image from the [NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch) registry.

This config currently works for **Ubuntu 22.04 Kernel 6.80-45-generic** , but also works for many kernels compatible with 22.04


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



``sudo sh cuda_12.6.1_560.35.03_linux.run``

If you are visual


You may want to try:

