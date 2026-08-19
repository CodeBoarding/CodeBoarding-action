#!/usr/bin/env bash
# Packages both analyses and PR metadata, then outputs the artifact upload directory.
set -euo pipefail
mkdir -p "${RUNNER_TEMP}/cb-review-artifact"
cp "$ANALYSIS_PATH" "${RUNNER_TEMP}/cb-review-artifact/analysis.json"
# The base graph is published separately, named for the commit it describes, so
# ten runs on one pull request store it once rather than ten times. metadata
# carries the name to look it up by.
# The engine writes this next to the analysis it came from. Ship it when present
# so a pull request's warnings are readable without rerunning the analysis.
HEALTH_REPORT="$(dirname "$ANALYSIS_PATH")/health/health_report.json"
[ ! -f "$HEALTH_REPORT" ] || cp "$HEALTH_REPORT" "${RUNNER_TEMP}/cb-review-artifact/health_report.json"

# base_sha stays the event's base branch tip for consumers that key on it;
# merge_base_sha records the commit the diagram actually compared against.
# pr_base_sha carries the same value under the name the webview already reads:
# its lookup is base_commit_sha || pr_base_sha || base_sha, so without it the
# webview silently falls through to the branch tip and compares against a base
# this review never used.
jq -n \
  --arg mode "$ANALYSIS_MODE" \
  --arg base_sha "$BASE_SHA" \
  --arg merge_base_sha "$MERGE_BASE_SHA" \
  --arg merge_base_resolved "$MERGE_BASE_RESOLVED" \
  --arg head_sha "$HEAD_SHA" \
  --arg pr_number "$PR_NUMBER" \
  --arg seed_source "$SEED_SOURCE" \
  --arg chain_depth "$CHAIN_DEPTH" \
  --arg base_artifact "$BASE_ARTIFACT_NAME" \
  '{mode: $mode, base_sha: $base_sha, merge_base_sha: $merge_base_sha, pr_base_sha: $merge_base_sha,
    merge_base_resolved: $merge_base_resolved, head_sha: $head_sha,
    pr_number: $pr_number, seed_source: $seed_source, chain_depth: $chain_depth,
    base_artifact: $base_artifact}' \
  > "${RUNNER_TEMP}/cb-review-artifact/metadata.json"
echo "artifact_dir=${RUNNER_TEMP}/cb-review-artifact" >> "$GITHUB_OUTPUT"
