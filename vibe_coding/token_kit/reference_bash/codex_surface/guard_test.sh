#!/usr/bin/env bash
# guard_test.sh — the verbs and protocol fields codex_fleet.sh depends on must
# still exist. A codex update that deletes `queue`, renames `agents`, drops
# `app-server --listen`, or removes `effort` from TurnStartParams must break
# HERE, loudly, on a login node, in under a minute -- not at 3am in a night run.
#
# Runs on login009. Needs no Slurm allocation and no model turn.
#   ./guard_test.sh              -> GUARD PASS / GUARD FAIL <n>
#   GUARD_SKIP_LIVE=1 ./guard_test.sh   -> static checks only (no process spawn)
set -uo pipefail

CODEX_BIN="${CODEX_BIN:-$HOME/.local/bin/codex}"
GUARD_HOME="${GUARD_HOME:-/tmp/codex-surface-guard-$USER}"
FAIL=0

ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }

[ -x "$CODEX_BIN" ] || { printf 'GUARD FAIL 1\n  FAIL  no codex binary at %s\n' "$CODEX_BIN"; exit 1; }

# The binary refuses to create PATH aliases under a tmpfs CODEX_HOME and says so
# on stderr every run; that WARNING is noise, not a failure.
HELP="$("$CODEX_BIN" --help 2>/dev/null)"

printf '== subcommands ==\n'
for v in agents queue resume fork archive mcp-server app-server remote-control exec; do
  if printf '%s' "$HELP" | grep -qE "^[[:space:]]+$v[[:space:]]"; then ok "codex $v"; else bad "codex $v is gone"; fi
done

printf '== app-server flags ==\n'
AS_HELP="$("$CODEX_BIN" app-server --help 2>/dev/null)"
for f in -- --listen --ws-auth --ws-token-file --stdio; do
  [ "$f" = "--" ] && continue
  if printf '%s' "$AS_HELP" | grep -q -- "$f"; then ok "app-server $f"; else bad "app-server $f is gone"; fi
done

printf '== remote transport flags ==\n'
Q_HELP="$("$CODEX_BIN" queue --help 2>/dev/null)"
for f in --remote --remote-auth-token-env --thread --message; do
  if printf '%s' "$Q_HELP" | grep -q -- "$f"; then ok "queue $f"; else bad "queue $f is gone"; fi
done

printf '== protocol methods and fields ==\n'
SCHEMA_DIR="$(mktemp -d /tmp/codex-surface-guard-schema.XXXXXX)"
if CODEX_HOME="$GUARD_HOME" "$CODEX_BIN" app-server generate-json-schema -o "$SCHEMA_DIR" >/dev/null 2>&1; then
  BUNDLE="$SCHEMA_DIR/codex_app_server_protocol.v2.schemas.json"
  for m in thread/list thread/start thread/fork thread/resume thread/name/set turn/start turn/steer turn/interrupt; do
    if grep -qF "\"$m\"" "$BUNDLE" 2>/dev/null; then ok "method $m"; else bad "method $m is gone"; fi
  done
  # Explicit model + effort per turn is the whole reason we bypass the plugin.
  for k in model effort; do
    if grep -q "\"$k\"" "$SCHEMA_DIR/v2/TurnStartParams.json" 2>/dev/null; then
      ok "TurnStartParams.$k"
    else
      bad "TurnStartParams.$k is gone"
    fi
  done
else
  bad "app-server generate-json-schema refused"
fi
rm -rf "$SCHEMA_DIR"

printf '== exec turn driver ==\n'
# `turn`/`resume` are `codex exec`, not turn/start over the ws (see the WHY block
# in codex_fleet.sh). These four flags ARE that contract: lose any one and a turn
# either blocks on a prompt, writes its answer nowhere, or runs at the config's
# default effort.
E_HELP="$("$CODEX_BIN" exec --help 2>/dev/null)"
for f in --output-last-message --json --model --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox; do
  if printf '%s' "$E_HELP" | grep -q -- "$f"; then ok "exec $f"; else bad "exec $f is gone"; fi
done
R_HELP="$("$CODEX_BIN" exec resume --help 2>/dev/null)"
printf '%s' "$R_HELP" | grep -q -- '--last' && ok "exec resume --last" || bad "exec resume --last is gone"

printf '== serve script + endpoint contract ==\n'
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVE="$D/codex_fleet_serve.sh"
if [ -x "$SERVE" ]; then ok "serve script present"; else bad "serve script missing at $SERVE"; fi
bash -n "$SERVE" && ok "serve script parses" || bad "serve script does not parse"
# every field the router reads must be in the writer, or the contract is fiction
for k in '"url"' '"host"' '"port"' '"slurm_jobid"' '"started"' '"codex_version"' '"codex_home"'; do
  grep -qF -- "$k" "$SERVE" || bad "endpoint json has no $k field"
done
ok "endpoint json carries every contract field"
# liveness is mtime: the refresh must be well under the consumer's max age
REF="$(sed -n 's/^REFRESH="\${REFRESH:-\([0-9]*\)}".*/\1/p' "$SERVE")"
MAXAGE="$(sed -n 's/^MAX_AGE="\${CODEX_FLEET_MAX_AGE:-\([0-9]*\)}".*/\1/p' "$D/../codex_native/codex_lane_dispatch.sh")"
if [ -n "$REF" ] && [ -n "$MAXAGE" ] && [ "$((REF*2))" -le "$MAXAGE" ]; then
  ok "refresh ${REF}s leaves margin under the dispatcher's ${MAXAGE}s max age"
else
  bad "refresh '$REF' vs max age '$MAXAGE': liveness has no margin"
fi
# loopback only, and the endpoint removed on every exit path
grep -qF 'ws://127.0.0.1:$PORT' "$SERVE" && ok "listener binds loopback only" \
  || bad "serve script does not bind ws://127.0.0.1 (an open port on a shared node is reachable by other users)"
grep -qF 'trap' "$SERVE" && grep -qF 'rm -f "$ENDPOINT"' "$SERVE" \
  && ok "endpoint removed on exit and on the app-server dying" \
  || bad "serve script can leave a stale endpoint behind"
# the server must refuse a login node and an NFS CODEX_HOME
OUTS="$(bash "$SERVE" 2>&1); true"
case "$(hostname)" in
  login*) printf '%s' "$OUTS" | grep -q 'login node' && ok "serve refuses to run on a login node" \
            || bad "serve did NOT refuse on a login node: $OUTS" ;;
esac
OUTS="$(CODEX_FLEET_HOME="$HOME/.codex-guard" bash "$SERVE" 2>&1); true"
printf '%s' "$OUTS" | grep -qE 'login node|NFS' && ok "serve refuses an NFS CODEX_HOME or a login node first" \
  || bad "serve accepted an NFS CODEX_HOME: $OUTS"

if [ "${GUARD_SKIP_LIVE:-0}" != "1" ]; then
  printf '== live round trip ==\n'
  mkdir -p "$GUARD_HOME" && chmod 700 "$GUARD_HOME"
  for f in config.toml auth.json version.json models_cache.json; do
    [ -e "$HOME/.codex/$f" ] && cp -f "$HOME/.codex/$f" "$GUARD_HOME/$f" 2>/dev/null
  done
  OUT="$(
    { printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"guard","version":"0.1.0"}}}\n'
      sleep 2
      printf '{"jsonrpc":"2.0","id":2,"method":"thread/list","params":{"pageSize":1}}\n'
      sleep 5
    } | CODEX_HOME="$GUARD_HOME" timeout 40 "$CODEX_BIN" app-server --stdio 2>/dev/null
  )"
  printf '%s' "$OUT" | grep -q '"codexHome"'  && ok "initialize answered" || bad "initialize did not answer"
  printf '%s' "$OUT" | grep -q '"nextCursor"' && ok "thread/list answered" || bad "thread/list did not answer"
fi

if [ "$FAIL" -eq 0 ]; then printf 'GUARD PASS\n'; exit 0; fi
printf 'GUARD FAIL %d\n' "$FAIL"; exit 1
