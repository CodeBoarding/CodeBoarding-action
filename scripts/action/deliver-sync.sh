#!/usr/bin/env bash
# Installs generated state and delivers it by direct push or a rolling sync PR.
set -euo pipefail
cd "$CHECKOUT_DIR"
SYNC_BRANCH=codeboarding/sync
REMOTE="${GITHUB_SERVER_URL%/}/${REPOSITORY}.git"
ASKPASS="$RUNNER_TEMP/codeboarding-git-askpass.sh"
GENERATED_PATHS="$RUNNER_TEMP/codeboarding-sync-paths"
BASE_SHA="$(git rev-parse HEAD)"
cat > "$ASKPASS" <<'SH'
#!/bin/sh
case "$1" in
  *Username*) echo x-access-token ;;
  *Password*) printf '%s\n' "$GITHUB_TOKEN" ;;
esac
SH
chmod 700 "$ASKPASS"
export GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0
export GH_HOST="${GH_HOST#*://}"
trap 'rm -f "$ASKPASS"' EXIT
# A pull request's merge base is whichever commit its author branched from, and
# this run's analysis is valid for two of them: the commit it analyzed, and the
# baseline commit it writes on top, which differs only in .codeboarding files
# that the fingerprint ignores. Publishing both means a pull request opened
# either side of a sync commit still gets an exact hit.
emit_result() {
  printf 'files_written=%s\ncommitted=%s\nbaseline_sha=%s\nanalyzed_sha=%s\n' \
    "$1" "$2" "$3" "$BASE_SHA" >> "$GITHUB_OUTPUT"
}
close_stale_pr() {
  [ "$SYNC_STRATEGY" = pull_request ] || return 0
  git fetch "$REMOTE" "$TARGET_BRANCH"
  [ "$(git rev-parse FETCH_HEAD)" = "$BASE_SHA" ] || return 0
  local number sync_sha
  number="$(gh api --method GET "repos/$REPOSITORY/pulls" \
    -f state=open -f base="$TARGET_BRANCH" \
    -f head="${REPOSITORY%%/*}:$SYNC_BRANCH" --jq '.[0].number // empty')"
  if [ -n "$number" ]; then
    sync_sha="$(git ls-remote "$REMOTE" "refs/heads/$SYNC_BRANCH" | awk '{print $1; exit}')"
    if [ "$sync_sha" != "${SYNC_BRANCH_START_SHA:-}" ]; then
      echo "::notice::A newer run updated $SYNC_BRANCH; leaving its PR open."
      return 0
    fi
    if [ -n "$sync_sha" ] && ! git push \
      "--force-with-lease=refs/heads/$SYNC_BRANCH:$sync_sha" "$REMOTE" ":refs/heads/$SYNC_BRANCH"; then
      echo "::notice::A newer run updated $SYNC_BRANCH; leaving its PR open."
      return 0
    fi
    gh pr close "$number" --repo "$REPOSITORY"
    echo "::notice::Closed obsolete CodeBoarding sync PR #$number."
  fi
}
classify_push_failure() {
  local expected="$1" branch="$2" current
  current="$(git ls-remote "$REMOTE" "refs/heads/$branch" | awk '{print $1; exit}')"
  [ "$current" != "$(git rev-parse HEAD)" ] || return 0
  if [ "$current" != "$expected" ]; then
    emit_result "$files_written" false "$BASE_SHA"
    echo "::notice::A newer run updated $branch; leaving it untouched."
    exit 0
  fi
  echo "::error::Could not push the CodeBoarding baseline to $branch."
  exit 1
}

"$ACTION_PATH/scripts/action/install-sync.sh" > "$GENERATED_PATHS"
stage_paths=()
while IFS= read -r path; do
  if [ -e "$path" ] || git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
    stage_paths+=("$path")
  fi
done < "$GENERATED_PATHS"
git config user.name 'codeboarding-review[bot]'
git config user.email 'codeboarding-review[bot]@users.noreply.github.com'
git add -f -A -- "${stage_paths[@]}"

files_written="$(find "$CHECKOUT_DIR/.codeboarding" -maxdepth 1 -type f \
  \( -name analysis.json -o -name fingerprint.json -o -name static_analysis.pkl -o -name static_analysis.sha \
  -o -name codeboarding_version.json \) | wc -l)"

git fetch "$REMOTE" "$TARGET_BRANCH"
remote_sha="$(git rev-parse FETCH_HEAD)"
if [ "$remote_sha" != "$BASE_SHA" ]; then
  emit_result "$files_written" false "$BASE_SHA"
  echo "::notice::$TARGET_BRANCH advanced during analysis; a newer run should update its baseline."
  exit 0
fi

if git diff --cached --quiet || git diff --cached --quiet -I '"generated_at"' -I '"timestamp"'; then
  git reset -q
  close_stale_pr
  emit_result "$files_written" false "$BASE_SHA"
  echo "::notice::The CodeBoarding baseline is unchanged."
  exit 0
fi

git commit -m 'chore(codeboarding): sync analysis baseline' >/dev/null

if [ "$SYNC_STRATEGY" = push ]; then
  if ! git push "$REMOTE" "HEAD:refs/heads/$TARGET_BRANCH"; then
    classify_push_failure "$BASE_SHA" "$TARGET_BRANCH"
  fi
  emit_result "$files_written" true "$(git rev-parse HEAD)"
  exit 0
fi

pr_json="$(gh api --method GET "repos/$REPOSITORY/pulls" \
  -f state=open -f head="${REPOSITORY%%/*}:$SYNC_BRANCH" --jq '.[0] // empty')"
if [ -n "$pr_json" ] && [ "$(jq -r .base.ref <<< "$pr_json")" != "$TARGET_BRANCH" ]; then
  echo "::error::$SYNC_BRANCH already has an open PR into $(jq -r .base.ref <<< "$pr_json"); close it before changing target_branch."
  exit 1
fi

old_sync_sha="$(git ls-remote "$REMOTE" "refs/heads/$SYNC_BRANCH" | awk '{print $1; exit}')"
if ! git push "--force-with-lease=refs/heads/$SYNC_BRANCH:$old_sync_sha" \
  "$REMOTE" "HEAD:refs/heads/$SYNC_BRANCH"; then
  classify_push_failure "$old_sync_sha" "$SYNC_BRANCH"
fi

if [ -z "$pr_json" ]; then
  gh pr create --repo "$REPOSITORY" --base "$TARGET_BRANCH" --head "$SYNC_BRANCH" \
    --title 'chore(codeboarding): sync analysis baseline' \
    --body "Updates the versioned CodeBoarding analysis for \`$TARGET_BRANCH\`."
  pr_json="$(gh api --method GET "repos/$REPOSITORY/pulls" \
    -f state=open -f base="$TARGET_BRANCH" \
    -f head="${REPOSITORY%%/*}:$SYNC_BRANCH" --jq '.[0]')"
fi

pr_url="$(jq -r .html_url <<< "$pr_json")"
pr_number="$(jq -r .number <<< "$pr_json")"
emit_result "$files_written" true "$BASE_SHA"
{
  echo "sync_pr_url=$pr_url"
  echo "sync_pr_number=$pr_number"
} >> "$GITHUB_OUTPUT"
