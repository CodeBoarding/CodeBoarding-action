#!/usr/bin/env bash
# Produces the analysis of the synced branch's head, continuing from the nearest
# saved analysis unless force_full asks for a fresh one. First match wins:
#
#   1. an entry on the baseline branch for this commit or an ancestor
#   2. the analysis committed on the branch
#   3. the saved analysis of this commit or the nearest ancestor
#   4. nothing: analyzed from scratch
set -euo pipefail
source "$(dirname "$0")/seed-sources.sh"
state="$RUNNER_TEMP/codeboarding-sync/analysis"
rm -rf "$RUNNER_TEMP/codeboarding-sync"
head_sha="$(git -C "$CHECKOUT_DIR" rev-parse HEAD 2>/dev/null || true)"

if [ "$(printf '%s' "${FORCE_FULL:-false}" | tr '[:upper:]' '[:lower:]')" = true ] || ! {
  from_codeboarding_baseline "${REPOSITORY:-}" "$head_sha" "$state" "$CHECKOUT_DIR" ||
    from_committed_baseline "$CHECKOUT_DIR" "$state" ||
    from_ancestor_artifact "${REPOSITORY:-}" "$head_sha" "$state" true "$CHECKOUT_DIR"
}; then
  keep_user_config "$CHECKOUT_DIR" "$state"
fi

# Core never sees the token.
unset GIT_TOKEN
result="$("$ACTION_PATH/scripts/action/analyze.sh" sync "$CHECKOUT_DIR" "$state")"
# Sync already computes the graph every review of this branch compares against,
# so publish it instead of making the first pull request recompute it.
stage "$state" base
{
  echo "$result"
  echo "analysis_dir=$state"
} >> "$GITHUB_OUTPUT"
