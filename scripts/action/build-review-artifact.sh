#!/usr/bin/env bash
# Packages both analyses and PR metadata, then outputs the artifact upload directory.
set -euo pipefail
mkdir -p "${RUNNER_TEMP}/cb-review-artifact"
cp "$ANALYSIS_PATH" "${RUNNER_TEMP}/cb-review-artifact/analysis.json"
# Ship the graph the diagram was measured against, so a reader can reproduce the
# comparison without guessing which commit it belongs to. Reading the default
# branch's committed baseline instead would drift from the merge base exactly as
# the review itself used to.
cp "$BASE_ANALYSIS_PATH" "${RUNNER_TEMP}/cb-review-artifact/base_analysis.json"
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
  '{mode: $mode, base_sha: $base_sha, merge_base_sha: $merge_base_sha, pr_base_sha: $merge_base_sha,
    merge_base_resolved: $merge_base_resolved, head_sha: $head_sha,
    pr_number: $pr_number, seed_source: $seed_source, chain_depth: $chain_depth}' \
  > "${RUNNER_TEMP}/cb-review-artifact/metadata.json"
echo "artifact_dir=${RUNNER_TEMP}/cb-review-artifact" >> "$GITHUB_OUTPUT"
