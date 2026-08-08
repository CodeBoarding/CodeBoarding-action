#!/usr/bin/env bash
set -euo pipefail
AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
HOSTED_PROXY_URL="https://auduihjmm4b735zci7vyabuikq0hppqn.lambda-url.us-east-1.on.aws"
umask 077
rm -rf "$AUTH_DIR"
mkdir -p "$AUTH_DIR"
provider="$(printf '%s' "${LLM_PROVIDER:-openrouter}" | tr '[:upper:]-' '[:lower:]_' | tr -cd 'a-z0-9_')"
[ -n "$provider" ] || { echo "::error::llm_provider is empty."; exit 1; }
printf '%s' "$provider" > "$AUTH_DIR/provider-name"
if [ -n "${LLM_API_KEY:-}" ] || [ "$provider" != openrouter ]; then
  case "$provider" in
    aws|aws_bedrock) provider_env="AWS_BEARER_TOKEN_BEDROCK" ;;
    *) provider_env="$(printf '%s' "$provider" | tr '[:lower:]' '[:upper:]')_API_KEY" ;;
  esac
  printf '%s' "$provider_env" > "$AUTH_DIR/provider-env"
  if [ -n "${LLM_API_KEY:-}" ]; then
    echo "::add-mask::$LLM_API_KEY"
    printf '%s' "$LLM_API_KEY" > "$AUTH_DIR/provider-key"
  fi
  echo "Using direct $provider credentials."
  exit 0
fi
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
  echo "::error::Missing OIDC token. Add permissions: id-token: write." && exit 1
fi

READY="$AUTH_DIR/ready-port"
PID="$AUTH_DIR/relay.pid"
LOG="$AUTH_DIR/relay.log"
LICENSE_FILE="$AUTH_DIR/license.txt"
: > "$LICENSE_FILE"
if [ -n "${LICENSE_KEY:-}" ]; then
  echo "::add-mask::$LICENSE_KEY"
  printf '%s' "$LICENSE_KEY" > "$LICENSE_FILE"
fi

RELAY_ARGS=(--upstream-base-url "$HOSTED_PROXY_URL" --ready-file "$READY")
if [ -s "$LICENSE_FILE" ]; then
  RELAY_ARGS+=(--license-file "$LICENSE_FILE")
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
  exit 1
fi

PORT="$(cat "$READY")"
printf '%s' "OPENROUTER_API_KEY" > "$AUTH_DIR/provider-env"
printf '%s' "github-actions-oidc-relay" > "$AUTH_DIR/provider-key"
printf '%s' "http://127.0.0.1:$PORT" > "$AUTH_DIR/base-url"
