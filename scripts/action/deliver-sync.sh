#!/usr/bin/env bash
# Installs generated state and delivers it to the analysis branch, or, when there
# is none, commits it to the synced branch itself.
set -euo pipefail
cd "$CHECKOUT_DIR"
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

source "$ACTION_PATH/scripts/action/codeboarding-baseline.sh"
[ -z "${BASELINE_BRANCH:-}" ] || save_to_codeboarding_baseline

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
  emit_result "$files_written" false "$BASE_SHA"
  echo "::notice::The CodeBoarding baseline is unchanged."
  exit 0
fi

git commit -m 'chore(codeboarding): sync analysis baseline' >/dev/null

if ! git push "$REMOTE" "HEAD:refs/heads/$SYNCED_BRANCH"; then
  classify_push_failure "$BASE_SHA" "$SYNCED_BRANCH"
fi
emit_result "$files_written" true "$(git rev-parse HEAD)"
