# Compatibility shortcut for the bundled orange OSCAR banner.
(
    banner_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit
    source "$banner_dir/banner.bash" "$banner_dir/oscar.ans" OSCAR
)
