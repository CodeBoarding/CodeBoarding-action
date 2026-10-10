#!/usr/bin/env bash
# Installs generated state and delivers it to the synced branch, a rolling sync PR, or the baseline branch.
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
  [ "$SAVE_BASELINE_TO" = pull_request ] || return 0
  git fetch "$REMOTE" "$SYNCED_BRANCH"
  [ "$(git rev-parse FETCH_HEAD)" = "$BASE_SHA" ] || return 0
  local number sync_sha
  number="$(gh api --method GET "repos/$REPOSITORY/pulls" \
    -f state=open -f base="$SYNCED_BRANCH" \
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

# save_baseline_to: baseline_branch keeps the analysis on an orphan branch of its
# own, one fast-forward commit per sync, and never writes to the synced branch.
deliver_to_baseline_branch() {
  local branch="$BASELINE_BRANCH" tree="$RUNNER_TEMP/codeboarding-baseline-tree"
  local index="$RUNNER_TEMP/codeboarding-baseline-index" git_dir files new_tree tip parent commit now
  git_dir="$(git rev-parse --absolute-git-dir)"
  rm -rf "$tree" "$index"
  mkdir -p "$tree"
  CHECKOUT_DIR="$tree" "$ACTION_PATH/scripts/action/install-sync.sh" > /dev/null
  files="$(find "$tree/.codeboarding" -maxdepth 1 -type f | wc -l | tr -d ' ')"
  # Engine output is never edited; which commit it describes, and under which
  # configuration, lives here and in the commit's trailers only.
  python3 -c 'import datetime,json,os,sys
json.dump({
    "schema": 1,
    "synced_branch": os.environ["SYNCED_BRANCH"],
    "source_sha": sys.argv[2],
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "engine_version": os.environ.get("ENGINE_VERSION", ""),
    "config": os.environ.get("CFG_HASH", ""),
}, open(sys.argv[1], "w"), indent=2)' "$tree/.codeboarding/source.json" "$BASE_SHA"
  GIT_INDEX_FILE="$index" git --git-dir="$git_dir" --work-tree="$tree" -C "$tree" add -A -f .codeboarding
  new_tree="$(GIT_INDEX_FILE="$index" git --git-dir="$git_dir" write-tree)"
  git config user.name 'codeboarding-review[bot]'
  git config user.email 'codeboarding-review[bot]@users.noreply.github.com'
  local trailers=(-m "CodeBoarding-Source: $BASE_SHA")
  [ -z "${CFG_HASH:-}" ] || trailers=(-m "CodeBoarding-Source: $BASE_SHA
CodeBoarding-Config: $CFG_HASH")

  # Two tries: a concurrent sync that moved the branch for an older commit is
  # built on top of once. A second move means a newer run is handling it.
  for _ in 1 2; do
    git fetch -q "$REMOTE" "$SYNCED_BRANCH"
    if [ "$(git rev-parse FETCH_HEAD)" != "$BASE_SHA" ]; then
      emit_result "$files" false "$BASE_SHA"
      echo "::notice::$SYNCED_BRANCH advanced during analysis; a newer run should update $branch."
      exit 0
    fi
    tip="" parent=()
    if [ -n "$(git ls-remote "$REMOTE" "refs/heads/$branch")" ]; then
      git fetch -q --depth=1 "$REMOTE" "refs/heads/$branch"
      # The parent is what was fetched, not what ls-remote saw: the branch may move in between.
      tip="$(git rev-parse FETCH_HEAD)"
      # Building on any other branch would leave it holding nothing but .codeboarding/.
      if [ -z "$(git log -1 --format='%(trailers:key=CodeBoarding-Source,valueonly)' "$tip" | tr -d '[:space:]')" ] ||
        [ "$(git ls-tree --name-only "$tip")" != .codeboarding ]; then
        echo "::error::$branch already exists and is not a CodeBoarding baseline branch, so sync will not write to it. Set baseline_branch to a branch name that is not in use."
        exit 1
      fi
      parent=(-p "$tip")
      if [ "$(git log -1 --format='%(trailers:key=CodeBoarding-Source,valueonly)' "$tip" | tr -d '[:space:]')" = "$BASE_SHA" ] &&
        git diff --quiet -I '"generated_at"' -I '"timestamp"' "$tip" "$new_tree"; then
        emit_result "$files" false "$BASE_SHA"
        echo "::notice::$branch already holds this analysis of $SYNCED_BRANCH @${BASE_SHA:0:7}."
        exit 0
      fi
    fi
    commit="$(git commit-tree "$new_tree" ${parent[@]+"${parent[@]}"} \
      -m "chore(codeboarding): diagram of $SYNCED_BRANCH @${BASE_SHA:0:7}" "${trailers[@]}")"
    # Never forced: the parent is the tip just read, so this only ever fast-forwards.
    if git push -q "$REMOTE" "$commit:refs/heads/$branch"; then
      emit_result "$files" true "$BASE_SHA"
      echo "baseline_branch_sha=$commit" >> "$GITHUB_OUTPUT"
      exit 0
    fi
    now="$(git ls-remote "$REMOTE" "refs/heads/$branch" | awk '{print $1; exit}')"
    if [ "$now" = "$tip" ]; then
      echo "::error::GitHub refused the push to $branch, most likely because a branch rule protects it. Add the identity sync pushes with (the CodeBoarding app, or GitHub Actions for the default token) as a bypass actor for $branch in the repository's rulesets, or set save_baseline_to: synced_branch."
      exit 1
    fi
  done
  emit_result "$files" false "$BASE_SHA"
  echo "::notice::Another sync keeps updating $branch; leaving it to that run."
  exit 0
}
[ "$SAVE_BASELINE_TO" != baseline_branch ] || deliver_to_baseline_branch

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

git fetch "$REMOTE" "$SYNCED_BRANCH"
remote_sha="$(git rev-parse FETCH_HEAD)"
if [ "$remote_sha" != "$BASE_SHA" ]; then
  emit_result "$files_written" false "$BASE_SHA"
  echo "::notice::$SYNCED_BRANCH advanced during analysis; a newer run should update its baseline."
  exit 0
fi

# The -I filters keep a run that changed nothing from committing: analysis.json
# carries generated_at and health_report.json carries timestamp, both rewritten
# every run. Dropping either filter turns every sync into a commit.
if git diff --cached --quiet || git diff --cached --quiet -I '"generated_at"' -I '"timestamp"'; then
  git reset -q
  close_stale_pr
  emit_result "$files_written" false "$BASE_SHA"
  echo "::notice::The CodeBoarding baseline is unchanged."
  exit 0
fi

git commit -m 'chore(codeboarding): sync analysis baseline' >/dev/null

if [ "$SAVE_BASELINE_TO" = synced_branch ]; then
  if ! git push "$REMOTE" "HEAD:refs/heads/$SYNCED_BRANCH"; then
    classify_push_failure "$BASE_SHA" "$SYNCED_BRANCH"
  fi
  emit_result "$files_written" true "$(git rev-parse HEAD)"
  exit 0
fi

pr_json="$(gh api --method GET "repos/$REPOSITORY/pulls" \
  -f state=open -f head="${REPOSITORY%%/*}:$SYNC_BRANCH" --jq '.[0] // empty')"
if [ -n "$pr_json" ] && [ "$(jq -r .base.ref <<< "$pr_json")" != "$SYNCED_BRANCH" ]; then
  echo "::error::$SYNC_BRANCH already has an open PR into $(jq -r .base.ref <<< "$pr_json"); close it before changing synced_branch."
  exit 1
fi

old_sync_sha="$(git ls-remote "$REMOTE" "refs/heads/$SYNC_BRANCH" | awk '{print $1; exit}')"
if ! git push "--force-with-lease=refs/heads/$SYNC_BRANCH:$old_sync_sha" \
  "$REMOTE" "HEAD:refs/heads/$SYNC_BRANCH"; then
  classify_push_failure "$old_sync_sha" "$SYNC_BRANCH"
fi

if [ -z "$pr_json" ]; then
  gh pr create --repo "$REPOSITORY" --base "$SYNCED_BRANCH" --head "$SYNC_BRANCH" \
    --title 'chore(codeboarding): sync analysis baseline' \
    --body "Updates the versioned CodeBoarding analysis for \`$SYNCED_BRANCH\`."
  pr_json="$(gh api --method GET "repos/$REPOSITORY/pulls" \
    -f state=open -f base="$SYNCED_BRANCH" \
    -f head="${REPOSITORY%%/*}:$SYNC_BRANCH" --jq '.[0]')"
fi

pr_url="$(jq -r .html_url <<< "$pr_json")"
pr_number="$(jq -r .number <<< "$pr_json")"
emit_result "$files_written" true "$BASE_SHA"
{
  echo "sync_pr_url=$pr_url"
  echo "sync_pr_number=$pr_number"
} >> "$GITHUB_OUTPUT"
