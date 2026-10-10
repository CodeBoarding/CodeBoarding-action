#!/usr/bin/env bash
# Rewrites deprecated inputs as codeboarding_analysis_branch, the way a person
# editing the workflow would, and fails on any it cannot express. It is the only
# place that knows they exist: every later step reads its output, never the inputs.
#
#   sync_strategy: push          -> codeboarding_analysis_branch: <the synced branch>
#   sync_strategy: pull_request  -> codeboarding_analysis_branch: <default> (code branches are never written)
#   target_branch: <the synced branch>  -> nothing
#   target_branch: <another branch>     -> fails: sync analyzes the branch it runs on
#
# Delete a mapping, and its input in action.yml, once no supported workflow sets it.
set -euo pipefail
MIGRATE_URL=https://github.com/CodeBoarding/CodeBoarding-action#moving-an-existing-setup
fail() { echo "::error title=CodeBoarding inputs::$1 See $MIGRATE_URL for the change, or a prompt that makes it for you."; exit 1; }
warn() { echo "::warning title=CodeBoarding inputs::$1 See $MIGRATE_URL."; }
DEFAULT_ANALYSIS_BRANCH=codeboarding/baseline

analysis_branch="${ANALYSIS_BRANCH:-}"
[ -n "$analysis_branch" ] || { echo "::error::codeboarding_analysis_branch must name a branch."; exit 1; }
# Both inputs only ever applied to sync.
if [ "$MODE" = sync ]; then
  if [ -n "${OLD_TARGET_BRANCH:-}" ]; then
    [ "$OLD_TARGET_BRANCH" = "$REF_NAME" ] ||
      fail "target_branch is no longer supported: sync analyzes the branch it runs on, $REF_NAME, not $OLD_TARGET_BRANCH. Remove target_branch and run sync on $OLD_TARGET_BRANCH."
    warn "target_branch is deprecated and has no effect here; remove it."
  fi
  if [ -n "${OLD_SYNC_STRATEGY:-}" ]; then
    # codeboarding_analysis_branch has a default, so only a value other than it is a conflict.
    [ "$analysis_branch" = "$DEFAULT_ANALYSIS_BRANCH" ] ||
      fail "Set codeboarding_analysis_branch only; sync_strategy is the input it replaces."
    case "$OLD_SYNC_STRATEGY" in
      push)
        analysis_branch="$REF_NAME"
        warn "sync_strategy is deprecated; replace sync_strategy: push with codeboarding_analysis_branch: $REF_NAME."
        ;;
      pull_request)
        warn "sync_strategy: pull_request is deprecated; the analysis is saved to $analysis_branch instead, and code branches are never written. Remove sync_strategy, and close any open codeboarding/sync pull request."
        ;;
      *) fail "sync_strategy is no longer supported; use codeboarding_analysis_branch." ;;
    esac
  fi
elif [ "${OLD_SYNC_STRATEGY:-}" = push ]; then
  warn "sync_strategy only applied to sync and is deprecated; replace it with codeboarding_analysis_branch set to the branch sync commits to, here and in the sync workflow."
elif [ -n "${OLD_TARGET_BRANCH:-}${OLD_SYNC_STRATEGY:-}" ]; then
  warn "target_branch and sync_strategy only applied to sync, and are deprecated; remove them."
fi

echo "analysis_branch=$analysis_branch" >> "$GITHUB_OUTPUT"
