#!/usr/bin/env bash
# Rewrites the review's sticky progress comment into two steps when the base is
# built from scratch: that is the slow path, and the reader should know why.
# Usage: update-review-progress.sh base|head, once as the base build starts and
# once as it ends. Best effort throughout.
set -euo pipefail
step="$1"
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
why="$branch has no saved diagram this review can start from, so it builds one first. Once a diagram of $branch is saved, reviews start from it and skip this step."
if [ "$step" = base ]; then
  first="1. ⏳ Building the diagram of $branch @$sha7 from scratch"
  second="2. Analysing this PR's changes"
else
  first="1. ✅ Built the diagram of $branch @$sha7 from scratch"
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
gh api -X PATCH "repos/$REPOSITORY/issues/comments/$id" -f body="$body" >/dev/null
