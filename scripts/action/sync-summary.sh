#!/usr/bin/env bash
# Appends sync analysis and delivery results to the GitHub job summary.
set -euo pipefail
{
  echo "### CodeBoarding Sync"
  [ -z "${FAILURE_REASON:-}" ] || echo "- **Failed:** ${FAILURE_REASON}"
  if [ -n "${MODE:-}" ]; then
    echo "- Analysis: ${MODE}"
    echo "- Analysis artifacts: ${FILES:-0}"
    echo "- Delivered: ${COMMITTED:-false}"
    echo "- Saved to: ${SAVED_TO}"
  fi
} >> "$GITHUB_STEP_SUMMARY"
