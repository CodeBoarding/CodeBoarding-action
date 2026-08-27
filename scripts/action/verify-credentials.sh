#!/usr/bin/env bash
# Resolves the run's LLM credentials before anything expensive happens.
#
# Runs ahead of the checkout and the engine install so a misconfigured workflow fails in
# seconds with the input to fix named, rather than after a minute of setup or, worse,
# succeeding quietly on credentials the workflow did not ask for.
set -euo pipefail

AUTH_DIR="${RUNNER_TEMP}/codeboarding-auth"
umask 077
rm -rf "$AUTH_DIR"

# Mask before resolving: a value that never reaches the log cannot leak from a later
# failure. The cleaned forms are masked again below, since trimming a pasted wrapper
# produces a string GitHub has not been told about yet.
while IFS= read -r var; do
  [ -z "${!var:-}" ] || echo "::add-mask::${!var}"
done < <(compgen -v | grep -E '^CB_IN_.*_API_KEY$' || true)
[ -z "${CB_IN_LICENSE_KEY:-}" ] || echo "::add-mask::${CB_IN_LICENSE_KEY}"

set +e
plan="$(python3 "${ACTION_PATH}/scripts/action/credential_check.py" --auth-dir "$AUTH_DIR")"
resolved=$?
set -e

field() { PLAN="$plan" python3 -c '
import json, os, sys
print(json.loads(os.environ["PLAN"]).get(sys.argv[1], ""))' "$1"; }

tier="$(field tier)"
provider="$(field provider)"
error="$(field error)"
message="$(field message)"
details="$(field details)"

backend_id=""
[ ! -s "$AUTH_DIR/backend-id" ] || backend_id="$(cksum < "$AUTH_DIR/backend-id" | cut -d' ' -f1)"

{
  echo "tier=$tier"
  echo "backend_id=$backend_id"
  echo "provider=$provider"
  echo "error=$error"
  echo "message<<CB_EOF"
  echo "$message"
  echo "CB_EOF"
  # Markdown, for the surfaces that can render it. The annotation gets `message`, which
  # is one line, because a workflow command cannot carry newlines.
  echo "details<<CB_EOF"
  echo "$details"
  echo "CB_EOF"
} >> "$GITHUB_OUTPUT"

if [ "$resolved" -ne 0 ]; then
  echo "::error title=CodeBoarding LLM configuration::${message}"
  {
    echo "### CodeBoarding could not start"
    echo
    echo "$details"
    echo
    echo "No analysis ran, and no CodeBoarding hosted usage was consumed."
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  rm -rf "$AUTH_DIR"
  exit 1
fi

# The trimmed values are what the analysis actually exports; mask those too. Endpoints
# and regions are configuration rather than credentials and stay readable, so a wrong
# one can be spotted in the log.
if [ -d "$AUTH_DIR/env" ]; then
  for file in "$AUTH_DIR/env"/*; do
    [ -e "$file" ] || continue
    case "${file##*/}" in
      *_BASE_URL|*_HOST|AWS_DEFAULT_REGION) continue ;;
    esac
    echo "::add-mask::$(cat "$file")"
  done
fi
[ ! -s "$AUTH_DIR/license.txt" ] || echo "::add-mask::$(cat "$AUTH_DIR/license.txt")"

# Written by the check, not restated here. The same phrase feeds the summary, so the log
# and the summary cannot end up claiming different things -- which they did: this said
# "your own openai key" for an endpoint-only run that resolved no key at all.
field headline
# The rows come from the check, which is the only thing that knows what it resolved.
{
  echo "### CodeBoarding configuration"
  echo
  echo "| | |"
  echo "|---|---|"
  field summary
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
