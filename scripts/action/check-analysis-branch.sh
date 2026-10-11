#!/usr/bin/env bash
# Sync keeps one branch's analysis on the analysis branch: the branch that first
# saved there. Checked before the engine is installed, so a sync from a second
# branch, or an analysis branch name already used by code, fails in seconds
# instead of after a full analysis.
set -euo pipefail
source "$(dirname "$0")/codeboarding-baseline.sh"
[ -n "${BASELINE_BRANCH:-}" ] || exit 0

status=0
baseline_branch_exists "$REPOSITORY" || status=$?
# The first sync creates it.
[ "$status" = 0 ] || exit 0

# Commit messages and the tree listing only: no analysis files are downloaded.
scratch="$RUNNER_TEMP/codeboarding-analysis-branch-check.git"
rm -rf "$scratch"
git init -q --bare "$scratch"
auth="$(printf 'x-access-token:%s' "${GIT_TOKEN:-}" | base64 -w0)"
git -C "$scratch" -c "http.extraheader=AUTHORIZATION: basic $auth" fetch -q --filter=blob:none --depth=1 \
  "${GITHUB_SERVER_URL%/}/${REPOSITORY}.git" "refs/heads/$BASELINE_BRANCH" ||
  baseline_fail "Could not fetch $BASELINE_BRANCH from $REPOSITORY."
trailer() { git -C "$scratch" log -1 --format="%(trailers:key=$1,valueonly)" FETCH_HEAD | tr -d '[:space:]'; }

if [ -z "$(trailer CodeBoarding-Source)" ] || [ "$(git -C "$scratch" ls-tree --name-only FETCH_HEAD)" != .codeboarding ]; then
  baseline_fail "$BASELINE_BRANCH already exists and is not a CodeBoarding analysis branch, so sync will not write to it. Rename that branch, or set codeboarding_analysis_location: in_place."
fi
owner="$(trailer CodeBoarding-Branch)"
if [ -n "$owner" ] && [ "$owner" != "$SYNCED_BRANCH" ]; then
  baseline_fail "$BASELINE_BRANCH keeps the analysis of $owner, and this sync runs on $SYNCED_BRANCH. Sync keeps one branch current: list only $owner under on: push: branches:, or delete $BASELINE_BRANCH to keep $SYNCED_BRANCH's analysis there instead."
fi
