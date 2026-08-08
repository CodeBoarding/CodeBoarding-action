#!/usr/bin/env bash
set -euo pipefail
COMPONENT_NOUN="components"
if [ "$N_CHANGED" = "1" ]; then
  COMPONENT_NOUN="component"
fi

RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
WEBVIEW_URL="https://app.codeboarding.org/${GITHUB_REPOSITORY}/pull/${PR_NUMBER}?run=${GITHUB_RUN_ID}"

BODY="${RUNNER_TEMP}/review-comment.md"
printf '### CodeBoarding review\n\n**Status:** %s changed %s\n' "$N_CHANGED" "$COMPONENT_NOUN" > "$BODY"
printf '\nSee the full change in [CodeBoarding](%s).\n' "$WEBVIEW_URL" >> "$BODY"
{
  printf '\n'
  cat "$DIAGRAM"
  printf '\n\n<sub>'
  if [ -n "$ARTIFACT_URL" ]; then
    printf '[download artifacts](%s) · ' "$ARTIFACT_URL"
  fi
  printf 'run [%s](%s)</sub>\n' "$GITHUB_RUN_ID" "$RUN_URL"
} >> "$BODY"
echo "path=$BODY" >> "$GITHUB_OUTPUT"
