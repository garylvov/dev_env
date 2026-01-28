#!/bin/bash
# slurm_usage_stats.bash
# Reports:
#   1. Average/min/max wait time for jobs requesting >= 8 GPUs
#   2. Ranking of users by total GPU-hours consumed
#
# Usage:
#   bash slurm_usage_stats.bash [days_lookback] [partition] [exclude_users] [min_gpus]
#   bash slurm_usage_stats.bash 7
#   bash slurm_usage_stats.bash 14 3090-gcondo
#   bash slurm_usage_stats.bash 7 3090-gcondo "glvov"
#   bash slurm_usage_stats.bash 7 3090-gcondo "" 1

DAYS=${1:-7}
PARTITION=${2:-3090-gcondo}
EXCLUDE_ARG=${3:-}
MIN_GPUS=${4:-1}
START_DATE=$(date -d "$DAYS days ago" +%Y-%m-%dT00:00:00)

# Convert comma-separated exclude list to pipe-separated for awk
EXCLUDE="${EXCLUDE_ARG//,/|}"

echo "=== Slurm Usage Stats ==="
echo "Partition : $PARTITION"
echo "Lookback  : $DAYS days (since $START_DATE)"
if [[ -n "$EXCLUDE" ]]; then
  echo "Excluding : ${EXCLUDE//|/, }"
else
  echo "Excluding : (none)"
fi
echo ""

# ── Section 1: Wait time for 8+ GPU jobs ──────────────────────────────

echo "── Wait Time for Jobs Requesting >= $MIN_GPUS GPUs ──"
echo ""

sacct \
  --partition="$PARTITION" \
  --starttime="$START_DATE" \
  --format=JobIDRaw,Submit,Start,AllocTRES,ReqTRES,State,User \
  --noheader \
  --parsable2 \
  --allusers \
2>/dev/null | awk -F'|' -v exclude="$EXCLUDE" -v min_gpus="$MIN_GPUS" '
BEGIN {
  split(exclude, ex_arr, "|")
  for (i in ex_arr) excluded[ex_arr[i]] = 1
}
{
  jobid    = $1
  submit   = $2
  start    = $3
  alloc    = $4
  req      = $5
  state    = $6
  user     = $7

  # Skip sub-steps (.batch/.extern)
  if (jobid ~ /\./) next

  # Skip excluded users
  if (user in excluded) next

  # Use AllocTRES if available, fall back to ReqTRES
  tres = (alloc != "") ? alloc : req

  # Extract GPU count from TRES (e.g. "gres/gpu=8" or "gres/gpu:rtx_3090=8")
  gpus = 0
  n = split(tres, parts, ",")
  for (i = 1; i <= n; i++) {
    if (parts[i] ~ /gres\/gpu/) {
      split(parts[i], kv, "=")
      gpus = kv[2] + 0
    }
  }
  if (gpus < min_gpus) next

  # Jobs that never started (cancelled while pending)
  if (start == "Unknown" || start == "None" || start == "") {
    never_started++
    next
  }

  # Convert timestamps to epoch
  cmd_sub = "date -d \"" submit "\" +%s 2>/dev/null"
  cmd_sub | getline submit_epoch
  close(cmd_sub)

  cmd_start = "date -d \"" start "\" +%s 2>/dev/null"
  cmd_start | getline start_epoch
  close(cmd_start)

  wait = start_epoch - submit_epoch
  if (wait < 0) next

  # Categorize by outcome
  if (state ~ /^COMPLETED/ || state ~ /^RUNNING/) {
    sched_sum += wait; sched_count++
    if (sched_count == 1 || wait > sched_max) sched_max = wait
  } else {
    fail_sum += wait; fail_count++
    if (fail_count == 1 || wait > fail_max) fail_max = wait
  }

  all_sum += wait
  all_count++
  if (all_count == 1 || wait > all_max) all_max = wait
}
END {
  if (all_count == 0 && never_started == 0) {
    print "  No matching jobs found."
    exit
  }

  printf "  %-35s %8s %12s %12s\n", "", "JOBS", "AVG WAIT", "MAX WAIT"
  printf "  %-35s %8s %12s %12s\n", "", "----", "--------", "--------"

  if (all_count > 0) {
    avg = all_sum / all_count
    printf "  %-35s %8d %4dd %2dh %2dm %4dd %2dh %2dm\n", \
      "All started jobs", all_count, \
      avg/86400, (avg%86400)/3600, (avg%3600)/60, \
      all_max/86400, (all_max%86400)/3600, (all_max%3600)/60
  }

  if (sched_count > 0) {
    avg = sched_sum / sched_count
    printf "  %-35s %8d %4dd %2dh %2dm %4dd %2dh %2dm\n", \
      "Completed/Running", sched_count, \
      avg/86400, (avg%86400)/3600, (avg%3600)/60, \
      sched_max/86400, (sched_max%86400)/3600, (sched_max%3600)/60
  }

  if (fail_count > 0) {
    avg = fail_sum / fail_count
    printf "  %-35s %8d %4dd %2dh %2dm %4dd %2dh %2dm\n", \
      "Failed/Cancelled/Timeout", fail_count, \
      avg/86400, (avg%86400)/3600, (avg%3600)/60, \
      fail_max/86400, (fail_max%86400)/3600, (fail_max%3600)/60
  }

  if (never_started > 0) {
    printf "  %-35s %8d %12s %12s\n", \
      "Cancelled before starting", never_started, "n/a", "n/a"
  }
}'

echo ""

# ── Section 2: GPU-hours ranking by user ───────────────────────────────

echo "── GPU-Hours Ranking by User ──"
echo ""

sacct \
  --partition="$PARTITION" \
  --starttime="$START_DATE" \
  --format=JobIDRaw,Elapsed,AllocTRES,ReqTRES,State,User \
  --noheader \
  --parsable2 \
  --allusers \
2>/dev/null | awk -F'|' -v exclude="$EXCLUDE" '
BEGIN {
  split(exclude, ex_arr, "|")
  for (i in ex_arr) excluded[ex_arr[i]] = 1
}
{
  jobid   = $1
  elapsed = $2
  alloc   = $3
  req     = $4
  state   = $5
  user    = $6

  # Skip sub-steps
  if (jobid ~ /\./) next

  # Skip excluded users
  if (user in excluded) next

  # Use AllocTRES if available, fall back to ReqTRES
  tres = (alloc != "") ? alloc : req

  # Extract GPU count from TRES (e.g. "gres/gpu=8" or "gres/gpu:rtx_3090=8")
  gpus = 0
  n = split(tres, parts, ",")
  for (i = 1; i <= n; i++) {
    if (parts[i] ~ /gres\/gpu/) {
      split(parts[i], kv, "=")
      gpus = kv[2] + 0
    }
  }
  if (gpus == 0) next

  # Parse elapsed time: [[days-]hours:]minutes:seconds
  seconds = 0
  e = elapsed
  if (e ~ /-/) {
    split(e, dp, "-")
    seconds += dp[1] * 86400
    e = dp[2]
  }
  n2 = split(e, tp, ":")
  if (n2 == 3)      seconds += tp[1]*3600 + tp[2]*60 + tp[3]
  else if (n2 == 2) seconds += tp[1]*60 + tp[2]
  else              seconds += tp[1]

  gpu_hours = (gpus * seconds) / 3600.0
  user_gpuhrs[user] += gpu_hours
  user_jobs[user]++
  total_gpuhrs += gpu_hours
}
END {
  if (length(user_gpuhrs) == 0) {
    print "  No matching jobs found."
    exit
  }

  # Sort by gpu-hours descending (collect into arrays)
  n = 0
  for (u in user_gpuhrs) {
    n++
    names[n] = u
    vals[n] = user_gpuhrs[u]
  }
  for (i = 1; i < n; i++) {
    for (j = i+1; j <= n; j++) {
      if (vals[j] > vals[i]) {
        tmp = vals[i];  vals[i] = vals[j];  vals[j] = tmp
        tmps = names[i]; names[i] = names[j]; names[j] = tmps
      }
    }
  }

  printf "  %-15s %10s %8s %8s\n", "USER", "GPU-HOURS", "JOBS", "% TOTAL"
  printf "  %-15s %10s %8s %8s\n", "----", "---------", "----", "-------"
  for (i = 1; i <= n; i++) {
    u = names[i]
    pct = (vals[i] / total_gpuhrs) * 100
    printf "  %-15s %10.1f %8d %7.1f%%\n", u, vals[i], user_jobs[u], pct
  }
  printf "\n  %-15s %10.1f\n", "TOTAL", total_gpuhrs
}'

