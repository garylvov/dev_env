#!/usr/bin/env bash
# Rank users by total jobs submitted, with max burst detection.
# Usage: bash rank_queue.bash [days_lookback] [partition] [window_minutes]
#   bash rank_queue.bash
#   bash rank_queue.bash 7 3090-gcondo
#   bash rank_queue.bash 120 3090-gcondo 10

DAYS=${1:-7}
PARTITION=${2:-}
WINDOW=${3:-10}
START_DATE=$(date -d "$DAYS days ago" +%Y-%m-%dT00:00:00)

SACCT_ARGS=(-S "$START_DATE" --allusers --noheader -X -o "User,Submit")
if [[ -n "$PARTITION" ]]; then
  SACCT_ARGS+=(--partition="$PARTITION")
fi

echo "Period    : last $DAYS days (since $START_DATE)"
[[ -n "$PARTITION" ]] && echo "Partition : $PARTITION"
echo "Window    : ${WINDOW}m"
echo ""

sacct "${SACCT_ARGS[@]}" | awk -v window="$WINDOW" '
NF >= 2 {
    gsub(/^ +| +$/, "", $1)
    gsub(/^ +| +$/, "", $2)
    user = $1
    ts = $2
    if (user == "" || ts == "") next

    total[user]++

    # parse "YYYY-MM-DDTHH:MM:SS" into a minute-resolution bucket
    split(ts, dt, "[T:-]")
    day = dt[3] + 0
    hr  = dt[4] + 0
    mn  = dt[5] + 0
    mins = day * 1440 + hr * 60 + mn
    bucket = int(mins / window)
    key = user SUBSEP bucket

    burst[key]++
    if (burst[key] > max_burst[user])
        max_burst[user] = burst[key]
}
END {
    printf "%-20s %8s %10s\n", "USER", "JOBS", "MAX_BURST"
    printf "%-20s %8s %10s\n", "----", "----", "---------"
    for (u in total)
        printf "%-20s %8d %10d\n", u, total[u], max_burst[u] | "sort -k3 -nr"
}
'
