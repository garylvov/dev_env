#!/bin/bash

partition="3090-gcondo"

echo "Scanning $partition for GPU availability..."
echo

# Declare associative arrays to track counts and nodes per GPU type
declare -A free8_count
declare -A nodes_by_type
node_infos=""

# Loop over all nodes in the partition
while read -r node; do
    info=$(scontrol show node "$node")

    # Total GPUs Slurm can allocate
    if [[ $info =~ CfgTRES=.*gres/gpu=([0-9]+) ]]; then
        total=${BASH_REMATCH[1]}
    else
        total=0
    fi

    # GPUs currently allocated
    if [[ $info =~ AllocTRES=.*gres/gpu=([0-9]+) ]]; then
        used=${BASH_REMATCH[1]}
    else
        used=0
    fi

    # Detect GPU type and hardware count
    if [[ $info =~ Gres=.*gpu:([^:]+):([0-9]+) ]]; then
        gpu_type=${BASH_REMATCH[1]}
        hw=${BASH_REMATCH[2]}
    else
        gpu_type="unknown"
        hw=$total
    fi

    free=$((total - used))
    state=$(echo "$info" | grep -oP '^State=\K\S+')

    # Track free nodes per type
    if (( free >= 8 )); then
        free8_count["$gpu_type"]=$((free8_count["$gpu_type"] + 1))
        nodes_by_type["$gpu_type"]+="$node "
    fi

    # Collect detailed node info
    node_infos+=$(printf "%3d\t%-10s (alloc=%d, used=%d, free=%d, hw=%d, type=%s, state=%s)" \
        "$free" "$node" "$total" "$used" "$free" "$hw" "$gpu_type" "$state")$'\n'

done < <(sinfo -p "$partition" -Nh -o "%N")

# Print summary
echo "Summary of nodes with at least 8 free GPUs:"
for type in "${!free8_count[@]}"; do
    printf "  %s: %d nodes -> %s\n" "$type" "${free8_count[$type]}" "${nodes_by_type[$type]}"
done
echo

# Print detailed node info, sorted by free GPUs descending
echo "$node_infos" | sort -nr

