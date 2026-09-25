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
  # A baseline that is short a language is committed and read for weeks. The
  # bullets above cannot show that, so the engine's own account goes here.
  if [ -n "${DIAGNOSTICS_MD:-}" ] && [ -s "${DIAGNOSTICS_MD}" ]; then
    echo
    echo "#### Analysis diagnostics"
    echo
    cat "${DIAGNOSTICS_MD}"
  fi
} >> "$GITHUB_STEP_SUMMARY"
