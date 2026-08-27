#!/usr/bin/env bash
# Runs one command with the resolved provider credentials, then removes them.
set -euo pipefail
AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
cleanup() {
  if [ -s "$AUTH_DIR/relay.pid" ]; then
    kill "$(cat "$AUTH_DIR/relay.pid")" 2>/dev/null || true
  fi
  rm -rf "$AUTH_DIR"
}
trap cleanup EXIT

if [ ! -s "$AUTH_DIR/tier" ] || [ ! -s "$AUTH_DIR/provider-name" ]; then
  echo "::error::CodeBoarding analysis credentials are unavailable."
  exit 1
fi

# Core picks a provider from whatever its environment happens to hold, so a variable the
# caller exported for something else could select a provider this run never asked for.
# The list of what to strip is written by the resolver from the provider table, rather
# than kept here, so it cannot fall behind the pinned engine.
if [ -s "$AUTH_DIR/foreign-envs" ]; then
  # `|| [ -n "$selector" ]` so a file whose last line is unterminated still strips that
  # entry: `read` reports failure at EOF even when it read a partial line, and losing the
  # last variable silently is exactly how a second provider stays configured.
  while IFS= read -r selector || [ -n "$selector" ]; do
    [ -z "$selector" ] || unset "$selector"
  done < "$AUTH_DIR/foreign-envs"
fi

for file in "$AUTH_DIR/env"/*; do
  [ -e "$file" ] || continue
  export "${file##*/}=$(cat "$file")"
done

export CODEBOARDING_SOURCE=github_action
unset ACTIONS_ID_TOKEN_REQUEST_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN
if [ -n "${MODEL:-}" ]; then
  export AGENT_MODEL="$MODEL"
  export PARSING_MODEL="$MODEL"
fi
[ -z "${AGENT_MODEL_INPUT:-}" ] || export AGENT_MODEL="$AGENT_MODEL_INPUT"
[ -z "${PARSING_MODEL_INPUT:-}" ] || export PARSING_MODEL="$PARSING_MODEL_INPUT"
"$@"
