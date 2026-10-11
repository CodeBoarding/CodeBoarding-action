#!/usr/bin/env bash
# An analysis branch name git cannot create, or one already used by code, fails
# here, before the engine is installed, instead of after a full analysis.
set -euo pipefail
# shellcheck source=scripts/action/codeboarding-baseline.sh
source "$(dirname "$0")/codeboarding-baseline.sh"
[ -n "${BASELINE_BRANCH:-}" ] || exit 0

status=0
# shellcheck disable=SC2153 # REPOSITORY comes from the step's env.
baseline_branch_exists "$REPOSITORY" || status=$?
if [ "$status" = 2 ]; then
  # The first sync creates it, which git refuses next to a branch named like its parent directory.
  blocking="${BASELINE_BRANCH%/*}"
  [ "$blocking" = "$BASELINE_BRANCH" ] || {
    status=0
    baseline_branch_exists "$REPOSITORY" "$blocking" || status=$?
    [ "$status" = 2 ] ||
      baseline_fail "$REPOSITORY has a branch named $blocking, so git cannot create $BASELINE_BRANCH next to it. Rename $blocking, or set codeboarding_analysis_location: in_place."
  }
  exit 0
fi

# Commit messages and the tree listing only: no analysis files are downloaded.
scratch="$RUNNER_TEMP/codeboarding-analysis-branch-check.git"
rm -rf "$scratch"
git init -q --bare "$scratch"
auth="$(printf 'x-access-token:%s' "${GIT_TOKEN:-}" | base64 -w0)"
git -C "$scratch" -c "http.extraheader=AUTHORIZATION: basic $auth" fetch -q --filter=blob:none --depth=1 \
  "${GITHUB_SERVER_URL%/}/${REPOSITORY}.git" "refs/heads/$BASELINE_BRANCH" ||
  baseline_fail "Could not fetch $BASELINE_BRANCH from $REPOSITORY."
source_trailer="$(git -C "$scratch" log -1 --format='%(trailers:key=CodeBoarding-Source,valueonly)' FETCH_HEAD | tr -d '[:space:]')"
if [ -z "$source_trailer" ] || [ "$(git -C "$scratch" ls-tree --name-only FETCH_HEAD)" != .codeboarding ]; then
  baseline_fail "$BASELINE_BRANCH already exists and is not a CodeBoarding analysis branch, so sync will not write to it. Rename that branch, or set codeboarding_analysis_location: in_place."
fi
