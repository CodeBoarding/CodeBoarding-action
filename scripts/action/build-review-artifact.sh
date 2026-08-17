#!/usr/bin/env bash
# Packages head analysis and PR metadata, then outputs the artifact upload directory.
set -euo pipefail
mkdir -p "${RUNNER_TEMP}/cb-review-artifact"
cp "$ANALYSIS_PATH" "${RUNNER_TEMP}/cb-review-artifact/analysis.json"
# base_sha stays the event's base branch tip for consumers that key on it;
# merge_base_sha records the commit the diagram actually compared against.
jq -n \
  --arg mode "$ANALYSIS_MODE" \
  --arg base_sha "$BASE_SHA" \
  --arg merge_base_sha "$MERGE_BASE_SHA" \
  --arg head_sha "$HEAD_SHA" \
  --arg pr_number "$PR_NUMBER" \
  --arg seed_source "$SEED_SOURCE" \
  --arg chain_depth "$CHAIN_DEPTH" \
  '{mode: $mode, base_sha: $base_sha, merge_base_sha: $merge_base_sha, head_sha: $head_sha,
    pr_number: $pr_number, seed_source: $seed_source, chain_depth: $chain_depth}' \
  > "${RUNNER_TEMP}/cb-review-artifact/metadata.json"
echo "artifact_dir=${RUNNER_TEMP}/cb-review-artifact" >> "$GITHUB_OUTPUT"
