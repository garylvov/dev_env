#!/bin/bash
# slurm_job_duration.bash
# Reports average/min/max job duration, broken down by state and GPU count.
#
# Usage:
#   bash slurm_job_duration.bash [days_lookback] [partition] [exclude_users] [min_gpus] [min_duration_secs]
#   bash slurm_job_duration.bash 7
#   bash slurm_job_duration.bash 14 3090-gcondo
#   bash slurm_job_duration.bash 7 3090-gcondo "zzeng28,cl165"
#   bash slurm_job_duration.bash 7 3090-gcondo "" 4
#   bash slurm_job_duration.bash 7 3090-gcondo "" 1 60    # skip jobs < 60s

DAYS=${1:-7}
PARTITION=${2:-3090-gcondo}
EXCLUDE_ARG=${3:-}
MIN_GPUS=${4:-1}
MIN_DUR=${5:-1}
START_DATE=$(date -d "$DAYS days ago" +%Y-%m-%dT00:00:00)

EXCLUDE="${EXCLUDE_ARG//,/|}"

echo "=== Slurm Job Duration Stats ==="
echo "Partition : $PARTITION"
echo "Lookback  : $DAYS days (since $START_DATE)"
if [[ -n "$EXCLUDE" ]]; then
  echo "Excluding : ${EXCLUDE//|/, }"
else
  echo "Excluding : (none)"
fi
echo "Min GPUs  : $MIN_GPUS"
echo "Min Dur   : ${MIN_DUR}s"
echo ""

sacct \
  --partition="$PARTITION" \
  --starttime="$START_DATE" \
  --format=JobIDRaw,Elapsed,AllocTRES,ReqTRES,State,User \
  --noheader \
  --parsable2 \
  --allusers \
2>/dev/null | awk -F'|' -v exclude="$EXCLUDE" -v min_gpus="$MIN_GPUS" -v min_dur="$MIN_DUR" '
function parse_elapsed(e,    seconds, dp, tp, n2) {
  seconds = 0
  if (e ~ /-/) {
    split(e, dp, "-")
    seconds += dp[1] * 86400
    e = dp[2]
  }
  n2 = split(e, tp, ":")
  if (n2 == 3)      seconds += tp[1]*3600 + tp[2]*60 + tp[3]
  else if (n2 == 2) seconds += tp[1]*60 + tp[2]
  else              seconds += tp[1]
  return seconds
}

function fmt(secs) {
  d = int(secs / 86400)
  h = int((secs % 86400) / 3600)
  m = int((secs % 3600) / 60)
  return sprintf("%dd %2dh %2dm", d, h, m)
}

function fmt_gpuh(hours) {
  if (hours >= 100)
    return sprintf("%.0f", hours)
  else
    return sprintf("%.1f", hours)
}

function print_pctiles(arr, count,    i, start, n, psum, pmin, pmax, pcts, p, label, _parts, s, g, gpuh) {
  if (count < 4) return
  asort(arr)
  pcts[1] = 25; pcts[2] = 10; pcts[3] = 5; pcts[4] = 1
  for (p = 1; p <= 4; p++) {
    n = int(count * pcts[p] / 100)
    if (n < 1) n = 1
    start = count - n + 1
    psum = 0; pmin = -1; pmax = 0; gpuh = 0
    for (i = start; i <= count; i++) {
      split(arr[i], _parts, ":")
      s = _parts[1] + 0
      g = _parts[2] + 0
      psum += s
      gpuh += s * g / 3600
      if (pmin < 0 || s < pmin) pmin = s
      if (s > pmax) pmax = s
    }
    label = sprintf("Longest %d%%", pcts[p])
    printf "  %-25s %8d %14s %14s %14s %12s\n", \
      label, n, fmt(psum / n), fmt(pmin), fmt(pmax), fmt_gpuh(gpuh)
  }
}

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

  if (jobid ~ /\./) next
  if (user in excluded) next

  tres = (alloc != "") ? alloc : req

  gpus = 0
  n = split(tres, parts, ",")
  for (i = 1; i <= n; i++) {
    if (parts[i] ~ /gres\/gpu/) {
      split(parts[i], kv, "=")
      gpus = kv[2] + 0
    }
  }
  if (gpus < min_gpus) next

  secs = parse_elapsed(elapsed)
  if (secs < min_dur) next

  # Bucket by GPU count
  if (gpus <= 1)      bucket = "1 GPU"
  else if (gpus <= 2) bucket = "2 GPUs"
  else if (gpus <= 4) bucket = "3-4 GPUs"
  else                bucket = "5+ GPUs"

  # ── Running jobs ──
  if (state ~ /^RUNNING/) {
    run_sum += secs; run_count++
    run_gpuh += secs * gpus / 3600
    if (run_count == 1 || secs < run_min) run_min = secs
    if (run_count == 1 || secs > run_max) run_max = secs

    rb_sum[bucket] += secs; rb_count[bucket]++
    rb_gpuh[bucket] += secs * gpus / 3600
    if (!(bucket in rb_min) || secs < rb_min[bucket]) rb_min[bucket] = secs
    if (!(bucket in rb_max) || secs > rb_max[bucket]) rb_max[bucket] = secs
    run_durs[run_count] = sprintf("%015d:%d", secs, gpus)
  }

  # ── Completed jobs ──
  if (state ~ /^COMPLETED/) {
    comp_sum += secs; comp_count++
    comp_gpuh += secs * gpus / 3600
    if (comp_count == 1 || secs < comp_min) comp_min = secs
    if (comp_count == 1 || secs > comp_max) comp_max = secs

    cb_sum[bucket] += secs; cb_count[bucket]++
    cb_gpuh[bucket] += secs * gpus / 3600
    if (!(bucket in cb_min) || secs < cb_min[bucket]) cb_min[bucket] = secs
    if (!(bucket in cb_max) || secs > cb_max[bucket]) cb_max[bucket] = secs
    comp_durs[comp_count] = sprintf("%015d:%d", secs, gpus)
  }

  # ── All finished (completed + failed + timeout + cancelled) ──
  if (state ~ /^COMPLETED/ || state ~ /^FAILED/ || state ~ /^TIMEOUT/ || state ~ /^CANCELLED/) {
    all_sum += secs; all_count++
    all_gpuh += secs * gpus / 3600
    if (all_count == 1 || secs < all_min) all_min = secs
    if (all_count == 1 || secs > all_max) all_max = secs
    all_durs[all_count] = sprintf("%015d:%d", secs, gpus)
  }
}
END {
  if (run_count == 0 && all_count == 0) {
    print "  No matching jobs found."
    exit
  }

  buckets[1] = "1 GPU"; buckets[2] = "2 GPUs"
  buckets[3] = "3-4 GPUs"; buckets[4] = "5+ GPUs"

  # ── Currently running ──
  print "── Currently Running Jobs ──"
  print ""
  printf "  %-25s %8s %14s %14s %14s %12s\n", "", "JOBS", "AVG DURATION", "MIN DURATION", "MAX DURATION", "GPU-HOURS"
  printf "  %-25s %8s %14s %14s %14s %12s\n", "", "----", "------------", "------------", "------------", "---------"

  if (run_count > 0) {
    avg = run_sum / run_count
    printf "  %-25s %8d %14s %14s %14s %12s\n", \
      "All running", run_count, fmt(avg), fmt(run_min), fmt(run_max), fmt_gpuh(run_gpuh)

    for (i = 1; i <= 4; i++) {
      b = buckets[i]
      if (rb_count[b] > 0) {
        avg = rb_sum[b] / rb_count[b]
        printf "    %-23s %8d %14s %14s %14s %12s\n", \
          b, rb_count[b], fmt(avg), fmt(rb_min[b]), fmt(rb_max[b]), fmt_gpuh(rb_gpuh[b])
      }
    }
    print ""
    print_pctiles(run_durs, run_count)
  } else {
    print "  No running jobs found."
  }

  print ""

  # ── Completed jobs ──
  print "── Completed Jobs ──"
  print ""
  printf "  %-25s %8s %14s %14s %14s %12s\n", "", "JOBS", "AVG DURATION", "MIN DURATION", "MAX DURATION", "GPU-HOURS"
  printf "  %-25s %8s %14s %14s %14s %12s\n", "", "----", "------------", "------------", "------------", "---------"

  if (comp_count > 0) {
    avg = comp_sum / comp_count
    printf "  %-25s %8d %14s %14s %14s %12s\n", \
      "Completed", comp_count, fmt(avg), fmt(comp_min), fmt(comp_max), fmt_gpuh(comp_gpuh)

    for (i = 1; i <= 4; i++) {
      b = buckets[i]
      if (cb_count[b] > 0) {
        avg = cb_sum[b] / cb_count[b]
        printf "    %-23s %8d %14s %14s %14s %12s\n", \
          b, cb_count[b], fmt(avg), fmt(cb_min[b]), fmt(cb_max[b]), fmt_gpuh(cb_gpuh[b])
      }
    }
    print ""
    print_pctiles(comp_durs, comp_count)
  } else {
    print "  No completed jobs found."
  }

  if (all_count > 0) {
    avg = all_sum / all_count
    printf "\n  %-25s %8d %14s %14s %14s %12s\n", \
      "All finished (any exit)", all_count, fmt(avg), fmt(all_min), fmt(all_max), fmt_gpuh(all_gpuh)
    print_pctiles(all_durs, all_count)
  }
}
'
