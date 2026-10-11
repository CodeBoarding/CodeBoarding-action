#!/usr/bin/env bash
# Produces the analysis of the review's merge base, the graph the pull request is
# compared against. First match wins:
#
#   1. the published analysis of the merge base (fetched by the step before)  used as is
#   2. an entry on the baseline branch for the merge base or an ancestor     \
#   3. the analysis committed on the branch at the merge base                  > caught up to the merge base
#   4. the saved analysis of the nearest ancestor                             /
#   5. nothing                                                                  built from scratch
#
# Outputs base_analysis_path, and publish_base when this run produced a base
# other pull requests forking there should reuse.
set -euo pipefail
source "$(dirname "$0")/seed-sources.sh"
work="$RUNNER_TEMP/codeboarding-review"
checkout="$work/base" state="$work/base-state"
rm -rf "$work"
mkdir -p "$work"
token="${GIT_TOKEN:-}"

progress() {
  GH_TOKEN="$token" GH_ENTERPRISE_TOKEN="$token" \
    "$ACTION_PATH/scripts/action/update-review-progress.sh" "$1" >/dev/null 2>&1 || true
}

publish_base=true
if compatible_state "${BASE_DIR:-}"; then
  cp -a "$BASE_DIR" "$state"
  echo "::notice::Using the published analysis of ${REVIEW_BASE_SHA:0:7}."
  # Only a base about to expire is published again: a review artifact references
  # it by id for its whole retention.
  publish_base="${RENEW_BASE:-false}"
else
  fetch_commit "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA"
  git -C "$CHECKOUT_DIR" worktree add --detach "$checkout" "$REVIEW_BASE_SHA" >/dev/null
  if from_codeboarding_baseline "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA" "$state" "$checkout" ||
    from_committed_baseline "$checkout" "$state" ||
    from_ancestor_artifact "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA" "$state" false "$checkout"; then
    seeded=true
  else
    seeded=false
    keep_user_config "$checkout" "$state"
  fi
  # Core never sees the token.
  unset GIT_TOKEN
  if [ "$SEED_SHA" = "$REVIEW_BASE_SHA" ]; then
    echo "::notice::That analysis describes the merge base itself; nothing to catch up."
  elif [ "$seeded" = true ]; then
    "$ACTION_PATH/scripts/action/analyze.sh" base "$checkout" "$state" >/dev/null
  else
    echo "::notice::No saved analysis to start from; analyzing ${REVIEW_BASE_SHA:0:7} from scratch."
    progress base
    "$ACTION_PATH/scripts/action/analyze.sh" base "$checkout" "$state" >/dev/null
    progress head
  fi
fi
[ -f "$state/analysis.json" ] || { echo "::error::Review baseline analysis is missing."; exit 1; }
[ "$publish_base" != true ] || stage "$state" base
printf 'base_analysis_path=%s\npublish_base=%s\n' "$state/analysis.json" "$publish_base" >> "$GITHUB_OUTPUT"
