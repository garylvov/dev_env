#!/usr/bin/env bash
# codex_fleet.sh — thin bash client for the codex app-server, NO plugin in the path.
#
# WHY THIS EXISTS
#   The codex@openai-codex plugin's whole runtime is `spawn("codex",
#   ["app-server"])` inside scripts/lib/app-server.mjs (class
#   SpawnedCodexAppServerClient.initialize). Everything the plugin gives us is a
#   node wrapper around a protocol the codex binary already speaks natively.
#   Driving that protocol directly buys three things the plugin cannot:
#     * one LONG-LIVED app-server per lane instead of one per dispatch, so a
#       thread stays live between Claude turns and can be steered mid-turn;
#     * explicit `model` and `effort` on every turn/start (TurnStartParams has
#       both as first-class fields) instead of the plugin's deliberate "leave
#       unset";
#     * CODEX_HOME chosen by US, which deletes the OSCAR patch that currently
#       lives in the plugin CACHE and dies on every plugin update.
#
# ISOLATION KEY
#   CODEX_HOME. Every piece of codex state -- thread store, queue_1.sqlite,
#   the control socket at $CODEX_HOME/app-server-control/, and the daemon's
#   managed-install check at $CODEX_HOME/packages/standalone/current/codex --
#   is keyed off it. One CODEX_HOME per lane = zero thread collision, with no
#   --cwd trick and no dependence on a session id.
#   CODEX_HOME must be node-local (tmpfs): its SQLite does not survive NFS.
#   Consequence, stated plainly: a thread lives on ONE node.
#
# TRANSPORT
#   --node "" (default)  : everything runs here.
#   --node <host>        : control verbs run over `ssh <host>`; the app-server
#                          process lives on <host>. Per doctrine, ssh (not
#                          srun --overlap) is what lets a daemon reparent to
#                          PID 1 instead of being reaped.
#
# VERBS
#   up      start a long-lived app-server ws listener (prints the ws addr)
#   down    stop it by the pid file it wrote
#   ping    HTTP /readyz + /healthz against the listener
#   list    thread/list           (JSON-RPC over `codex app-server --stdio`)
#   start   thread/start          (--model/--effort honoured; no model turn)
#   steer   `codex queue --remote ws://...` into an existing thread
#   rpc     send one raw JSON-RPC request; escape hatch for any method
#   turn    ONE real model turn on the node; prints ONLY the final agent message
#   resume  one more turn on the SAME thread; prints ONLY the final agent message
#
# WHY turn/resume ARE `codex exec` AND NOT `turn/start` OVER THE WS
#   `turn/start` is a streaming JSON-RPC method: the final message arrives as one
#   event among many on a connection that must stay open for the whole turn. In
#   bash that is a coprocess whose read loop has to survive ssh, and whose failure
#   mode is a silent empty answer -- the exact failure this lane exists to remove.
#   `codex exec -o <file>` writes the final agent message and nothing else, and
#   `codex exec resume` reopens the same thread. Both key off CODEX_HOME, which is
#   the SAME per-lane store the ws app-server owns, so `thread/list` and
#   `queue --remote` see the threads `turn` creates. The ws listener is therefore
#   the lane's LIVENESS + STEERING surface; exec is the turn driver. Stated
#   plainly so nobody mistakes the ws port for the thing that runs the turn.
#
# WHAT IS NOT PROVEN FROM A LOGIN NODE
#   turn/start and turn/steer need a real model turn, which needs a compute
#   node. `steer` is proven only as far as "queue accepted and persisted".
set -uo pipefail

die() { printf 'FLEET_REFUSE: %s\n' "$*" >&2; exit 2; }

CODEX_BIN="${CODEX_BIN:-$HOME/.local/bin/codex}"
NODE=""
HOME_DIR=""
PORT=""
VERB=""
THREAD=""
MESSAGE=""
MODEL=""
EFFORT=""
CWD=""
RPC_METHOD=""
RPC_PARAMS="{}"
PROMPT_FILE=""
LOG_FILE=""
TURN_TIMEOUT="${CODEX_FLEET_TURN_TIMEOUT:-1800}"

while [ $# -gt 0 ]; do
  case "$1" in
    up|down|ping|list|start|steer|rpc|turn|resume) VERB="$1"; shift ;;
    --prompt-file) PROMPT_FILE="${2:-}"; shift 2 ;;
    --log)         LOG_FILE="${2:-}";    shift 2 ;;
    --turn-timeout) TURN_TIMEOUT="${2:-}"; shift 2 ;;
    --node)    NODE="${2:-}";      shift 2 ;;
    --home)    HOME_DIR="${2:-}";  shift 2 ;;
    --port)    PORT="${2:-}";      shift 2 ;;
    --thread)  THREAD="${2:-}";    shift 2 ;;
    --message) MESSAGE="${2:-}";   shift 2 ;;
    --model)   MODEL="${2:-}";     shift 2 ;;
    --effort)  EFFORT="${2:-}";    shift 2 ;;
    --cwd)     CWD="${2:-}";       shift 2 ;;
    --method)  RPC_METHOD="${2:-}";shift 2 ;;
    --params)  RPC_PARAMS="${2:-}";shift 2 ;;
    *) die "unknown argument $1" ;;
  esac
done

[ -n "$VERB" ] || die "give a verb: up|down|ping|list|start|steer|rpc"
[ -n "$HOME_DIR" ] || die "--home is required (absolute, node-local tmpfs, one per lane)"
case "$HOME_DIR" in /*) ;; *) die "--home must be absolute" ;; esac
case "$HOME_DIR" in
  /oscar/*|/gpfs/*|"$HOME"/*) die "--home is on NFS ($HOME_DIR); codex SQLite does not survive NFS locking" ;;
esac

PIDFILE="$HOME_DIR/codex_fleet.pid"
PORTFILE="$HOME_DIR/codex_fleet.port"

# run <shell-command-string> -- locally, or on --node over ssh.
run() {
  if [ -z "$NODE" ]; then
    bash -lc "$1"
  else
    ssh -o BatchMode=yes "$NODE" "$1"
  fi
}

# The managed-install check in `codex app-server daemon` and `codex
# remote-control` looks for $CODEX_HOME/packages/standalone/current/codex and
# refuses otherwise. Our tmpfs home has no packages/ of its own, so point it at
# the real standalone install. Verified: with this symlink `daemon start`
# returns {"status":"started",...}; without it it refuses.
seed_home() {
  run "mkdir -p '$HOME_DIR' && chmod 700 '$HOME_DIR' && \
       for f in config.toml auth.json version.json models_cache.json; do \
         [ -e \"\$HOME/.codex/\$f\" ] && cp -f \"\$HOME/.codex/\$f\" '$HOME_DIR/' 2>/dev/null; \
       done; \
       ln -sfn \"\$HOME/.codex/packages\" '$HOME_DIR/packages'; true"
}

# One JSON-RPC round trip against a freshly spawned `codex app-server --stdio`
# on the same CODEX_HOME. The thread store is CODEX_HOME state, so a stdio
# server sees exactly the threads the long-lived ws server sees.
# Both request lines travel base64-encoded so that nothing -- neither `bash -lc`
# nor ssh -- gets a second look at the JSON's quotes. Nested quoting is the one
# thing that silently returns an empty result instead of an error.
rpc_call() {
  local method="$1" params="$2" a b
  a=$(printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"codex_fleet","version":"0.1.0"}}}' | base64 -w0)
  b=$(printf '{"jsonrpc":"2.0","id":2,"method":"%s","params":%s}' "$method" "$params" | base64 -w0)
  run "{ echo $a | base64 -d; echo; sleep 2; echo $b | base64 -d; echo; sleep 6; } \
       | CODEX_HOME='$HOME_DIR' timeout 45 '$CODEX_BIN' app-server --stdio 2>/dev/null" \
    | grep -F '"id":2' | head -1
}

# run_b64 <shell-command-string> -- same as run(), but the command travels
# base64-encoded. Nested quoting through `bash -lc` and then ssh is the one thing
# here that returns an empty answer instead of an error, so nothing but base64 is
# trusted with a command that carries a prompt path, a model name and a redirect.
run_b64() {
  local b; b=$(printf '%s' "$1" | base64 -w0)
  run "echo $b | base64 -d | bash"
}

# ONE real model turn on the node. Prints ONLY the final agent message, between
# markers the NODE emits, so we never read it back across NFS (a fresh write read
# from another client can come back stale/empty).
#   $1 = "new" | "resume"
do_turn() {
  local mode="$1" inner exec_cmd
  [ -n "$MODEL" ]  || die "--model is required; never inherit the codex config default"
  [ -n "$EFFORT" ] || die "--effort is required; never inherit the codex config default"
  [ -n "$CWD" ]    || die "--cwd is required (the thread's workspace root)"
  [ -n "$PROMPT_FILE" ] || die "--prompt-file is required"
  [ -n "$LOG_FILE" ]    || die "--log is required (the full jsonl transcript; the caller never prints it)"
  case "$mode" in
    new)    exec_cmd="exec" ;;
    resume) exec_cmd="exec resume ${THREAD:---last}" ;;
  esac
  inner="set -uo pipefail
export CODEX_HOME='$HOME_DIR'
LAST=\"\$(mktemp /tmp/codex_fleet_last.XXXXXX)\"
timeout $TURN_TIMEOUT '$CODEX_BIN' $exec_cmd \\
  --model '$MODEL' -c model_reasoning_effort='$EFFORT' \\
  --cd '$CWD' --skip-git-repo-check \\
  --dangerously-bypass-approvals-and-sandbox \\
  --json -o \"\$LAST\" - < '$PROMPT_FILE' > '$LOG_FILE' 2>&1
RC=\$?
cp -f \"\$LAST\" '$LOG_FILE.last' 2>/dev/null
echo '---FLEET-FINAL-BEGIN---'
cat \"\$LAST\"
echo
echo \"---FLEET-FINAL-END rc=\$RC---\"
rm -f \"\$LAST\""
  run_b64 "$inner"
}

case "$VERB" in
  up)
    [ -n "$PORT" ] || die "--port is required for up"
    seed_home
    run "CODEX_HOME='$HOME_DIR' setsid nohup '$CODEX_BIN' app-server \
           --listen ws://127.0.0.1:$PORT >'$HOME_DIR/app-server.log' 2>&1 </dev/null & \
         echo \$! > '$PIDFILE'; echo $PORT > '$PORTFILE'"
    sleep 4
    run "curl -sS -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/readyz" \
      | grep -q '^200$' || die "listener did not come up on port $PORT (see $HOME_DIR/app-server.log)"
    printf 'FLEET_UP node=%s ws=ws://127.0.0.1:%s home=%s\n' "${NODE:-local}" "$PORT" "$HOME_DIR"
    ;;
  down)
    run "[ -f '$PIDFILE' ] && kill \$(cat '$PIDFILE') 2>/dev/null; rm -f '$PIDFILE'; true"
    printf 'FLEET_DOWN node=%s home=%s\n' "${NODE:-local}" "$HOME_DIR"
    ;;
  ping)
    [ -n "$PORT" ] || PORT="$(run "cat '$PORTFILE' 2>/dev/null")"
    [ -n "$PORT" ] || die "no --port and no $PORTFILE"
    printf 'readyz=%s healthz=%s\n' \
      "$(run "curl -sS -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/readyz")" \
      "$(run "curl -sS -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/healthz")"
    ;;
  list)
    rpc_call "thread/list" '{"pageSize":20}'
    ;;
  start)
    [ -n "$MODEL" ]  || die "--model is required; never inherit the codex config default"
    [ -n "$EFFORT" ] || die "--effort is required; never inherit the codex config default"
    [ -n "$CWD" ]    || die "--cwd is required (the thread's workspace root)"
    rpc_call "thread/start" \
      "{\"cwd\":\"$CWD\",\"model\":\"$MODEL\",\"config\":{\"model_reasoning_effort\":\"$EFFORT\"},\"sandbox\":\"danger-full-access\",\"approvalPolicy\":\"never\"}"
    ;;
  steer)
    [ -n "$THREAD" ]  || die "--thread is required (uuid or exact thread name)"
    [ -n "$MESSAGE" ] || die "--message is required"
    [ -n "$PORT" ] || PORT="$(run "cat '$PORTFILE' 2>/dev/null")"
    [ -n "$PORT" ] || die "no --port and no $PORTFILE"
    run "CODEX_HOME='$HOME_DIR' timeout 60 '$CODEX_BIN' queue \
           --remote ws://127.0.0.1:$PORT --thread '$THREAD' --message '$MESSAGE' 2>&1" \
      | grep -v '^WARNING'
    ;;
  rpc)
    [ -n "$RPC_METHOD" ] || die "--method is required for rpc"
    rpc_call "$RPC_METHOD" "$RPC_PARAMS"
    ;;
  turn)
    do_turn new
    ;;
  resume)
    do_turn resume
    ;;
esac
