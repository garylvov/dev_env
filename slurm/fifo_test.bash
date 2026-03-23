#!/usr/bin/env bash
# fifo_test.bash — Detect queue floods and measure FIFO similarity.
#
# 1. Finds the top burst episodes (users submitting many jobs in a window)
# 2. For each flood, grabs ALL jobs (all users) submitted in that period + aftermath
# 3. Computes Spearman rank correlation (submit order vs start order)
#    rho ~ 1.0 = effectively FIFO scheduling
#
# Usage: bash fifo_test.bash [days] [window_min] [aftermath_hrs] [top_n_floods]
#   bash fifo_test.bash              # 7 days, 10min window, 2hr aftermath, top 5
#   bash fifo_test.bash 30 10 4 3    # 30 days, 10min bursts, 4hr aftermath, top 3

set -uo pipefail

DAYS=${1:-7}
WINDOW=${2:-10}
AFTERMATH_HRS=${3:-2}
TOP_N=${4:-5}
START_DATE=$(date -d "$DAYS days ago" +%Y-%m-%dT00:00:00)

echo "=== Queue Flood FIFO Analysis ==="
echo "Period        : last $DAYS days (since $START_DATE)"
echo "Burst window  : ${WINDOW}m"
echo "Aftermath     : ${AFTERMATH_HRS}h after burst start"
echo "Top floods    : $TOP_N"
echo ""

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ─── Phase 1: Detect flood episodes ────────────────────────────────────
echo "--- Phase 1: Detecting flood episodes ---"

sacct -S "$START_DATE" --allusers --noheader -X -o "User%30,Submit" | awk -v window="$WINDOW" '
NF >= 2 {
    gsub(/^ +| +$/, "", $1)
    gsub(/^ +| +$/, "", $2)
    user = $1; ts = $2
    if (user == "" || ts == "") next

    split(ts, dt, "[T:-]")
    # Use full date for bucket to avoid cross-day collisions
    yr  = dt[1]+0; mo = dt[2]+0; dy = dt[3]+0
    hr  = dt[4]+0; mn = dt[5]+0
    abs_mins = ((yr*12+mo)*31+dy)*1440 + hr*60 + mn
    bucket = int(abs_mins / window)
    key = user SUBSEP bucket

    count[key]++
    if (!(key in first_ts)) first_ts[key] = ts
    last_ts[key] = ts
}
END {
    for (key in count) {
        split(key, parts, SUBSEP)
        printf "%d\t%s\t%s\t%s\n", count[key], parts[1], first_ts[key], last_ts[key]
    }
}' | sort -t$'\t' -k1 -nr | head -"$TOP_N" > "$TMP/floods.txt"

echo ""
printf "%-6s  %-20s  %-20s  %-20s\n" "JOBS" "USER" "BURST_START" "BURST_END"
printf "%-6s  %-20s  %-20s  %-20s\n" "----" "----" "-----------" "---------"
while IFS=$'\t' read -r cnt user bstart bend; do
    printf "%-6d  %-20s  %-20s  %-20s\n" "$cnt" "$user" "$bstart" "$bend"
done < "$TMP/floods.txt"
echo ""

# ─── Phase 2: FIFO analysis for each flood ─────────────────────────────
echo "--- Phase 2: FIFO similarity per flood episode ---"
echo ""

FLOOD_IDX=0
while IFS=$'\t' read -r burst_count flooder burst_start burst_end; do
    FLOOD_IDX=$((FLOOD_IDX + 1))

    # Compute window: from burst_start to burst_start + AFTERMATH_HRS
    window_start="$burst_start"
    # Add aftermath hours using date arithmetic
    # Parse burst_start and add hours
    window_end=$(date -d "${burst_start/T/ } ${AFTERMATH_HRS} hours" +%Y-%m-%dT%H:%M:%S)

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Flood #$FLOOD_IDX: $flooder submitted $burst_count jobs"
    echo "  Burst:   $burst_start → $burst_end"
    echo "  Window:  $window_start → $window_end"

    # Pull ALL jobs (all users) submitted in this window that actually started
    sacct -S "$window_start" -E "$window_end" --allusers --noheader -X \
        --format="JobIDRaw,User%30,Submit,Start" \
        --state=CD,TO,F,OOM,CA \
    | awk -v ws="$window_start" -v we="$window_end" '
    NF >= 4 {
        gsub(/^ +| +$/, "")
        jobid=$1; user=$2; submit=$3; start=$4
        if (start == "Unknown" || start == "None" || start ~ /^0001/) next
        if (submit == "" || start == "") next
        # Only jobs submitted within our window
        if (submit >= ws && submit <= we)
            print jobid, user, submit, start
    }' > "$TMP/flood_${FLOOD_IDX}.txt"

    njobs=$(wc -l < "$TMP/flood_${FLOOD_IDX}.txt")
    echo "  Jobs in window: $njobs"

    if (( njobs < 10 )); then
        echo "  (too few jobs to analyze)"
        echo ""
        continue
    fi

    # Rank by submit time, then by start time
    sort -k3,3 -k1,1n "$TMP/flood_${FLOOD_IDX}.txt" \
        | awk '{print NR, $0}' > "$TMP/f${FLOOD_IDX}_sub.txt"

    sort -k5,5 -k1,1n "$TMP/f${FLOOD_IDX}_sub.txt" \
        | awk '{print NR, $0}' > "$TMP/f${FLOOD_IDX}_both.txt"
    # Format: start_rank submit_rank jobid user submit start

    # Compute Spearman rho + per-user stats for this flood
    awk -v flooder="$flooder" '
    {
        sr  = $1  # start_rank
        sbr = $2  # submit_rank
        user = $4

        d = sr - sbr
        sum_d2 += d * d
        n++

        user_n[user]++
        ad = (d > 0 ? d : -d)
        user_sum_ad[user] += ad
    }
    END {
        rho = 1.0 - (6.0 * sum_d2) / (n * (n * n - 1.0))

        printf "\n"
        printf "  ┌────────────────────────────────────┐\n"
        printf "  │  Spearman rho = %-8.4f            │\n", rho
        if (rho > 0.9)
            printf "  │  >> STRONG FIFO behavior <<        │\n"
        else if (rho > 0.7)
            printf "  │  >> Moderate FIFO tendency <<       │\n"
        else
            printf "  │  >> Weak FIFO (priority active?) << │\n"
        printf "  └────────────────────────────────────┘\n"

        # Count flooder vs others
        flooder_jobs = user_n[flooder]+0
        other_jobs = n - flooder_jobs
        printf "\n  Flooder (%s): %d jobs", flooder, flooder_jobs
        if (flooder_jobs > 0)
            printf "  avg displacement=%.1f", user_sum_ad[flooder]/flooder_jobs
        printf "\n  Other users:       %d jobs", other_jobs

        # Average displacement for non-flooder users
        other_sum = 0; other_n = 0
        for (u in user_n) {
            if (u != flooder) {
                other_sum += user_sum_ad[u]
                other_n += user_n[u]
            }
        }
        if (other_n > 0)
            printf "  avg displacement=%.1f", other_sum/other_n
        printf "\n"

        # Top displaced users (non-flooder)
        printf "\n  Per-user breakdown:\n"
        printf "  %-20s %6s %12s\n", "USER", "JOBS", "AVG_|SHIFT|"
        printf "  %-20s %6s %12s\n", "----", "----", "----------"
        for (u in user_n) {
            avg = user_sum_ad[u] / user_n[u]
            marker = (u == flooder) ? " <-- flooder" : ""
            printf "  %-20s %6d %12.1f%s\n", u, user_n[u], avg, marker | "sort -k3 -nr"
        }
    }' "$TMP/f${FLOOD_IDX}_both.txt"

    echo ""

done < "$TMP/floods.txt"

echo ""
echo "=== How to Read Results ==="
echo "Spearman rho close to 1.0 = jobs start in nearly the same order they were submitted (FIFO)."
echo "If non-flooder users have high avg displacement, their jobs got pushed back by the flood."
echo "If rho > 0.9 consistently, the queue is effectively FIFO despite Slurm's priority system."
