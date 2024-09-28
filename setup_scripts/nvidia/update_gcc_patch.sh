#!/bin/bash

# Function to display usage instructions
usage() {
    echo "Usage: $0 -default <version> <version_to_install> [<other_versions_to_install>...]"
    echo "Example: $0 -default 13 11 12"
    exit 1
}

# Check for minimum number of arguments
if [ "$#" -lt 3 ]; then
    usage
fi

# Parse the -default argument and extract the default version
if [ "$1" == "-default" ]; then
    DEFAULT_VERSION=$2
    shift 2
else
    usage
fi

# Remaining arguments are the versions to install
VERSIONS_TO_INSTALL=("$@")

# Update package lists and upgrade system
echo "Updating package lists and upgrading system..."
sudo apt update
sudo apt-get upgrade -y
sudo apt-get dist-upgrade -y

# Install required packages
echo "Installing required packages..."
sudo apt-get install build-essential software-properties-common manpages-dev -y

# Add the Ubuntu Toolchain PPA
echo "Adding Ubuntu Toolchain PPA..."
sudo add-apt-repository ppa:ubuntu-toolchain-r/test -y
sudo apt-get update -y

# Install the specified GCC versions
echo "Installing GCC versions: ${VERSIONS_TO_INSTALL[*]}..."
for version in "${VERSIONS_TO_INSTALL[@]}"; do
    sudo apt install gcc-$version g++-$version -y
done

# Set up alternatives for each installed GCC version
echo "Setting up GCC alternatives..."
for version in "${VERSIONS_TO_INSTALL[@]}"; do
    sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-$version $version --slave /usr/bin/g++ g++ /usr/bin/g++-$version --slave /usr/bin/gcov gcov /usr/bin/gcov-$version
done

# Set the specified default GCC version
echo "Setting GCC $DEFAULT_VERSION as the default..."
sudo update-alternatives --set gcc /usr/bin/gcc-$DEFAULT_VERSION

# Verify the installed GCC and G++ versions
echo "Verifying GCC installation..."
gcc --version
g++ --version

echo "GCC setup complete!"

