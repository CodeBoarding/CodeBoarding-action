#!/usr/bin/env bash
# Builds the failure body shared by the PR comment and the job summary.
set -euo pipefail
BODY="${RUNNER_TEMP}/review-failure.md"
{
  printf '### CodeBoarding review · failed\n\n'
  if [ -n "${FAILURE_REASON:-}" ]; then
    printf '%s\n\n' "$FAILURE_REASON"
  fi
  printf 'See the [workflow logs](%s/%s/actions/runs/%s).\n' \
    "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID"
} > "$BODY"
echo "path=$BODY" >> "$GITHUB_OUTPUT"
cat "$BODY" >> "$GITHUB_STEP_SUMMARY"
