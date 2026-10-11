#!/usr/bin/env bash
# Rewrites deprecated inputs as codeboarding_analysis_location, the way a person
# editing the workflow would, and fails on any it cannot express. It is the only
# place that knows they exist: every later step reads its output, never the inputs.
#
#   sync_strategy: push                        -> codeboarding_analysis_location: in_place
#   sync_strategy: pull_request                -> codeboarding_analysis_location: codeboarding_branch
#   target_branch: <the branch the run is on>  -> nothing
#   target_branch: <another branch>            -> fails: the push trigger picks the branch
#
# Delete a mapping, and its input in action.yml, once no supported workflow sets it.
set -euo pipefail
MIGRATE_URL=https://github.com/CodeBoarding/CodeBoarding-action#moving-an-existing-setup
fail() { echo "::error title=CodeBoarding inputs::$1 See $MIGRATE_URL for the change, or a prompt that makes it for you."; exit 1; }
warn() { echo "::warning title=CodeBoarding inputs::$1 See $MIGRATE_URL."; }
DEFAULT_LOCATION=codeboarding_branch

location="${ANALYSIS_LOCATION:-}"
case "$location" in
  codeboarding_branch | in_place) ;;
  *) fail "codeboarding_analysis_location must be codeboarding_branch or in_place, not '$location'." ;;
esac

if [ -n "${OLD_SYNC_STRATEGY:-}" ]; then
  # codeboarding_analysis_location has a default, so only a value other than it is a conflict.
  [ "$location" = "$DEFAULT_LOCATION" ] ||
    fail "Set codeboarding_analysis_location only; sync_strategy is the input it replaces."
  case "$OLD_SYNC_STRATEGY" in
    push) location=in_place ;;
    pull_request) location=codeboarding_branch ;;
    *) fail "sync_strategy is no longer supported; use codeboarding_analysis_location." ;;
  esac
  warn "sync_strategy is deprecated; replace sync_strategy: $OLD_SYNC_STRATEGY with codeboarding_analysis_location: $location, in the review workflow too."
  [ "$OLD_SYNC_STRATEGY" != pull_request ] ||
    warn "Rolling sync pull requests are gone; close any open codeboarding/sync pull request."
fi

if [ -n "${OLD_TARGET_BRANCH:-}" ]; then
  # Only sync read it; the branch a sync runs on is the one its push trigger lists.
  [ "$MODE" != sync ] || [ "$OLD_TARGET_BRANCH" = "$REF_NAME" ] ||
    fail "target_branch is no longer supported: sync analyzes the branch its push trigger lists, and this run is on $REF_NAME. List $OLD_TARGET_BRANCH under on: push: branches: instead, and remove target_branch."
  warn "target_branch is deprecated and has no effect; remove it."
fi

echo "analysis_location=$location" >> "$GITHUB_OUTPUT"
