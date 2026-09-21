#!/usr/bin/env bash
# codex_fleet_serve.sh — the SERVER half. Runs INSIDE a CPU-only Slurm job, on
# the compute node. Claude stays on login009 and never runs codex there.
#
# WHAT IT DOES
#   1. seeds a NODE-LOCAL CODEX_HOME (tmpfs) from ~/.codex: auth.json, config,
#      version.json, and a symlink to the real packages/ tree. The NFS ~/.codex
#      SQLite is never used by a server -- its POSIX locking does not survive NFS.
#   2. starts `codex app-server --listen ws://127.0.0.1:<port>` on this node.
#   3. writes the ENDPOINT CONTRACT and REFRESHES it every REFRESH seconds, so
#      its mtime IS the liveness signal. Removes it on clean exit, and on the
#      app-server dying, and on Slurm's TERM.
#   4. logs one row per event; never a directory listing.
#
# BIND + AUTH, DECIDED HONESTLY
#   The listener binds 127.0.0.1 ONLY. An unauthenticated ws port on a shared
#   cluster node is reachable by every other user on that node, and codex's own
#   banner says the listener "binds localhost only (use SSH port-forwarding for
#   remote access)". codex 0.153.4 DOES offer `--ws-auth capability-token
#   --ws-token-file`, but a token file good enough to publish to login009 has to
#   live on NFS, i.e. exactly the shared filesystem we are trying not to trust,
#   and it buys nothing that ssh does not already give.
#   So: LOOPBACK + `ssh <node>`. The client (codex_fleet.sh --node <host>) runs
#   its verbs ON the node over ssh; nothing is ever exposed off the node. This is
#   also the doctrinal transport for a daemon that must reparent to PID 1
#   (srun --overlap reaps detached children; ssh does not).
#   FALLBACK, if ssh to compute nodes is ever denied: --ws-auth capability-token
#   with --ws-token-file on a 0600 file, plus `ssh -L`. Not used, not proven.
set -uo pipefail

ENDPOINT_DEFAULT=/oscar/data/stellex/glvov/agrescap/evidence/codex_fleet/endpoint.json
LANE="${LANE:-default}"
ENDPOINT="${ENDPOINT:-$ENDPOINT_DEFAULT}"
REFRESH="${REFRESH:-30}"          # must stay well under the consumer's max age
PORT="${PORT:-}"
CODEX_BIN="${CODEX_BIN:-$HOME/.local/bin/codex}"
HOME_DIR="${CODEX_FLEET_HOME:-/tmp/codex-fleet-$USER/$LANE}"
TTL="${TTL:-0}"                   # seconds; 0 = serve until the job ends

die() { printf 'SERVE_REFUSE: %s\n' "$*" >&2; exit 2; }
log() { printf '%s serve lane=%s %s\n' "$(date -Is)" "$LANE" "$*" >> "$LOGFILE"; }

case "$(hostname)" in
  login*) die "codex may not run on a login node; this script belongs inside a Slurm job" ;;
esac
[ -x "$CODEX_BIN" ] || die "no codex binary at $CODEX_BIN"
case "$HOME_DIR" in
  /oscar/*|/gpfs/*|"$HOME"/*) die "CODEX_HOME $HOME_DIR is on NFS; codex SQLite does not survive NFS locking" ;;
  /*) ;;
  *) die "CODEX_HOME must be absolute" ;;
esac

mkdir -p "$HOME_DIR" "$(dirname "$ENDPOINT")" || die "cannot create state dirs"
chmod 700 "$HOME_DIR"
LOGFILE="${SERVE_LOG:-$(dirname "$ENDPOINT")/serve-$LANE.log}"
: >> "$LOGFILE" || die "cannot write $LOGFILE"

# --- seed the node-local home -------------------------------------------------
for f in config.toml auth.json version.json models_cache.json; do
  [ -e "$HOME/.codex/$f" ] && cp -f "$HOME/.codex/$f" "$HOME_DIR/" 2>/dev/null
done
ln -sfn "$HOME/.codex/packages" "$HOME_DIR/packages"
[ -s "$HOME_DIR/auth.json" ] || die "no auth.json seeded into $HOME_DIR; codex would refuse every turn"
chmod 600 "$HOME_DIR"/*.json 2>/dev/null
log "seeded home=$HOME_DIR"

# --- pick a free loopback port ------------------------------------------------
free_port() {
  local p
  for p in $(seq 8400 8460); do
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null || { printf '%s' "$p"; return 0; }
    exec 3>&- 2>/dev/null
  done
  return 1
}
[ -n "$PORT" ] || PORT="$(free_port)" || die "no free loopback port in 8400-8460"

# --- start the app-server -----------------------------------------------------
CODEX_HOME="$HOME_DIR" setsid nohup "$CODEX_BIN" app-server \
  --listen "ws://127.0.0.1:$PORT" > "$HOME_DIR/app-server.log" 2>&1 < /dev/null &
SRV_PID=$!
log "app-server pid=$SRV_PID port=$PORT"

READY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  code="$(curl -sS -m5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/readyz" 2>/dev/null)"
  [ "$code" = "200" ] && { READY=1; break; }
done
[ "$READY" = "1" ] || { log "FAIL readyz never returned 200"; kill "$SRV_PID" 2>/dev/null; die "app-server did not become ready on 127.0.0.1:$PORT"; }
log "ready readyz=200"

CODEX_VER="$("$CODEX_BIN" --version 2>/dev/null | tail -1)"
STARTED="$(date -Is)"

write_endpoint() {
  local tmp="$ENDPOINT.tmp.$$"
  printf '{"url":"ws://127.0.0.1:%s","host":"%s","port":%s,"slurm_jobid":"%s","started":"%s","codex_version":"%s","codex_home":"%s","pid":%s,"lane":"%s","transport":"ssh","reach":"ssh %s then ws://127.0.0.1:%s","refreshed":"%s"}\n' \
    "$PORT" "$(hostname)" "$PORT" "${SLURM_JOB_ID:-none}" "$STARTED" "$CODEX_VER" \
    "$HOME_DIR" "$SRV_PID" "$LANE" "$(hostname)" "$PORT" "$(date -Is)" > "$tmp" \
    && mv -f "$tmp" "$ENDPOINT"
}

cleanup() {
  log "cleanup: removing endpoint and stopping pid=$SRV_PID"
  rm -f "$ENDPOINT"
  kill "$SRV_PID" 2>/dev/null
  # §12: a kill is verified by the process being gone, never by an exit code.
  for _ in 1 2 3 4 5; do kill -0 "$SRV_PID" 2>/dev/null || break; sleep 1; done
  kill -0 "$SRV_PID" 2>/dev/null && kill -9 "$SRV_PID" 2>/dev/null
  log "cleanup done alive=$(kill -0 "$SRV_PID" 2>/dev/null && echo yes || echo no)"
}
trap 'cleanup; exit 0' TERM INT EXIT

write_endpoint
log "endpoint written $ENDPOINT"
printf 'SERVE_UP host=%s port=%s jobid=%s home=%s endpoint=%s\n' \
  "$(hostname)" "$PORT" "${SLURM_JOB_ID:-none}" "$HOME_DIR" "$ENDPOINT"

# --- refresh loop: mtime IS liveness -----------------------------------------
ELAPSED=0
while :; do
  sleep "$REFRESH"
  ELAPSED=$((ELAPSED + REFRESH))
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    # §9: the detector terminates in an actuator. The endpoint goes away so the
    # dispatcher refuses fast and the router falls back, instead of hanging.
    log "FAIL app-server pid=$SRV_PID is gone; removing endpoint and exiting loudly"
    rm -f "$ENDPOINT"
    exit 3
  fi
  write_endpoint
  if [ "$TTL" -gt 0 ] && [ "$ELAPSED" -ge "$TTL" ]; then
    log "TTL $TTL reached; clean exit"
    exit 0
  fi
done
