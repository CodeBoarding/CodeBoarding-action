#!/usr/bin/env bash
# Runs one command with scoped provider credentials, then removes those credentials.
set -euo pipefail
AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
PROVIDER_FILE="$AUTH_DIR/provider-name"
PROVIDER_ENV_FILE="$AUTH_DIR/provider-env"
PROVIDER_KEY_FILE="$AUTH_DIR/provider-key"
cleanup() {
  if [ -s "$AUTH_DIR/relay.pid" ]; then
    kill "$(cat "$AUTH_DIR/relay.pid")" 2>/dev/null || true
  fi
  rm -rf "$AUTH_DIR"
}
trap cleanup EXIT
if [ ! -s "$PROVIDER_FILE" ] || [ ! -s "$PROVIDER_ENV_FILE" ]; then
  echo "::error::CodeBoarding analysis credentials are unavailable."
  exit 1
fi
PROVIDER="$(cat "$PROVIDER_FILE")"
case "$PROVIDER" in
  ollama) [ -n "${OLLAMA_BASE_URL:-${OLLAMA_HOST:-}}" ] || { echo "::error::ollama requires OLLAMA_BASE_URL or OLLAMA_HOST." >&2; exit 1; } ;;
  litellm) [ -n "${LITELLM_BASE_URL:-}" ] || { echo "::error::litellm requires LITELLM_BASE_URL." >&2; exit 1; } ;;
esac

# Core selects a provider from its environment. Remove inherited selectors for
# every provider except the one explicitly requested by this action invocation.
selectors=(OPENAI_API_KEY OPENAI_BASE_URL VERCEL_API_KEY VERCEL_BASE_URL ANTHROPIC_API_KEY GOOGLE_API_KEY
  AWS_BEARER_TOKEN_BEDROCK CEREBRAS_API_KEY OLLAMA_API_KEY OLLAMA_BASE_URL OLLAMA_HOST DEEPSEEK_API_KEY
  DEEPSEEK_BASE_URL GLM_API_KEY GLM_BASE_URL KIMI_API_KEY KIMI_BASE_URL OPENROUTER_API_KEY LITELLM_API_KEY LITELLM_BASE_URL)
for selector in "${selectors[@]}"; do
  case "$PROVIDER:$selector" in
    openai:OPENAI_*|vercel:VERCEL_*|anthropic:ANTHROPIC_*|google:GOOGLE_*|aws:AWS_*|aws_bedrock:AWS_*|cerebras:CEREBRAS_*|ollama:OLLAMA_*|deepseek:DEEPSEEK_*|glm:GLM_*|kimi:KIMI_*|openrouter:OPENROUTER_*|litellm:LITELLM_*) ;;
    *) unset "$selector" ;;
  esac
done

PROVIDER_ENV="$(cat "$PROVIDER_ENV_FILE")"
if [ -s "$PROVIDER_KEY_FILE" ]; then
  export "$PROVIDER_ENV=$(cat "$PROVIDER_KEY_FILE")"
fi
if [ -s "$AUTH_DIR/base-url" ]; then
  OPENROUTER_BASE_URL="$(cat "$AUTH_DIR/base-url")"
  export OPENROUTER_BASE_URL
fi

export CODEBOARDING_SOURCE=github_action
unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN
if [ -n "${MODEL:-}" ]; then
  export AGENT_MODEL="$MODEL"
  export PARSING_MODEL="$MODEL"
fi
[ -z "${AGENT_MODEL_INPUT:-}" ] || export AGENT_MODEL="$AGENT_MODEL_INPUT"
[ -z "${PARSING_MODEL_INPUT:-}" ] || export PARSING_MODEL="$PARSING_MODEL_INPUT"
"$@"
