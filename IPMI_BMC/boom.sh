#!/usr/bin/env bash
set -Eeuo pipefail

# ===== Config =====
BMC_IP="192.168.1.162"
BMC_USER="rescueadmin"
BMC_PASS="StrongRescuePass"

POLL=2
TIMEOUT_BMC=150
TIMEOUT_SYSREADY=300
TIMEOUT_TASK=240
SLEEP_AFTER_OFF=20
SLEEP_AFTER_BMC=5
RETRIES_ON=150

# ===== Dry-run mode =====
DRY_RUN=false

# ===== Auth mode state =====
AUTH_MODE="session"
SESSION_TOKEN=""
SESSION_URI=""
REQ_ID="boom"

# ===== Discovered URIs =====
SYS_URI=""
CH_URI=""
MAN_URI=""

# ===== Usage =====
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Power-cycle or power-on a server via BMC Redfish API.

Options:
  --dry, --dry-run    Show what would be done without sending power commands
  -h, --help          Show this help message

EOF
  exit 0
}

# ===== Parse arguments =====
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry|--dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      ;;
  esac
done

if $DRY_RUN; then
  echo "=========================================="
  echo "  DRY-RUN MODE - no power commands sent"
  echo "=========================================="
  echo ""
fi

# ===== Curl base (returns payload + ' HTTPSTATUS:<code>') =====
_base_curl() {
  curl -kLsS -w ' HTTPSTATUS:%{http_code}' "$@" || true
}

_build_auth_argv() {
  local __name="$1"
  local -a a=()
  case "$AUTH_MODE" in
    session)
      if [[ -n "${SESSION_TOKEN:-}" ]]; then
        a+=(-H "X-Auth-Token: ${SESSION_TOKEN}")
      fi
      ;;
    digest)
      if [[ -n "${BMC_USER:-}" ]] && [[ -n "${BMC_PASS:-}" ]]; then
        a+=(--digest -u "${BMC_USER}:${BMC_PASS}")
      else
        echo "    WARN: Missing BMC_USER/BMC_PASS for digest auth" >&2
      fi
      ;;
    basic)
      if [[ -n "${BMC_USER:-}" ]] && [[ -n "${BMC_PASS:-}" ]]; then
        a+=(-u "${BMC_USER}:${BMC_PASS}")
      else
        echo "    WARN: Missing BMC_USER/BMC_PASS for basic auth" >&2
      fi
      ;;
  esac
  eval "$__name=(\"\${a[@]}\")"
}

curl_bmc() {
  local url="$1"; shift || true
  local -a auth_argv=(); _build_auth_argv auth_argv
  _base_curl "$url" "$@" "${auth_argv[@]}"
}

curl_json() {
  local url="$1"; shift || true
  local -a auth_argv=(); _build_auth_argv auth_argv
  _base_curl "$url" -H "Content-Type: application/json" -H "X-Requested-By: ${REQ_ID}" "$@" "${auth_argv[@]}"
}

bmc_up() {
  local out status
  out="$(curl -ks --connect-timeout 2 "https://${BMC_IP}/redfish/v1/" -w ' HTTPSTATUS:%{http_code}' || true)"
  status="${out##*HTTPSTATUS:}"
  [[ "$status" == "200" ]]
}

# ===== Auth helpers =====
create_session() {
  echo "    Creating Redfish session…"
  local resp token loc
  resp="$(curl -kLsS -D - "https://${BMC_IP}/redfish/v1/SessionService/Sessions" \
      -H 'Content-Type: application/json' \
      -H "X-Requested-By: ${REQ_ID}" \
      --data "{\"UserName\":\"${BMC_USER}\",\"Password\":\"${BMC_PASS}\"}" || true)"
  token="$(printf %s "$resp" | awk 'BEGIN{IGNORECASE=1} /^X-Auth-Token:/ {sub(/\r$/,"",$0); print $2}' | tail -n1)"
  loc="$(printf %s "$resp" | awk 'BEGIN{IGNORECASE=1} /^Location:/ {sub(/\r$/,"",$0); print $2}' | tail -n1)"
  if [[ -n "$token" ]]; then
    SESSION_TOKEN="$token"
    if [[ "$loc" == https://* ]]; then
      SESSION_URI="/${loc#*//*/}"; SESSION_URI="/${SESSION_URI#*/}"
    else
      SESSION_URI="$loc"
    fi
    echo "    Session established."
    return 0
  fi
  echo "    WARN: Session creation failed; falling back."
  return 1
}

destroy_session() {
  [[ -z "${SESSION_URI:-}" ]] && return 0
  _base_curl "https://${BMC_IP}${SESSION_URI}" -X DELETE -H "X-Auth-Token: ${SESSION_TOKEN}" >/dev/null 2>&1 || true
  SESSION_TOKEN=""; SESSION_URI=""
}

set_auth_mode() {
  local next="$1"
  if [[ "$AUTH_MODE" != "$next" ]]; then
    echo "    Switching auth to: $next"
    AUTH_MODE="$next"
  fi
}

ensure_auth() {
  case "$AUTH_MODE" in
    session)
      [[ -n "$SESSION_TOKEN" ]] && return 0
      if create_session; then return 0; fi
      set_auth_mode digest
      return 0
      ;;
    digest|basic) return 0 ;;
  esac
}

handle_auth_failure() {
  case "$AUTH_MODE" in
    session) destroy_session; set_auth_mode digest ;;
    digest)  set_auth_mode basic ;;
    basic)   echo "    ERROR: Authentication failed under all modes (session/digest/basic)."; exit 1 ;;
  esac
}

# ===== Discovery =====
discover_uris() {
  echo "[~] Discovering Redfish URIs…"
  ensure_auth

  local systems status payload
  systems="$(curl_bmc "https://${BMC_IP}/redfish/v1/Systems")"
  status="${systems##*HTTPSTATUS:}"; payload="${systems% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth; systems="$(curl_bmc "https://${BMC_IP}/redfish/v1/Systems")"; status="${systems##*HTTPSTATUS:}"; payload="${systems% HTTPSTATUS:*}"; fi
  SYS_URI="$(printf %s "$payload" | sed -n 's/.*"Members":[^]]*{"@odata.id":"\([^"]*\)".*/\1/p' | head -n1 || true)"
  [[ -z "${SYS_URI:-}" ]] && SYS_URI="/redfish/v1/Systems/Self"

  local chassis
  chassis="$(curl_bmc "https://${BMC_IP}/redfish/v1/Chassis")"
  status="${chassis##*HTTPSTATUS:}"; payload="${chassis% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth; chassis="$(curl_bmc "https://${BMC_IP}/redfish/v1/Chassis")"; status="${chassis##*HTTPSTATUS:}"; payload="${chassis% HTTPSTATUS:*}"; fi
  CH_URI="$(printf %s "$payload" | sed -n 's/.*"Members":[^]]*{"@odata.id":"\([^"]*\)".*/\1/p' | head -n1 || true)"
  [[ -z "${CH_URI:-}" ]] && CH_URI="/redfish/v1/Chassis/Self"

  local managers
  managers="$(curl_bmc "https://${BMC_IP}/redfish/v1/Managers")"
  status="${managers##*HTTPSTATUS:}"; payload="${managers% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth; managers="$(curl_bmc "https://${BMC_IP}/redfish/v1/Managers")"; status="${managers##*HTTPSTATUS:}"; payload="${managers% HTTPSTATUS:*}"; fi
  MAN_URI="$(printf %s "$payload" | sed -n 's/.*"Members":[^]]*{"@odata.id":"\([^"]*\)".*/\1/p' | head -n1 || true)"
  [[ -z "${MAN_URI:-}" ]] && MAN_URI="/redfish/v1/Managers/Self"

  echo "    SYS_URI=${SYS_URI}  CH_URI=${CH_URI}  MAN_URI=${MAN_URI}  (auth=${AUTH_MODE})"
}

# ===== Waits =====
wait_bmc_up() {
  echo "[~] Waiting for BMC HTTPS…"
  local dl=$((SECONDS+TIMEOUT_BMC))
  while ((SECONDS<dl)); do
    if bmc_up; then echo "    BMC is up."; return; fi
    sleep "$POLL"
  done
  echo "    ERROR: BMC did not come up"; exit 1
}

wait_systems_ready() {
  echo "[~] Waiting for Systems readiness…"
  ensure_auth
  local url="https://${BMC_IP}${SYS_URI}" dl=$((SECONDS+TIMEOUT_SYSREADY)) body status payload oneline
  while ((SECONDS<dl)); do
    body="$(curl_bmc "$url")"
    status="${body##*HTTPSTATUS:}"; payload="${body% HTTPSTATUS:*}"
    oneline="$(printf %s "$payload" | tr -d '\n')"
    printf '  poll %s status=%s\r' "$(date +%H:%M:%S)" "$status"

    if [[ "$status" == "401" || "$status" == "403" ]]; then
      echo -e "\n    auth rejected (status=$status); retrying with different auth…"
      handle_auth_failure; ensure_auth; continue
    fi
    if [[ "$status" == 3* || "$status" == "404" ]]; then
      echo -e "\n    URI not ready (status=$status); rediscovering…"
      discover_uris; url="https://${BMC_IP}${SYS_URI}"; continue
    fi

    if grep -q '"Actions".*"#\{0,1\}ComputerSystem\.Reset"' <<<"$oneline" || \
       grep -q '"Actions".*"ComputerSystem\.Reset"' <<<"$oneline"; then
      echo -e "\n    Systems Reset action is available."
      return
    fi
    sleep "$POLL"
  done
  echo -e "\n    ERROR: Systems not ready at $url"; exit 1
}

# ===== Utilities =====
get_power() {
  local r status payload
  r="$(curl_bmc "https://${BMC_IP}${SYS_URI}")"
  status="${r##*HTTPSTATUS:}"; payload="${r% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth; r="$(curl_bmc "https://${BMC_IP}${SYS_URI}")"; payload="${r% HTTPSTATUS:*}"; fi
  printf %s "$payload" | sed -n 's/.*"PowerState":"\([^"]*\)".*/\1/p'
}

wait_task_complete() {
  local task_id="${1:-}"
  if [[ -z "$task_id" ]]; then
    echo "    WARN: wait_task_complete() called without a task id — skipping wait."
    return 1
  fi
  if $DRY_RUN; then
    echo "    [DRY-RUN] Would wait for task ${task_id}"
    return 0
  fi
  local url="https://${BMC_IP}/redfish/v1/TaskService/Tasks/${task_id}" dl=$((SECONDS+TIMEOUT_TASK)) st r status payload
  echo -n "    waiting for task ${task_id}…"
  while ((SECONDS<dl)); do
    r="$(curl_bmc "$url")"; status="${r##*HTTPSTATUS:}"; payload="${r% HTTPSTATUS:*}"
    if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth; r="$(curl_bmc "$url")"; payload="${r% HTTPSTATUS:*}"; fi
    st="$(printf %s "$payload" | sed -n 's/.*"TaskState":"\([^"]*\)".*/\1/p')"
    [[ -n "$st" ]] && echo -n " ${st}\r"
    case "$st" in Completed|Exception|Killed) echo; return;; esac
    sleep 1
  done
  echo; echo "    WARN: task ${task_id} wait timed out"
}

post_systems() {
  local action="$1"
  local url="https://${BMC_IP}${SYS_URI}/Actions/ComputerSystem.Reset"

  if $DRY_RUN; then
    echo "    [DRY-RUN] Would POST to ${url} with ResetType=${action}"
    return
  fi

  local r status payload
  r="$(curl_json "$url" -X POST --data "{\"ResetType\":\"$action\"}")"
  status="${r##*HTTPSTATUS:}"; payload="${r% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth
    r="$(curl_json "$url" -X POST --data "{\"ResetType\":\"$action\"}")"
    payload="${r% HTTPSTATUS:*}"
  fi
  printf %s "$payload"
}

post_chassis() {
  local action="$1"
  local url="https://${BMC_IP}${CH_URI}/Actions/Chassis.Reset"

  if $DRY_RUN; then
    echo "    [DRY-RUN] Would POST to ${url} with ResetType=${action}"
    return
  fi

  local r status payload
  r="$(curl_json "$url" -X POST --data "{\"ResetType\":\"$action\"}")"
  status="${r##*HTTPSTATUS:}"; payload="${r% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth
    r="$(curl_json "$url" -X POST --data "{\"ResetType\":\"$action\"}")"
    payload="${r% HTTPSTATUS:*}"
  fi
  printf %s "$payload"
}

manager_reset() {
  local url="https://${BMC_IP}${MAN_URI}/Actions/Manager.Reset"

  if $DRY_RUN; then
    echo "[~] Manager.Reset (BMC reboot)…"
    echo "    [DRY-RUN] Would POST to ${url} with ResetType=ForceRestart"
    return
  fi

  echo "[~] Manager.Reset (BMC reboot)…"
  local r status payload tid
  r="$(curl_json "$url" -X POST --data '{"ResetType":"ForceRestart"}')"
  status="${r##*HTTPSTATUS:}"; payload="${r% HTTPSTATUS:*}"
  if [[ "$status" == "401" || "$status" == "403" ]]; then handle_auth_failure; ensure_auth
    r="$(curl_json "$url" -X POST --data '{"ResetType":"ForceRestart"}')"
    payload="${r% HTTPSTATUS:*}"
  fi
  tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$payload" || true)"
  [[ -n "$tid" ]] && echo "    (task ${tid})"

  # BMC reboot invalidates the session — clear it so ensure_auth() creates a new one
  SESSION_TOKEN=""
  SESSION_URI=""
}

try_power_on_loop() {
  echo "[4] Powering On (retry until Task appears; Systems.On + Chassis.On)…"

  if $DRY_RUN; then
    echo "    [DRY-RUN] Would attempt up to ${RETRIES_ON} retries of:"
    echo "      - POST ${SYS_URI}/Actions/ComputerSystem.Reset {ResetType:On}"
    echo "      - POST ${CH_URI}/Actions/Chassis.Reset {ResetType:On}"
    return 0
  fi

  local resp tid nbytes preview pwr
  for ((i=1;i<=RETRIES_ON;i++)); do
    curl_bmc "https://${BMC_IP}${SYS_URI}" >/dev/null || true

    resp="$(post_systems On)"; nbytes=$(printf %s "$resp" | wc -c); preview="$(printf %s "$resp" | head -c 280)"
    printf '    try %d Systems.On:  bytes=%s preview=%s\n' "$i" "$nbytes" "$preview"
    if grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
      tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp")"
      echo "    Success: Task $tid (Systems)"; return 0
    fi

    pwr="$(get_power || true)"; echo "       (reported PowerState=${pwr:-unknown})"

    # BMC may report stale PowerState=On after its own reboot; if On gave
    # NoOperation, try ForceRestart to actually bring the host up.
    if grep -q '"NoOperation"' <<<"$resp" && [[ "${pwr:-}" == "On" ]]; then
      echo "    BMC reports On but host may be stale — trying ForceRestart…"
      resp="$(post_systems ForceRestart)"
      if grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
        tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp")"
        echo "    Success: Task $tid (ForceRestart)"; return 0
      fi
      preview="$(printf %s "$resp" | head -c 280)"
      echo "    ForceRestart response: $preview"
    fi

    resp="$(post_chassis On)"; nbytes=$(printf %s "$resp" | wc -c); preview="$(printf %s "$resp" | head -c 280)"
    printf '    try %d Chassis.On:  bytes=%s preview=%s\n' "$i" "$nbytes" "$preview"
    if grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
      tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp")"
      echo "    Success: Task $tid (Chassis)"; return 0
    fi

    sleep "$POLL"
  done
  return 1
}

# ===== Main =====
main() {
  if [[ -z "${BMC_USER:-}" || -z "${BMC_PASS:-}" ]]; then
    echo "WARN: BMC_USER or BMC_PASS is empty."
  fi

  trap 'destroy_session' EXIT

  wait_bmc_up
  discover_uris
  wait_systems_ready

  state="$(get_power || true)"; echo "[1] Current PowerState=${state:-unknown}"

  if [[ "${state:-unknown}" == "On" ]]; then
    echo "[2] ForceOff (wait task)…"
    resp="$(post_systems ForceOff)"
    if $DRY_RUN; then
      echo "    [DRY-RUN] Would wait for ForceOff task, then sleep ${SLEEP_AFTER_OFF}s"
    elif grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
      tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp" || true)"
      echo "    task ${tid:-<unknown>}"
      wait_task_complete "${tid:-}"
    else
      echo "    WARN: ForceOff had no Task; resp: $resp"
    fi

    if $DRY_RUN; then
      echo "    [DRY-RUN] Would sleep ${SLEEP_AFTER_OFF}s to settle"
      manager_reset
      echo "    [DRY-RUN] Would sleep ${SLEEP_AFTER_BMC}s, then wait for BMC + rediscover"
    else
      echo "    sleeping ${SLEEP_AFTER_OFF}s to settle…"; sleep "$SLEEP_AFTER_OFF"
      manager_reset; sleep "$SLEEP_AFTER_BMC"; wait_bmc_up; discover_uris; wait_systems_ready
    fi
  else
    echo "[2] Already Off/unknown — skipping ForceOff."
  fi

  if try_power_on_loop; then
    echo "[5] Done."
    $DRY_RUN && echo "[DRY-RUN] No actual power commands were sent."
    exit 0
  fi

  echo "[!] Still no On task — performing one more BMC reboot, then last try…"
  if $DRY_RUN; then
    manager_reset
    echo "    [DRY-RUN] Would sleep ${SLEEP_AFTER_BMC}s, wait for BMC, rediscover, then retry power on"
    echo "[5] Done."
    echo "[DRY-RUN] No actual power commands were sent."
    exit 0
  fi

  manager_reset; sleep "$SLEEP_AFTER_BMC"; wait_bmc_up; discover_uris; wait_systems_ready
  if try_power_on_loop; then echo "[5] Done (after extra BMC reboot)."; exit 0; fi

  echo "[X] ERROR: Could not obtain an On task."; exit 1
}

main
