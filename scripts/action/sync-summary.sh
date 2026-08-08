#!/usr/bin/env bash
# Appends sync analysis and delivery results to the GitHub job summary.
set -euo pipefail
{
  echo "### CodeBoarding Sync"
  echo "- Analysis: ${MODE}"
  echo "- Analysis artifacts: ${FILES:-0}"
  echo "- Delivered: ${COMMITTED:-false}"
  echo "- Strategy: ${STRATEGY}"
  if [ -n "${PR_URL:-}" ]; then
    echo "- Sync PR: ${PR_URL}"
  fi
} >> "$GITHUB_STEP_SUMMARY"
