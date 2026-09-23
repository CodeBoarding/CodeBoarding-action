#!/usr/bin/env bash
# Starts the hosted credential relay when the resolved plan calls for it.
#
# The plan itself was decided by verify-credentials.sh before the checkout; this step only
# does the part that needs the engine's Python present. A direct-provider run has
# nothing to do here: its key never leaves the runner.
set -euo pipefail
AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
# One URL for the model calls here and the run's start and finish in run_meter.py.
# CODEBOARDING_PROXY_URL points a workflow at the dev stack; it only redirects that
# workflow's own OIDC tokens, so it grants nothing.
HOSTED_PROXY_URL="${CODEBOARDING_PROXY_URL:-$(cat "$ACTION_PATH/scripts/action/hosted-proxy-url")}"
umask 077

if [ ! -s "$AUTH_DIR/tier" ]; then
  echo "::error::CodeBoarding analysis credentials are unavailable; the configuration step did not run."
  exit 1
fi
TIER="$(cat "$AUTH_DIR/tier")"

case "$TIER" in
  hosted|license) ;;
  *) echo "Using direct $(cat "$AUTH_DIR/provider-name") credentials."; exit 0 ;;
esac

READY="$AUTH_DIR/ready-port"
PID="$AUTH_DIR/relay.pid"
LOG="$AUTH_DIR/relay.log"

RELAY_ARGS=(--upstream-base-url "$HOSTED_PROXY_URL" --ready-file "$READY")
if [ -s "$AUTH_DIR/license.txt" ]; then
  RELAY_ARGS+=(--license-file "$AUTH_DIR/license.txt")
fi
# Written by the preflight when the proxy gave this run an id; read per request.
RELAY_ARGS+=(--run-id-file "$AUTH_DIR/run-id")
# Outside the auth directory, which goes with the analysis step: the wall outlives it.
RELAY_ARGS+=(--wall-file "$RUNNER_TEMP/codeboarding-wall/wall.json")

python3 "$ACTION_PATH/scripts/oidc_relay.py" "${RELAY_ARGS[@]}" > "$LOG" 2>&1 &
echo $! > "$PID"

for _ in {1..60}; do
  [ -s "$READY" ] && break
  sleep 0.25
done

if [ ! -s "$READY" ]; then
  echo "::error::OIDC relay did not start." >&2
  cat "$LOG" >&2 || true
  kill "$(cat "$PID")" 2>/dev/null || true
  wait "$(cat "$PID")" 2>/dev/null || true
  rm -rf "$AUTH_DIR"
  exit 1
fi

PORT="$(cat "$READY")"
mkdir -p "$AUTH_DIR/env"
printf '%s' "github-actions-oidc-relay" > "$AUTH_DIR/env/OPENROUTER_API_KEY"
printf '%s' "http://127.0.0.1:$PORT" > "$AUTH_DIR/env/OPENROUTER_BASE_URL"
echo "Using CodeBoarding hosted credentials ($TIER)."
