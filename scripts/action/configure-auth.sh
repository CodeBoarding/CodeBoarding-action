#!/usr/bin/env bash
# Starts the hosted credential relay when the resolved plan calls for it.
#
# The plan itself was decided by preflight-llm.sh before the checkout; this step only
# does the part that needs the engine's Python present. A direct-provider run has
# nothing to do here: its key never leaves the runner.
set -euo pipefail
AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
HOSTED_PROXY_URL="https://auduihjmm4b735zci7vyabuikq0hppqn.lambda-url.us-east-1.on.aws"
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
