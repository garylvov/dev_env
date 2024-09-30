#!/bin/bash

echo "Make sure you don't have docker desktop installed"
echo "This script was created by Gary Lvov"
echo "Please open a Github issue at https://github.com/garylvov/dev_env/issues if this didn't work"

# Function to check if Docker is installed and get its version
get_docker_version() {
    if ! command -v docker &> /dev/null; then
        echo "Docker is not installed."
        return 1
    fi
    docker --version | grep -Eo '([0-9]{1,}\.)+[0-9]{1,}]'
}

# Function to prompt for uninstalling Docker or NVIDIA components
prompt_for_uninstall() {
    read -p "Existing $1 detected. Would you like to uninstall it before installing the new version (y/n)? " choice
    case "$choice" in 
        y|Y ) return 0;;
        * ) return 1;;
    esac
}

# Function to uninstall Docker
uninstall_docker() {
    echo "Uninstalling Docker..."
    sudo apt-get purge -y docker-ce docker-ce-cli containerd.io
    sudo apt-get autoremove -y
    sudo rm -rf /var/lib/docker
}

# Function to uninstall NVIDIA Container Toolkit or NVIDIA Docker 2
uninstall_nvidia() {
    echo "Uninstalling NVIDIA Docker components..."
    sudo apt-get purge -y nvidia-docker2 nvidia-container-toolkit
    sudo apt-get autoremove -y
}

# Function to install Docker
install_docker() {
    echo "Installing Docker..."
    sudo apt-get update
    sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io
    sudo systemctl enable docker
    sudo systemctl restart docker
}

# Function to install nvidia-container-toolkit
install_nvidia_toolkit() {
    echo "Installing NVIDIA Container Toolkit..."
    distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
    curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-docker-archive-keyring.gpg
    curl -s -L "https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list" | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-docker-archive-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-docker.list
    sudo apt-get update
    sudo apt-get install -y nvidia-container-toolkit
    sudo systemctl restart docker
}

# Function to install nvidia-docker2
install_nvidia_docker2() {
    echo "Installing NVIDIA Docker 2..."
    distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
    curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-docker-archive-keyring.gpg
    curl -s -L "https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list" | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-docker-archive-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-docker.list
    sudo apt-get update
    sudo apt-get install -y nvidia-docker2
    sudo systemctl daemon-reload
    sudo systemctl restart docker
}

# Function to prompt for upgrading Docker
prompt_for_upgrade() {
    read -p "Your Docker version is less than 19.03. Would you like to upgrade Docker (y/n)? " choice
    case "$choice" in 
        y|Y ) return 0;;
        * ) return 1;;
    esac
}

# Function to check for `nvidia-smi` using both methods and output the working GPU command
check_nvidia_smi() {
    echo "Trying with '--gpus all' flag"
    echo "Trying docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi"
    docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

    if [ $? -eq 0 ]; then
        echo "GPU command type: '--gpus all'"
        echo "run nvidia containers with: docker run --rm --gpus all"
        return 0
    else
        echo "Failed to run nvidia-smi with '--gpus all'."
    fi

    echo "Trying with '--runtime=nvidia' flag"
    echo "Trying docker run --rm --runtime=nvidia nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi"
    docker run --rm --runtime=nvidia nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

    if [ $? -eq 0 ]; then
        echo "GPU command type: '--runtime=nvidia' flag'"
        echo "run nvidia containers with: docker run --rm --runtime=nvidia"
        return 0
    else
        echo "Failed to run nvidia-smi with '--runtime=nvidia'."
    fi

    echo "Could not successfully run nvidia-smi with either method."
}


# Main installation logic
main() {
    enable_kubernetes="false"
    
    # Check for command-line arguments
    while [[ "$#" -gt 0 ]]; do
        case $1 in
            --enable_kubernetes) enable_kubernetes="true";;
            *) echo "Unknown parameter passed: $1"; exit 1;;
        esac
        shift
    done

    # Check if Docker is installed
    docker_version=$(get_docker_version)

    # If Docker is installed, prompt for uninstallation
    if [ $? -eq 0 ]; then
        echo "Docker version $docker_version detected."
        if prompt_for_uninstall "Docker"; then
            uninstall_docker
        fi
    else
        docker_version="none"
    fi

    # Check if any NVIDIA components are installed
    if check_existing_nvidia; then
        if prompt_for_uninstall "NVIDIA Docker components"; then
            uninstall_nvidia
        fi
    fi

    # Proceed with Docker and NVIDIA installation
    if [ "$enable_kubernetes" == "true" ]; then
        echo "Installing Docker and NVIDIA Docker 2 for Kubernetes support..."
        install_docker
        install_nvidia_docker2
    else
        if [ "$docker_version" != "none" ] && dpkg --compare-versions "$docker_version" "ge" "19.03"; then
            echo "Installing Docker and NVIDIA Container Toolkit..."
            install_docker
            install_nvidia_toolkit
        else
            if prompt_for_upgrade; then
                echo "Upgrading Docker and installing NVIDIA Container Toolkit..."
                install_docker
                install_nvidia_toolkit
            else
                echo "Installing Docker and NVIDIA Docker 2 for older Docker version..."
                install_docker
                install_nvidia_docker2
            fi
        fi
    fi

    # Add user to docker group if needed
    if ! getent group docker > /dev/null; then
        sudo groupadd docker
    fi
    sudo usermod -aG docker $USER

    # Note: Do not use newgrp; instead, log a message to the user to re-login
    echo "You have been added to the Docker group. Please log out and log back in for the changes to take effect."
    echo "Docker and NVIDIA setup completed."
    echo "Checking how you should specify the GPU runtime to docker"
    echo "If nvidia-smi outputs correctly below, then this script ran correctly"
    check_nvidia_smi
    echo "This script was created by Gary Lvov"
    echo "Please open a Github issue at https://github.com/garylvov/dev_env/issues if this script didn't work for you"
    echo "You can also star https://github.com/garylvov/dev_env/ if it did work."
}

# Run the main function, accepting --enable_kubernetes as an argument
main "$@"
