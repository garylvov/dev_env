#!/bin/bash

# Use me like: sudo bash update_gcc_patch.sh --default_version 13

# Function to display usage instructions
usage() {
    echo "Usage: $0 --default_version <version> [--bonus_versions <version_to_install>...]"
    echo "Example: $0 --default_version 13 --bonus_versions 12 11"
    exit 1
}

# Check if at least one argument (default_version) is provided
if [ "$#" -lt 2 ]; then
    usage
fi

# Parse command-line arguments
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --default_version)
            DEFAULT_VERSION="$2"
            shift 2
            ;;
        --bonus_versions)
            BONUS_VERSIONS=()
            while [[ "$#" -gt 1 && ! "$2" =~ ^-- ]]; do
                BONUS_VERSIONS+=("$2")
                shift
            done
            shift
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

# Check if default_version is set
if [ -z "$DEFAULT_VERSION" ]; then
    usage
fi

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

# Install the default GCC version
echo "Installing default GCC version: $DEFAULT_VERSION"
sudo apt install gcc-$DEFAULT_VERSION g++-$DEFAULT_VERSION -y

# Install bonus versions if provided
if [ "${#BONUS_VERSIONS[@]}" -gt 0 ]; then
    echo "Installing bonus GCC versions: ${BONUS_VERSIONS[*]}"
    for version in "${BONUS_VERSIONS[@]}"; do
        sudo apt install gcc-$version g++-$version -y
    done
fi

# Set up alternatives for each installed GCC version
echo "Setting up GCC alternatives..."
if [ "${#BONUS_VERSIONS[@]}" -gt 0 ]; then
    for version in "${BONUS_VERSIONS[@]}"; do
        sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-$version $version --slave /usr/bin/g++ g++ /usr/bin/g++-$version --slave /usr/bin/gcov gcov /usr/bin/gcov-$version
    done
fi

# Set up alternatives for the default version
sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-$DEFAULT_VERSION $DEFAULT_VERSION --slave /usr/bin/g++ g++ /usr/bin/g++-$DEFAULT_VERSION --slave /usr/bin/gcov gcov /usr/bin/gcov-$DEFAULT_VERSION

# Set the specified default GCC version
echo "Setting GCC $DEFAULT_VERSION as the default..."
sudo update-alternatives --set gcc /usr/bin/gcc-$DEFAULT_VERSION

# Verify the installed GCC and G++ versions
echo "Verifying GCC installation..."
gcc --version
g++ --version

echo "GCC setup complete!"
