#!/usr/bin/env bash
# fifo_test.bash — Detect queue floods and measure FIFO similarity.
#
# Finds top burst episodes, then for each one measures how closely
# Slurm scheduled jobs in FIFO order (Spearman rank correlation),
# along with condo GPU saturation, job durations, and queue depth.
#
# Usage:
#   bash fifo_test.bash [days] [window_min] [aftermath_hrs] [top_n] [gpu_limit] [partition] [min_avg_sec]
#
# Examples:
#   bash fifo_test.bash                          # 7d, 10m window, 2h aftermath, top 5
#   bash fifo_test.bash 30                       # last 30 days
#   bash fifo_test.bash 7 10 2 5 '' 3090-gcondo 60   # skip floods whose flooder avg-dur < 60s
#
# If gpu_limit is empty or unset, it is auto-detected from Slurm's QOS GrpTRES
# for any QOS whose name contains the partition name (e.g. cs-3090-gcondo).
#
# Pass -v as first arg for verbose per-user breakdowns:
#   bash fifo_test.bash -v 7 10 2 3

set -uo pipefail

VERBOSE=0
if [[ "${1:-}" == "-v" ]]; then
    VERBOSE=1
    shift
fi

DAYS=${1:-7}
WINDOW=${2:-10}
AFTERMATH_HRS=${3:-2}
TOP_N=${4:-5}
GRP_GPU_LIMIT=${5:-}
CONDO_PARTITION=${6:-3090-gcondo}
MIN_AVG_SEC=${7:-0}
START_DATE=$(date -d "$DAYS days ago" +%Y-%m-%dT00:00:00)

# Auto-detect group GPU limit from Slurm if not provided
if [[ -z "$GRP_GPU_LIMIT" ]]; then
    GRP_GPU_LIMIT=$(sacctmgr show qos format=Name,GrpTRES -np 2>/dev/null \
        | awk -F'|' -v p="$CONDO_PARTITION" '
            tolower($1) ~ tolower(p) && $2 ~ /gres\/gpu=/ {
                if (match($2, /gres\/gpu=[0-9]+/)) {
                    s = substr($2, RSTART, RLENGTH); split(s, a, "=")
                    if (a[2] + 0 > max) { max = a[2] + 0; qos = $1 }
                }
            }
            END { if (max > 0) printf "%d\t%s\n", max, qos }')
    if [[ -n "$GRP_GPU_LIMIT" ]]; then
        auto_qos=$(echo "$GRP_GPU_LIMIT" | cut -f2)
        GRP_GPU_LIMIT=$(echo "$GRP_GPU_LIMIT" | cut -f1)
        echo "[Auto-detected group GPU limit for $CONDO_PARTITION: $GRP_GPU_LIMIT (from QOS '$auto_qos')]"
    else
        GRP_GPU_LIMIT=0
        echo "[WARN: no matching QOS found for partition $CONDO_PARTITION — CONDO_GPUs % will show N/A]"
    fi
fi

# Helper: resolve username -> "Full Name (email)"
resolve_user() {
    local uname="$1"
    local gecos
    gecos=$(getent passwd "$uname" 2>/dev/null | cut -d: -f5)
    if [[ -n "$gecos" ]]; then
        local fullname
        fullname=$(echo "$gecos" | sed 's/@.*//' | tr '_' ' ' | sed 's/\b\(.\)/\u\1/g')
        echo "$fullname ($gecos)"
    else
        echo "$uname"
    fi
}

# Helper: resolve username -> just "Full Name"
resolve_name() {
    local uname="$1"
    local gecos
    gecos=$(getent passwd "$uname" 2>/dev/null | cut -d: -f5)
    if [[ -n "$gecos" ]]; then
        echo "$gecos" | sed 's/@.*//' | tr '_' ' ' | sed 's/\b\(.\)/\u\1/g'
    else
        echo "$uname"
    fi
}

echo "=== Queue Flood FIFO Analysis ==="
echo "Period: last ${DAYS}d | Burst window: ${WINDOW}m | Aftermath: ${AFTERMATH_HRS}h | Condo: $CONDO_PARTITION (limit=${GRP_GPU_LIMIT} GPUs) | Min flooder avg dur: ${MIN_AVG_SEC}s"
echo ""

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ─── Phase 1: Detect flood episodes ────────────────────────────────────
sacct -S "$START_DATE" --allusers --noheader -X -o "User%30,Submit" | awk -v window="$WINDOW" '
NF >= 2 {
    gsub(/^ +| +$/, "", $1)
    gsub(/^ +| +$/, "", $2)
    user = $1; ts = $2
    if (user == "" || ts == "") next

    split(ts, dt, "[T:-]")
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
}' | sort -t$'\t' -k1 -nr | head -$((TOP_N * 10)) > "$TMP/floods.txt"

# ─── Phase 2: Analyze each flood, collect results ──────────────────────
# We collect one TSV row per flood into results.txt, then print a table at the end.

FLOOD_IDX=0
PASS_COUNT=0
while IFS=$'\t' read -r burst_count flooder burst_start burst_end; do
    if (( PASS_COUNT >= TOP_N )); then break; fi
    FLOOD_IDX=$((FLOOD_IDX + 1))

    window_start="$burst_start"
    window_end=$(date -d "${burst_start/T/ } ${AFTERMATH_HRS} hours" +%Y-%m-%dT%H:%M:%S)
    flood_epoch=$(date -d "${burst_start/T/ }" +%s)
    pre_flood=$(date -d "@$(( flood_epoch - 86400 ))" +%Y-%m-%dT%H:%M:%S)

    flooder_name=$(resolve_name "$flooder")

    # ── Condo GPU utilization at flood start ─────────────────────────
    read -r condo_gpus condo_jobs all_gpus all_jobs < <(
        sacct -S "$pre_flood" -E "$burst_start" --allusers --noheader -X \
            --format="JobIDRaw,User%30,Start,End,AllocTRES%80,Partition%20" \
            --state=CD,TO,F,OOM,CA,R \
        | awk -v flood_t="$burst_start" -v condo="$CONDO_PARTITION" '
        {
            gsub(/^ +| +$/, "")
            start=$3; end=$4; tres=$5; partition=$6
            if (start == "Unknown" || start == "None" || start ~ /^0001/) next
            if (start > flood_t) next
            if (end != "Unknown" && end != "None" && end < flood_t) next

            gpus = 0
            n = split(tres, fields, ",")
            for (i = 1; i <= n; i++) {
                if (fields[i] ~ /gres\/gpu=/) {
                    split(fields[i], gp, "=")
                    gpus = gp[2] + 0
                }
            }
            all_gpus += gpus; all_jobs++
            if (partition == condo) { condo_gpus += gpus; condo_jobs++ }
        }
        END { printf "%d %d %d %d\n", condo_gpus+0, condo_jobs+0, all_gpus+0, all_jobs+0 }'
    )

    condo_pct=$(( condo_gpus * 100 / (GRP_GPU_LIMIT > 0 ? GRP_GPU_LIMIT : 1) ))

    # ── Pending jobs at flood start ────────────────────────────────
    pending=$(sacct -S "$pre_flood" -E "$window_end" --allusers --noheader -X \
        --format="JobIDRaw,Submit,Start" \
        --state=CD,TO,F,OOM,CA \
    | awk -v flood_t="$burst_start" '
    {
        gsub(/^ +| +$/, "")
        submit=$2; start=$3
        if (start == "Unknown" || start == "None") next
        if (submit <= flood_t && start > flood_t) count++
    }
    END { print count+0 }')

    # ── Pull ALL jobs in window for FIFO + duration analysis ───────
    sacct -S "$window_start" -E "$window_end" --allusers --noheader -X \
        --format="JobIDRaw,User%30,Submit,Start,Elapsed,Partition%20,AllocTRES%100,Priority" \
        --state=CD,TO,F,OOM,CA \
    | awk -v ws="$window_start" -v we="$window_end" '
    NF >= 5 {
        gsub(/^ +| +$/, "")
        jobid=$1; user=$2; submit=$3; start=$4; elapsed=$5; partition=$6; tres=$7; prio=$8+0
        if (start == "Unknown" || start == "None" || start ~ /^0001/) next
        if (submit == "" || start == "") next
        gpus = 0
        if (match(tres, /gres\/gpu=[0-9]+/)) {
            s = substr(tres, RSTART, RLENGTH); split(s, a, "="); gpus = a[2] + 0
        }
        if (submit >= ws && submit <= we)
            print jobid, user, submit, start, elapsed, partition, gpus, prio
    }' > "$TMP/flood_${FLOOD_IDX}.txt"

    njobs=$(wc -l < "$TMP/flood_${FLOOD_IDX}.txt")

    if (( njobs < 10 )); then
        printf "%d\t%s\t%s\t%s\t%d\t%d/%d (%d%%)\t%d\t%d\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\n" \
            "$FLOOD_IDX" "$flooder" "$flooder_name" "$burst_start" "$burst_count" \
            "$condo_gpus" "$GRP_GPU_LIMIT" "$condo_pct" "$pending" "$njobs" \
            >> "$TMP/results.txt"
        PASS_COUNT=$((PASS_COUNT + 1))
        continue
    fi

    # ── Job duration stats ─────────────────────────────────────────
    read -r fl_med ot_med fl_mean ot_mean fl_mean_secs ot_mean_secs < <(
        awk -v flooder="$flooder" '
        function parse_elapsed(s) {
            gsub(/-/, ":", s)
            n = split(s, parts, ":")
            if (n == 4) return (parts[1]*86400 + parts[2]*3600 + parts[3]*60 + parts[4])
            if (n == 3) return (parts[1]*3600 + parts[2]*60 + parts[3])
            if (n == 2) return (parts[1]*60 + parts[2])
            return 0
        }
        function fmt(secs) {
            if (secs < 60) return sprintf("%ds", secs)
            if (secs < 3600) return sprintf("%dm%ds", secs/60, secs%60)
            if (secs < 86400) return sprintf("%dh%dm", secs/3600, (secs%3600)/60)
            return sprintf("%dd%dh", secs/86400, (secs%86400)/3600)
        }
        {
            secs = parse_elapsed($5)
            if ($2 == flooder) { fl_n++; fl_dur[fl_n] = secs; fl_sum += secs }
            else               { ot_n++; ot_dur[ot_n] = secs; ot_sum += secs }
        }
        END {
            # insertion sort for medians
            for (i = 2; i <= fl_n; i++) { v=fl_dur[i]; j=i-1; while(j>=1 && fl_dur[j]>v){fl_dur[j+1]=fl_dur[j];j--}; fl_dur[j+1]=v }
            for (i = 2; i <= ot_n; i++) { v=ot_dur[i]; j=i-1; while(j>=1 && ot_dur[j]>v){ot_dur[j+1]=ot_dur[j];j--}; ot_dur[j+1]=v }
            fl_med = (fl_n>0) ? ((fl_n%2==1) ? fl_dur[int(fl_n/2)+1] : (fl_dur[fl_n/2]+fl_dur[fl_n/2+1])/2) : 0
            ot_med = (ot_n>0) ? ((ot_n%2==1) ? ot_dur[int(ot_n/2)+1] : (ot_dur[ot_n/2]+ot_dur[ot_n/2+1])/2) : 0
            fl_mean = (fl_n>0) ? fl_sum/fl_n : 0
            ot_mean = (ot_n>0) ? ot_sum/ot_n : 0
            printf "%s %s %s %s %d %d\n", fmt(fl_med), fmt(ot_med), fmt(fl_mean), fmt(ot_mean), fl_mean, ot_mean
        }' "$TMP/flood_${FLOOD_IDX}.txt"
    )

    # ── Min flooder avg duration filter ────────────────────────────
    if (( MIN_AVG_SEC > 0 && fl_mean_secs < MIN_AVG_SEC )); then
        echo "[Skip flood $FLOOD_IDX ($flooder @ $burst_start): flooder avg=${fl_mean_secs}s < threshold=${MIN_AVG_SEC}s]"
        continue
    fi

    # ── Spearman rho + displacement ───────────────────────────────
    sort -k3,3 -k1,1n "$TMP/flood_${FLOOD_IDX}.txt" \
        | awk '{print NR, $0}' > "$TMP/f${FLOOD_IDX}_sub.txt"

    sort -k5,5 -k1,1n "$TMP/f${FLOOD_IDX}_sub.txt" \
        | awk '{print NR, $0}' > "$TMP/f${FLOOD_IDX}_both.txt"

    read -r rho fl_disp ot_disp < <(
        awk -v flooder="$flooder" '
        {
            d = $1 - $2  # start_rank - submit_rank
            sum_d2 += d * d; n++
            ad = (d > 0 ? d : -d)
            user_n[$4]++; user_sum_ad[$4] += ad
        }
        END {
            rho = 1.0 - (6.0 * sum_d2) / (n * (n * n - 1.0))
            fl_disp = (user_n[flooder] > 0) ? user_sum_ad[flooder] / user_n[flooder] : 0
            ot_sum = 0; ot_n = 0
            for (u in user_n) {
                if (u != flooder) { ot_sum += user_sum_ad[u]; ot_n += user_n[u] }
            }
            ot_disp = (ot_n > 0) ? ot_sum / ot_n : 0
            printf "%.4f %.1f %.1f\n", rho, fl_disp, ot_disp
        }' "$TMP/f${FLOOD_IDX}_both.txt"
    )

    # ── Jumps analysis: did non-flooder jobs with same (partition, gpu)
    #    shape as the flooder start *before* flooder's already-pending jobs?
    #    Also compute LONG_* subset: restrict competitors to elapsed >=
    #    flooder's dominant-shape median — jumps there cannot be explained
    #    by backfill and thus indicate real fairshare priority reordering. ──
    read -r ref_shape ot_match avg_jumps pct_jumped max_jumps long_n long_avg long_pct < <(
        awk -v flooder="$flooder" '
        function parse_elapsed(s,   n, parts) {
            gsub(/-/, ":", s)
            n = split(s, parts, ":")
            if (n == 4) return (parts[1]*86400 + parts[2]*3600 + parts[3]*60 + parts[4])
            if (n == 3) return (parts[1]*3600 + parts[2]*60 + parts[3])
            if (n == 2) return (parts[1]*60 + parts[2])
            return 0
        }
        {
            submit=$3; start=$4; partition=$6; gpus=$7
            dur = parse_elapsed($5)
            if ($2 == flooder) {
                fl_n++; fl_sub[fl_n]=submit; fl_sta[fl_n]=start
                fl_part[fl_n]=partition; fl_gpu[fl_n]=gpus; fl_dur[fl_n]=dur
                shape_count[partition SUBSEP gpus]++
            } else {
                ot_n++; ot_sub[ot_n]=submit; ot_sta[ot_n]=start
                ot_part[ot_n]=partition; ot_gpu[ot_n]=gpus; ot_dur[ot_n]=dur
            }
        }
        END {
            max = 0; ref_part = "-"; ref_gpu = "-"
            for (k in shape_count) {
                if (shape_count[k] > max) {
                    max = shape_count[k]
                    split(k, p, SUBSEP); ref_part = p[1]; ref_gpu = p[2]
                }
            }

            # Flooder median elapsed restricted to dominant shape
            dom_n = 0
            for (j = 1; j <= fl_n; j++) {
                if (fl_part[j] == ref_part && fl_gpu[j] == ref_gpu) {
                    dom_n++; dom_dur[dom_n] = fl_dur[j]
                }
            }
            for (i = 2; i <= dom_n; i++) {
                v=dom_dur[i]; k=i-1
                while (k >= 1 && dom_dur[k] > v) { dom_dur[k+1]=dom_dur[k]; k-- }
                dom_dur[k+1]=v
            }
            dom_med = (dom_n > 0) ? ((dom_n % 2 == 1) ? dom_dur[int(dom_n/2)+1] : (dom_dur[dom_n/2]+dom_dur[dom_n/2+1])/2) : 0

            matched = 0; total_jumps = 0; any_jumped = 0; max_j = 0
            long_matched = 0; long_total_jumps = 0; long_any_jumped = 0
            for (i = 1; i <= ot_n; i++) {
                if (ot_part[i] != ref_part || ot_gpu[i] != ref_gpu) continue
                matched++
                jumps = 0
                for (j = 1; j <= fl_n; j++) {
                    if (fl_part[j] != ref_part || fl_gpu[j] != ref_gpu) continue
                    if (fl_sub[j] < ot_sub[i] && fl_sta[j] > ot_sta[i]) jumps++
                }
                total_jumps += jumps
                if (jumps > 0) any_jumped++
                if (jumps > max_j) max_j = jumps
                if (ot_dur[i] >= dom_med) {
                    long_matched++
                    long_total_jumps += jumps
                    if (jumps > 0) long_any_jumped++
                }
            }
            avg_j = (matched > 0) ? total_jumps / matched : 0
            pct_j = (matched > 0) ? 100.0 * any_jumped / matched : 0
            long_avg_j = (long_matched > 0) ? long_total_jumps / long_matched : 0
            long_pct_j = (long_matched > 0) ? 100.0 * long_any_jumped / long_matched : 0
            printf "%s:%s %d %.1f %.1f %d %d %.1f %.1f\n",
                ref_part, ref_gpu, matched, avg_j, pct_j, max_j,
                long_matched, long_avg_j, long_pct_j
        }' "$TMP/flood_${FLOOD_IDX}.txt"
    )

    # ── Priority-decay breakdown: finer bins + priority values ────────────
    # NORM% = sum(jumps) / sum(pending-at-submit) per bin — unbiased by drain.
    # FL_PRIO = avg Slurm Priority of flooder's dominant-shape jobs STARTING
    #           in this bin (shows age-accumulated priority at dispatch).
    # OT_PRIO = avg Slurm Priority of competitor jobs starting in this bin.
    # DIFF    = FL_PRIO - OT_PRIO. If DIFF > 0 in late bins, it confirms
    #           flooder-old-pending-jobs outprioritized competitors — the
    #           mechanism of the "queue-flooding-rewarded-by-aging" bug.
    ref_part="${ref_shape%:*}"
    ref_gpu="${ref_shape##*:}"
    awk -v flooder="$flooder" -v burst_start="$burst_start" \
        -v ref_part="$ref_part" -v ref_gpu="$ref_gpu" \
        -v raw_out="$TMP/aggraw_${FLOOD_IDX}.txt" '
    function iso_mins(s,   p) {
        split(s, p, /[T:-]/)
        return ((p[1]*12 + p[2]) * 31 + p[3]) * 1440 + p[4] * 60 + p[5]
    }
    function time_bin(t) {
        if (t < 15)  return 1
        if (t < 30)  return 2
        if (t < 45)  return 3
        if (t < 60)  return 4
        if (t < 90)  return 5
        if (t < 120) return 6
        if (t < 180) return 7
        return 8
    }
    BEGIN {
        burst_m = iso_mins(burst_start)
        split("0-15m 15-30m 30-45m 45-60m 60-90m 90-120m 120-180m 180m+", bin_label, " ")
        n_bins = 8
    }
    {
        submit=$3; start=$4; partition=$6; gpus=$7; prio=$8+0
        if ($2 == flooder) {
            fl_n++; fl_sub[fl_n]=submit; fl_sta[fl_n]=start
            fl_part[fl_n]=partition; fl_gpu[fl_n]=gpus; fl_prio[fl_n]=prio
        } else {
            ot_n++; ot_sub[ot_n]=submit; ot_sta[ot_n]=start
            ot_part[ot_n]=partition; ot_gpu[ot_n]=gpus; ot_prio[ot_n]=prio
        }
    }
    END {
        # Competitor-side binning (by competitor start time)
        for (i = 1; i <= ot_n; i++) {
            if (ot_part[i] != ref_part || ot_gpu[i] != ref_gpu) continue
            jumps = 0; pending = 0
            for (j = 1; j <= fl_n; j++) {
                if (fl_part[j] != ref_part || fl_gpu[j] != ref_gpu) continue
                if (fl_sub[j] < ot_sub[i] && fl_sta[j] > ot_sub[i]) pending++
                if (fl_sub[j] < ot_sub[i] && fl_sta[j] > ot_sta[i]) jumps++
            }
            t = iso_mins(ot_sta[i]) - burst_m
            bin = time_bin(t)
            bin_n[bin]++
            bin_jumps[bin] += jumps
            bin_pending[bin] += pending
            if (jumps > 0) bin_any[bin]++
            bin_ot_prio_sum[bin] += ot_prio[i]
            bin_ot_prio_n[bin]++
        }
        # Flooder-side binning (by flooder start time) — same shape only
        for (j = 1; j <= fl_n; j++) {
            if (fl_part[j] != ref_part || fl_gpu[j] != ref_gpu) continue
            t = iso_mins(fl_sta[j]) - burst_m
            bin = time_bin(t)
            bin_fl_starts[bin]++
            bin_fl_prio_sum[bin] += fl_prio[j]
            bin_fl_prio_n[bin]++
        }
        for (b = 1; b <= n_bins; b++) {
            n = bin_n[b] + 0
            avg = (n > 0) ? bin_jumps[b]/n : 0
            pct = (n > 0) ? 100.0 * bin_any[b]/n : 0
            norm = (bin_pending[b] > 0) ? 100.0 * bin_jumps[b]/bin_pending[b] : 0
            fl_p = (bin_fl_prio_n[b] > 0) ? bin_fl_prio_sum[b]/bin_fl_prio_n[b] : 0
            ot_p = (bin_ot_prio_n[b] > 0) ? bin_ot_prio_sum[b]/bin_ot_prio_n[b] : 0
            printf "  %-10s %6d %9.1f %8.1f %10d %10d\n",
                bin_label[b], n, norm, pct, fl_p, ot_p
            # Machine-readable line for aggregation
            printf "%d\t%s\t%d\t%d\t%d\t%d\t%.0f\t%d\t%.0f\t%d\t%d\n",
                b, bin_label[b],
                bin_n[b]+0, bin_jumps[b]+0, bin_pending[b]+0, bin_any[b]+0,
                bin_fl_prio_sum[b]+0, bin_fl_prio_n[b]+0,
                bin_ot_prio_sum[b]+0, bin_ot_prio_n[b]+0,
                bin_fl_starts[b]+0 >> raw_out
        }
    }' "$TMP/flood_${FLOOD_IDX}.txt" > "$TMP/decay_${FLOOD_IDX}.txt"

    # ── 8-GPU competitor analysis: wider (24h) window, same partition ─────
    # 8-GPU jobs take a full node → cannot be backfilled into small gaps.
    # Jumps by 8-GPU competitors over flooder-pending jobs (of any shape,
    # same partition) are the cleanest backfill-proof fairshare signal.
    # Use 24h window since 8-GPU submissions are rare — 2h rarely catches any.
    gpu8_end=$(date -d "${burst_start/T/ } 24 hours" +%Y-%m-%dT%H:%M:%S)
    sacct -S "$burst_start" -E "$gpu8_end" --allusers --noheader -X \
        --format="JobIDRaw,User%30,Submit,Start,Partition%20,AllocTRES%100" \
        --state=CD,TO,F,OOM,CA 2>/dev/null \
    | awk -v flooder="$flooder" -v ref_part="$ref_part" -v ws="$burst_start" -v we="$gpu8_end" '
    NF >= 5 {
        gsub(/^ +| +$/, "")
        user=$2; submit=$3; start=$4; partition=$5; tres=$6
        if (start == "Unknown" || start == "None" || start ~ /^0001/) next
        if (submit == "" || start == "") next
        if (partition != ref_part) next
        if (submit < ws || submit > we) next
        gpus = 0
        if (match(tres, /gres\/gpu=[0-9]+/)) {
            s = substr(tres, RSTART, RLENGTH); split(s, a, "="); gpus = a[2] + 0
        }
        if (user == flooder) {
            fl_n++; fl_sub[fl_n]=submit; fl_sta[fl_n]=start
        } else if (gpus == 8) {
            ot_n++; ot_sub[ot_n]=submit; ot_sta[ot_n]=start; ot_user[ot_n]=user
        }
    }
    END {
        total_jumps = 0; total_pending = 0; any_jumped = 0; max_j = 0
        for (i = 1; i <= ot_n; i++) {
            jumps = 0; pending = 0
            for (j = 1; j <= fl_n; j++) {
                if (fl_sub[j] < ot_sub[i] && fl_sta[j] > ot_sub[i]) pending++
                if (fl_sub[j] < ot_sub[i] && fl_sta[j] > ot_sta[i]) jumps++
            }
            total_jumps += jumps
            total_pending += pending
            if (jumps > 0) any_jumped++
            if (jumps > max_j) max_j = jumps
        }
        avg_j = (ot_n > 0) ? total_jumps / ot_n : 0
        pct_j = (ot_n > 0) ? 100.0 * any_jumped / ot_n : 0
        norm = (total_pending > 0) ? 100.0 * total_jumps / total_pending : 0
        printf "%d %d %.1f %.1f %.1f %d\n", ot_n, total_pending, avg_j, pct_j, norm, max_j
    }' > "$TMP/gpu8_${FLOOD_IDX}.txt"

    # Save result row
    printf "%d\t%s\t%s\t%s\t%d\t%d/%d (%d%%)\t%d\t%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$FLOOD_IDX" "$flooder" "$flooder_name" "$burst_start" "$burst_count" \
        "$condo_gpus" "$GRP_GPU_LIMIT" "$condo_pct" "$pending" "$njobs" \
        "$rho" "$fl_disp" "$ot_disp" "$fl_med" "$ot_med" "$fl_mean" "$ot_mean" \
        "$ref_shape" "$ot_match" "$avg_jumps" "$pct_jumped" "$max_jumps" \
        "$long_n" "$long_avg" "$long_pct" \
        >> "$TMP/results.txt"
    PASS_COUNT=$((PASS_COUNT + 1))

    # ── Verbose per-user breakdown (optional) ─────────────────────
    if (( VERBOSE )); then
        echo ""
        echo "━━━ Flood #$FLOOD_IDX: $flooder ($flooder_name) — $burst_count jobs at $burst_start ━━━"
        echo "  Condo: ${condo_gpus}/${GRP_GPU_LIMIT} GPUs (${condo_pct}%) | Pending: $pending | rho=$rho"
        echo ""
        echo "  Per-user displacement:"
        printf "  %-20s %6s %12s\n" "USER" "JOBS" "AVG_|SHIFT|"
        printf "  %-20s %6s %12s\n" "----" "----" "----------"
        awk -v flooder="$flooder" '
        {
            user=$4; d=$1-$2; ad=(d>0?d:-d)
            user_n[user]++; user_sum_ad[user]+=ad
        }
        END {
            for (u in user_n) {
                avg = user_sum_ad[u]/user_n[u]
                marker = (u == flooder) ? " <-- flooder" : ""
                printf "  %-20s %6d %12.1f%s\n", u, user_n[u], avg, marker | "sort -k3 -nr"
            }
        }' "$TMP/f${FLOOD_IDX}_both.txt"
        echo ""
    fi

done < "$TMP/floods.txt"

# ─── Print summary table ──────────────────────────────────────────────
echo ""
echo "=== Summary Table ==="
echo ""

# Header
printf "%-3s  %-12s %-18s  %-11s  %5s  %-16s  %7s  %5s  "  "#" "USER" "NAME" "WHEN" "BURST" "CONDO_GPUs" "PENDING" "TOTAL"
printf "%8s  %8s  %8s  %10s %10s  %10s %10s  "              "SPEAR_r" "FL_DISP" "OT_DISP" "FL_MED_DUR" "OT_MED_DUR" "FL_AVG_DUR" "OT_AVG_DUR"
printf "%-18s %7s %9s %9s %7s  %7s %9s %9s\n"                "SHAPE(part:gpu)" "OT_SUB" "AVG_JUMPS" "PCT_JUMPD" "MAX_JMP" "LONG_N" "LONG_AVG" "LONG_PCT"

printf "%-3s  %-12s %-18s  %-11s  %5s  %-16s  %7s  %5s  "  "---" "----" "----" "----" "-----" "----------" "-------" "-----"
printf "%8s  %8s  %8s  %10s %10s  %10s %10s  "              "-------" "-------" "-------" "----------" "----------" "----------" "----------"
printf "%-18s %7s %9s %9s %7s  %7s %9s %9s\n"                "---------------" "------" "---------" "---------" "-------" "------" "--------" "--------"

display_idx=0
while IFS=$'\t' read -r idx user name when burst gpuinfo pend total rho fl_d ot_d fl_med ot_med fl_mean ot_mean shape ot_sub avg_j pct_j max_j long_n long_avg long_pct; do
    display_idx=$((display_idx + 1))
    # Trim the date to just MM-DD HH:MM
    short_when=$(echo "$when" | sed 's/^[0-9]*-//; s/T/ /; s/:[0-9]*$//')
    printf "%-3s  %-12s %-18s  %-11s  %5d  %-16s  %7d  %5d  " \
        "$display_idx" "$user" "$name" "$short_when" "$burst" "$gpuinfo" "$pend" "$total"
    printf "%8s  %8s  %8s  %10s %10s  %10s %10s  " \
        "$rho" "$fl_d" "$ot_d" "$fl_med" "$ot_med" "$fl_mean" "$ot_mean"
    printf "%-18s %7s %9s %9s %7s  %7s %9s %9s\n" \
        "$shape" "$ot_sub" "$avg_j" "$pct_j" "$max_j" "$long_n" "$long_avg" "$long_pct"
done < "$TMP/results.txt"

echo ""
echo "KEY: SPEAR_r = Spearman rank correlation (1.0 = perfect FIFO)"
echo "     FL_DISP / OT_DISP = avg rank displacement for flooder / other users"
echo "     FL_MED_DUR / OT_MED_DUR = median job duration for flooder / others"
echo "     CONDO_GPUs = GPUs in use on $CONDO_PARTITION at flood start (% of $GRP_GPU_LIMIT limit)"
echo ""
echo "  SHAPE      = flooder's dominant (partition, GPU count) — the apples-to-apples cohort"
echo "  OT_SUB     = non-flooder jobs in window with the same shape (sample size)"
echo "  AVG_JUMPS  = mean # of flooder's earlier-submitted-but-still-pending jobs each non-flooder job beat to start"
echo "  PCT_JUMPD  = % of OT_SUB jobs that jumped >= 1 flooder-pending job"
echo "  MAX_JMP    = max jumps by any single non-flooder job"
echo "  LONG_N     = subset of OT_SUB with elapsed >= flooder's dominant-shape median (backfill-resistant cohort)"
echo "  LONG_AVG   = avg jumps among LONG_N competitors"
echo "  LONG_PCT   = % of LONG_N competitors that jumped >= 1"
echo ""
echo "Interpretation:"
echo "  AVG_JUMPS ~ 0 and PCT_JUMPD ~ 0   => strict FIFO; non-flooder jobs waited their turn"
echo "  AVG_JUMPS high but LONG_AVG ~ 0   => jumps are from backfill (short competitors filling gaps)"
echo "  LONG_AVG high and LONG_PCT high   => real fairshare: long competitors leapfrogged despite being backfill-unfriendly"

# ─── Priority decay breakdown per flood ────────────────────────────
echo ""
echo "=== Priority Decay Analysis: competitor jumps binned by (start − burst_start) ==="
echo "    NORM%   = sum(jumps) / sum(pending-at-submit) per bin — unbiased by backlog drain."
echo "    FL_PRIO = avg Slurm Priority of flooder's same-shape jobs that STARTED in this bin."
echo "    OT_PRIO = avg Slurm Priority of same-shape competitors that started in this bin."
echo ""
echo "    If aging is rewarding flooding: in late bins, FL_PRIO catches up to or exceeds OT_PRIO"
echo "    as flooder's OLD pending jobs accumulate age-priority that overcomes fairshare penalty."
echo ""
display_idx=0
while IFS=$'\t' read -r fidx user name when burst rest; do
    display_idx=$((display_idx + 1))
    if [[ -f "$TMP/decay_$fidx.txt" && -s "$TMP/decay_$fidx.txt" ]]; then
        short_when=$(echo "$when" | sed 's/^[0-9]*-//; s/T/ /; s/:[0-9]*$//')
        echo "Flood #$display_idx  ($user, $name, $short_when):"
        printf "  %-10s %6s %9s %8s %10s %10s\n" "BIN" "N" "NORM%" "PCT_JMP" "FL_PRIO" "OT_PRIO"
        printf "  %-10s %6s %9s %8s %10s %10s\n" "---" "--" "-----" "-------" "-------" "-------"
        cat "$TMP/decay_$fidx.txt"
        echo ""
    fi
done < "$TMP/results.txt"

# ─── Aggregate decay curve across all floods ────────────────────────
echo ""
echo "=== AGGREGATE Decay Curve (all floods combined) — the money chart ==="
echo ""
echo "  Proves the mechanism: NORM% decays as bins get later (fairshare reach is time-limited)."
echo "  Compare FL_PRIO vs OT_PRIO columns: if flooder's priority meets/exceeds competitor's"
echo "  in late bins, aging has offset fairshare penalty — rewarding queue-flooding."
echo ""
printf "  %-10s %8s %10s %10s %10s %10s %11s %9s\n" \
    "BIN" "COMP_N" "NORM%" "PCT_JUMPD" "FL_PRIO" "OT_PRIO" "PRIO_DIFF" "FL_START"
printf "  %-10s %8s %10s %10s %10s %10s %11s %9s\n" \
    "---" "------" "-----" "---------" "-------" "-------" "---------" "--------"
cat "$TMP"/aggraw_*.txt 2>/dev/null | awk '
BEGIN {
    split("0-15m 15-30m 30-45m 45-60m 60-90m 90-120m 120-180m 180m+", labels, " ")
}
{
    b=$1
    sum_n[b]           += $3
    sum_jumps[b]       += $4
    sum_pending[b]     += $5
    sum_any[b]         += $6
    sum_fl_prio_s[b]   += $7
    sum_fl_prio_n[b]   += $8
    sum_ot_prio_s[b]   += $9
    sum_ot_prio_n[b]   += $10
    sum_fl_starts[b]   += $11
}
END {
    for (b = 1; b <= 8; b++) {
        n = sum_n[b]+0
        norm = (sum_pending[b] > 0) ? 100.0 * sum_jumps[b]/sum_pending[b] : 0
        pct = (n > 0) ? 100.0 * sum_any[b]/n : 0
        fl_p = (sum_fl_prio_n[b] > 0) ? sum_fl_prio_s[b]/sum_fl_prio_n[b] : 0
        ot_p = (sum_ot_prio_n[b] > 0) ? sum_ot_prio_s[b]/sum_ot_prio_n[b] : 0
        diff = fl_p - ot_p
        printf "  %-10s %8d %10.1f %10.1f %10.0f %10.0f %+11.0f %9d\n",
            labels[b], n, norm, pct, fl_p, ot_p, diff, sum_fl_starts[b]+0
    }
}'

# ─── 8-GPU competitor analysis ─────────────────────────────────────
echo ""
echo "=== 8-GPU Competitor Analysis (same-partition, any flooder shape, 24h window) ==="
echo "    8-GPU jobs occupy whole nodes — cannot be backfilled into small gaps."
echo "    Jumps by 8-GPU competitors over flooder-pending jobs are the cleanest"
echo "    possible fairshare signal available from sacct history."
echo "    Window widened to 24h here (vs 2h main analysis) because 8-GPU submissions"
echo "    are rare and would otherwise produce N=0 everywhere."
echo ""
printf "%-3s  %-12s %-18s  %-11s  %-13s  %6s  %9s  %8s  %8s  %8s  %8s\n" \
    "#" "USER" "NAME" "WHEN" "PARTITION" "GPU8_N" "FL_PEND" "AVG_JMP" "PCT_JMP" "NORM%" "MAX_JMP"
printf "%-3s  %-12s %-18s  %-11s  %-13s  %6s  %9s  %8s  %8s  %8s  %8s\n" \
    "---" "----" "----" "----" "---------" "------" "-------" "-------" "-------" "-----" "-------"
display_idx=0
while IFS=$'\t' read -r fidx user name when burst gpuinfo pend total rho fl_d ot_d fl_med ot_med fl_mean ot_mean shape rest; do
    display_idx=$((display_idx + 1))
    if [[ -f "$TMP/gpu8_$fidx.txt" && -s "$TMP/gpu8_$fidx.txt" ]]; then
        read -r g8_n g8_pending g8_avg g8_pct g8_norm g8_max < "$TMP/gpu8_$fidx.txt"
        short_when=$(echo "$when" | sed 's/^[0-9]*-//; s/T/ /; s/:[0-9]*$//')
        part="${shape%:*}"
        if [[ "$g8_n" == "0" ]]; then
            printf "%-3s  %-12s %-18s  %-11s  %-13s  %6s  %9s  %8s  %8s  %8s  %8s\n" \
                "$display_idx" "$user" "$name" "$short_when" "$part" "0" "-" "-" "-" "-" "-"
        else
            printf "%-3s  %-12s %-18s  %-11s  %-13s  %6s  %9s  %8s  %8s  %8s  %8s\n" \
                "$display_idx" "$user" "$name" "$short_when" "$part" "$g8_n" "$g8_pending" "$g8_avg" "$g8_pct" "$g8_norm" "$g8_max"
        fi
    fi
done < "$TMP/results.txt"
echo ""
echo "  FL_PEND = total sum of flooder jobs that were pending at each 8-GPU competitor's submit time"
echo "  If FL_PEND=0: flooder's queue had already drained by the time 8-GPU competitor arrived (no signal)"
echo "  If FL_PEND>0 and NORM%=0: 8-GPU competitor waited its turn (FIFO-like for this competitor)"
echo "  If FL_PEND>0 and NORM% high: 8-GPU competitor jumped most of flooder's visible backlog (fairshare)"
