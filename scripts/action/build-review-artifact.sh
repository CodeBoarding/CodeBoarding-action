#!/usr/bin/env bash
set -euo pipefail
mkdir -p "${RUNNER_TEMP}/cb-review-artifact"
cp "$ANALYSIS_PATH" "${RUNNER_TEMP}/cb-review-artifact/analysis.json"
jq -n \
  --arg mode "$ANALYSIS_MODE" \
  --arg base_sha "$BASE_SHA" \
  --arg head_sha "$HEAD_SHA" \
  --arg pr_number "$PR_NUMBER" \
  '{mode: $mode, base_sha: $base_sha, head_sha: $head_sha, pr_number: $pr_number}' \
  > "${RUNNER_TEMP}/cb-review-artifact/metadata.json"
echo "artifact_dir=${RUNNER_TEMP}/cb-review-artifact" >> "$GITHUB_OUTPUT"
