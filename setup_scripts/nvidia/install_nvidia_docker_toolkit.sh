#!/bin/bash

echo "THIS SCRIPT ASSUMES THAT nvidia-smi COMMAND WORKS!"
echo "THIS SCRIPT ASSUMES THAT YOU DON'T HAVE DOCKER DESKTOP (incompatible with nvidia container toolkit) INSTALLED!"


# Update and install required packages
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common

# Add Docker's official GPG key
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

# Set up the stable repository for Docker
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Update apt package index
sudo apt-get update

# Install Docker CE
sudo apt-get install -y docker-ce docker-ce-cli containerd.io

# Check if the docker group already exists, if not, add it
if [ $(getent group docker) ]; then
    echo "Docker group already exists"
else
    sudo groupadd docker
fi

# Add your user to the docker group
sudo usermod -aG docker $USER

# Restart Docker to apply changes
sudo systemctl enable docker
sudo systemctl restart docker

echo "*!*!* Docker installation completed *!*!*"

echo "*!*!* Setting up Nvidia Docker support *!*!*"

# Add the package repositories for Nvidia Docker
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-docker-archive-keyring.gpg
curl -s -L "https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list" | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-docker-archive-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

# Update apt package index
sudo apt-get update

# Install NVIDIA runtime packages
sudo apt-get install -y nvidia-container-toolkit

# Restart Docker to apply changes
sudo systemctl restart docker

echo "*!*!* Nvidia Docker setup completed :) *!*!*"


echo "Adding user to docker group so that sudo isn't needed with docker commands"
sudo groupadd docker
sudo usermod -aG docker $USER
newgrp docker

