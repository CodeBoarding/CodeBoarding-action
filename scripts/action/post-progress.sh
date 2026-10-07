#!/usr/bin/env bash
# Rewrites the review's sticky progress comment into two steps while the base is
# built from scratch: that is the slow path, and the reader should know why.
# Usage: post-progress.sh base|head <elapsed seconds>. Best effort throughout.
set -euo pipefail
step="$1" elapsed="$2"
[ -n "${PROGRESS_HEADER:-}" ] && [ -n "${PR_NUMBER:-}" ] && [ -n "${REPOSITORY:-}" ] || exit 0
export GH_HOST="${GH_HOST:-github.com}"
GH_HOST="${GH_HOST#*://}"

# The sticky-comment action finds its comment by this line, so it must survive the edit.
marker="<!-- Sticky Pull Request Comment${PROGRESS_HEADER} -->"
id_file="${RUNNER_TEMP:?}/codeboarding-progress-comment-${PROGRESS_HEADER}"
id="$(cat "$id_file" 2>/dev/null || true)"
if [ -z "$id" ]; then
  id="$(gh api --paginate "repos/$REPOSITORY/issues/$PR_NUMBER/comments?per_page=100" \
    --jq ".[] | select((.body // \"\") | startswith(\"### CodeBoarding review\") and contains(\"$marker\")) | .id" |
    tail -n 1)"
  [ -n "$id" ] || exit 0
  echo "$id" > "$id_file"
fi

if [ -n "${BASE_REF:-}" ]; then
  branch="\`$BASE_REF\`"
else
  branch="the base branch"
fi
sha7="${REVIEW_BASE_SHA:0:7}"
case "${BASE_REASON:-}" in
  incompatible) why="The saved diagram of $branch was made by a different engine version or settings, so this review builds a new one." ;;
  too_far_behind) why="The nearest saved diagram of $branch is too far behind this pull request's base, so this review builds one from scratch." ;;
  *) why="$branch has no saved diagram yet, so this review builds one first. Once a diagram of $branch is saved, reviews start from it and skip this step." ;;
esac
minutes=$(( elapsed / 60 ))
if [ "$step" = base ]; then
  running="running for $minutes min"
  [ "$minutes" -gt 0 ] || running="running for less than a minute"
  first="1. ⏳ Building the diagram of $branch @$sha7 from scratch · $running"
  second="2. Analysing this PR's changes"
else
  took="$(( elapsed % 60 )) s"
  [ "$minutes" -eq 0 ] || took="$minutes m $took"
  first="1. ✅ Built the diagram of $branch @$sha7 from scratch in $took"
  second="2. ⏳ Analysing this PR's changes"
fi

platform="https://app.codeboarding.org/$REPOSITORY/pull/$PR_NUMBER?utm_source=github&utm_medium=pr_comment&utm_campaign=gh_action"
run_url="${GITHUB_SERVER_URL:-https://github.com}/$REPOSITORY/actions/runs/${GITHUB_RUN_ID:-}"
body="$(printf '%s\n\n%s\n   %s\n%s\n\n%s\n\n%s\n%s' \
  '### CodeBoarding review · analyzing…' \
  "$first" "$why" "$second" \
  "Open it in [CodeBoarding]($platform) meanwhile: the files, comments and review are there already, and the diff appears when the run finishes." \
  "<sub>run [${GITHUB_RUN_ID:-}]($run_url) · attempt ${GITHUB_RUN_ATTEMPT:-1}</sub>" \
  "$marker")"
# Checked last: the ticker may have been stopped while this ran, and a stale
# "running" edit must not land on top of the next step.
if [ "$step" = base ] && [ -n "${PROGRESS_STOP_FILE:-}" ] && [ -e "$PROGRESS_STOP_FILE" ]; then
  exit 0
fi
gh api -X PATCH "repos/$REPOSITORY/issues/comments/$id" -f body="$body" >/dev/null
