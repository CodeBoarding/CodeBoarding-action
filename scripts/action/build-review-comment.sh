#!/usr/bin/env bash
# Builds the final PR review Markdown and outputs its file path for posting.
set -euo pipefail
COMPONENT_NOUN="components"
if [ "$N_CHANGED" = "1" ]; then
  COMPONENT_NOUN="component"
fi
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
# Stable PR route: the webview resolves the repo/PR's latest artifact itself,
# so the link stays valid across runs instead of pinning one run id.
# Standard utm_* rather than a bespoke ?src=: PostHog lifts utm_* into person and
# session properties by itself, so this is attributable the day the action ships,
# with no matching change in the web app. Without it the only evidence a visit
# came from here is a github.com referrer, which most clients strip and a link
# pasted into chat never had. Constant across runs on purpose: a run id here
# would scatter one pull request's clicks across a new value per re-run.
WEBVIEW_URL="https://app.codeboarding.org/${GITHUB_REPOSITORY}/pull/${PR_NUMBER}?utm_source=github&utm_medium=pr_comment&utm_campaign=gh_action"
BODY="${RUNNER_TEMP}/review-comment.md"
printf '### CodeBoarding review\n\n**Status:** %s changed %s\n' "$N_CHANGED" "$COMPONENT_NOUN" > "$BODY"
printf '\nSee the full change in [CodeBoarding](%s).\n' "$WEBVIEW_URL" >> "$BODY"
# The diagram compares against the merge base, so commits landed on the base
# branch since this PR forked are excluded. Say so rather than hide it.
BEHIND="${BEHIND_BY:-0}"
if [ "${MERGE_BASE_RESOLVED:-true}" != true ]; then
  # shellcheck disable=SC2016  # the backticks are Markdown, not a command
  printf '\n> [!WARNING]\n> The merge base could not be resolved, so this compares against the tip of `%s`. Changes made on `%s` since this branch forked may appear here as this pull request'\''s changes.\n' \
    "${BASE_REF:-the base branch}" "${BASE_REF:-the base branch}" >> "$BODY"
elif [ "$BEHIND" -gt 0 ] 2>/dev/null; then
  COMMIT_NOUN="commits"
  [ "$BEHIND" != "1" ] || COMMIT_NOUN="commit"
  # shellcheck disable=SC2016  # the backticks are Markdown, not a command
  printf '\n<sub>Compared against the merge base: this branch is %s %s behind `%s`.</sub>\n' \
    "$BEHIND" "$COMMIT_NOUN" "${BASE_REF:-the base branch}" >> "$BODY"
fi
{
  printf '\n'
  cat "$DIAGRAM"
  printf '\n\n<sub>'
  if [ -n "$ARTIFACT_URL" ]; then
    printf '[download artifacts](%s) · ' "$ARTIFACT_URL"
  fi
  printf 'run [%s](%s)</sub>\n' "$GITHUB_RUN_ID" "$RUN_URL"
} >> "$BODY"
echo "path=$BODY" >> "$GITHUB_OUTPUT"
