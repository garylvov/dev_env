#!/usr/bin/env bash
set -Eeuo pipefail

BMC_IP="192.168.1.162"
BMC_USER="rescueadmin"
BMC_PASS="StrongRescuePass"

POLL=2
TIMEOUT_BMC=150
TIMEOUT_SYSREADY=120
TIMEOUT_TASK=240
SLEEP_AFTER_OFF=20
SLEEP_AFTER_BMC=5
RETRIES_ON=150

curl_json(){ curl -ksu "$BMC_USER:$BMC_PASS" -H "Content-Type: application/json" "$@"; }
curl_bmc(){ curl -ksu "$BMC_USER:$BMC_PASS" "$1"; }
bmc_up(){ curl -ks --connect-timeout 2 "https://${BMC_IP}/redfish/v1/" >/dev/null; }

wait_bmc_up(){
  echo "[~] Waiting for BMC HTTPS…"
  local dl=$((SECONDS+TIMEOUT_BMC))
  while ((SECONDS<dl)); do bmc_up && { echo "    BMC is up."; return; }; sleep "$POLL"; done
  echo "    ERROR: BMC did not come up"; exit 1
}
wait_systems_ready(){
  echo "[~] Waiting for Systems/Self readiness…"
  local url="https://${BMC_IP}/redfish/v1/Systems/Self" dl=$((SECONDS+TIMEOUT_SYSREADY))
  while ((SECONDS<dl)); do
    body="$(curl_bmc "$url" || true)"
    grep -q 'Actions.*ComputerSystem.Reset' <<<"$body" && { echo "    Systems Reset action is available."; return; }
    sleep "$POLL"
  done
  echo "    ERROR: Systems/Self not ready"; exit 1
}
get_power(){ curl_bmc "https://${BMC_IP}/redfish/v1/Systems/Self" | sed -n 's/.*"PowerState":"\([^"]*\)".*/\1/p'; }
wait_task_complete(){
  local task_id="${1:-}"
  if [[ -z "$task_id" ]]; then
    echo "    WARN: wait_task_complete() called without a task id — skipping wait."
    return 1
  fi
  local url="https://${BMC_IP}/redfish/v1/TaskService/Tasks/${task_id}" dl=$((SECONDS+TIMEOUT_TASK)) st
  echo -n "    waiting for task ${task_id}…"
  while ((SECONDS<dl)); do
    st="$(curl_bmc "$url" | sed -n 's/.*"TaskState":"\([^"]*\)".*/\1/p')"
    [[ -n "$st" ]] && echo -n " ${st}\r"
    case "$st" in Completed|Exception|Killed) echo; return;; esac
    sleep 1
  done; echo; echo "    WARN: task ${task_id} wait timed out"
}
post_systems(){ curl_json -X POST -d "{\"ResetType\":\"$1\"}" "https://${BMC_IP}/redfish/v1/Systems/Self/Actions/ComputerSystem.Reset"; }
post_chassis(){ curl_json -X POST -d "{\"ResetType\":\"$1\"}" "https://${BMC_IP}/redfish/v1/Chassis/Self/Actions/Chassis.Reset"; }
manager_reset(){
  echo "[~] Manager.Reset (BMC reboot)…"
  local r; r="$(curl_json -X POST -d '{"ResetType":"ForceRestart"}' "https://${BMC_IP}/redfish/v1/Managers/Self/Actions/Manager.Reset" || true)"
  local tid; tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$r" || true)"
  [[ -n "$tid" ]] && echo "    (task ${tid})"
}

try_power_on_loop(){
  echo "[4] Powering On (retry until Task appears; Systems.On + Chassis.On)…"
  local resp tid nbytes preview pwr
  for ((i=1;i<=RETRIES_ON;i++)); do
    curl_bmc "https://${BMC_IP}/redfish/v1/Systems/Self" >/dev/null 2>&1 || true
    resp="$(post_systems On)"; nbytes=$(printf %s "$resp" | wc -c); preview="$(printf %s "$resp" | head -c 280)"
    printf '    try %d Systems.On:  bytes=%s preview=%s\n' "$i" "$nbytes" "$preview"
    if grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
      tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp")"; echo "    Success: Task $tid (Systems)"; return 0; fi
    pwr="$(get_power || true)"; echo "       (reported PowerState=${pwr:-unknown})"
    resp="$(post_chassis On)"; nbytes=$(printf %s "$resp" | wc -c); preview="$(printf %s "$resp" | head -c 280)"
    printf '    try %d Chassis.On:  bytes=%s preview=%s\n' "$i" "$nbytes" "$preview"
    if grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
      tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp")"; echo "    Success: Task $tid (Chassis)"; return 0; fi
    sleep "$POLL"
  done; return 1
}

main(){
  wait_bmc_up; wait_systems_ready
  state="$(get_power || true)"; echo "[1] Current PowerState=${state:-unknown}"
  if [[ "$state" == "On" ]]; then
    echo "[2] ForceOff (wait task)…"
    resp="$(post_systems ForceOff)"
    if grep -q '"@odata.id":"/redfish/v1/TaskService/Tasks/' <<<"$resp"; then
      tid="$(sed -n 's/.*"Id":"\([^"]*\)".*/\1/p' <<<"$resp" || true)"
      echo "    task ${tid:-<unknown>}"
      wait_task_complete "${tid:-}"
    else
      echo "    WARN: ForceOff had no Task; resp: $resp"
    fi
    echo "    sleeping ${SLEEP_AFTER_OFF}s to settle…"; sleep "$SLEEP_AFTER_OFF"
    manager_reset; sleep "$SLEEP_AFTER_BMC"; wait_bmc_up; wait_systems_ready
  else
    echo "[2] Already Off/unknown — skipping ForceOff."
  fi
  if try_power_on_loop; then echo "[5] Done."; exit 0; fi
  echo "[!] Still no On task — performing one more BMC reboot, then last try…"
  manager_reset; sleep "$SLEEP_AFTER_BMC"; wait_bmc_up; wait_systems_ready
  if try_power_on_loop; then echo "[5] Done (after extra BMC reboot)."; exit 0; fi
  echo "[X] ERROR: Could not obtain an On task."; exit 1
}
main

