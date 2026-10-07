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
PLATFORM_URL="https://app.codeboarding.org/${GITHUB_REPOSITORY}/pull/${PR_NUMBER}"
WEBVIEW_URL="${PLATFORM_URL}?utm_source=github&utm_medium=pr_comment&utm_campaign=gh_action"
BODY="${RUNNER_TEMP}/review-comment.md"
# The status line is what the web platform reads the count from, so its shape is a contract.
# ANALYSED_FILES_CHANGED counts the analysed files whose content hash differs between base and
# head ("unknown" when the analyses cannot say). At zero no analysed code changed, so the status
# says so, and a non-zero component count is the analysis grouping the same code differently.
ANALYSED_FILES_CHANGED="${ANALYSED_FILES_CHANGED:-unknown}"
STATUS="${N_CHANGED} changed ${COMPONENT_NOUN}"
if [ "$ANALYSED_FILES_CHANGED" = "0" ]; then
  STATUS="${STATUS} (no analysed file changed)"
fi
printf '### CodeBoarding review\n\n**Status:** %s\n' "$STATUS" > "$BODY"
if [ "$ANALYSED_FILES_CHANGED" = "0" ] && [ "$N_CHANGED" != "0" ]; then
  printf '\nNo file CodeBoarding analyses changed in this pull request, so the components marked below differ only because the analysis grouped the same code differently.\n' >> "$BODY"
fi
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
# How the base was obtained, with measured times only: an estimate would be wrong for
# exactly the slow runs it is meant to explain.
duration() {
  local seconds="${1:-0}"
  case "$seconds" in ''|*[!0-9]*) seconds=0 ;; esac
  if [ "$seconds" -ge 60 ]; then
    printf '%s m %s s' "$(( seconds / 60 ))" "$(( seconds % 60 ))"
  else
    printf '%s s' "$seconds"
  fi
}
BASE_SOURCE="${BASE_SOURCE:-}"
BASE_BRANCH="${BASE_REF:-the base branch}"
BASE_FROM="${BASE_FROM_SHA:-}"
CATCHUP="${CATCHUP_COMMITS:-}"
case "$CATCHUP" in *[!0-9]*) CATCHUP="" ;; esac
CHANGES="changes $(duration "${HEAD_SECONDS:-}")"
TOOK="$(duration "${BASE_SECONDS:-}")"
BASE_LINE=""
# An empty sha or count is unknown, not zero: the line then says it caught up
# without claiming the base was exact.
case "$BASE_SOURCE" in
  computed)
    case "${BASE_REASON:-}" in
      incompatible) WHY="saved diagram incompatible" ;;
      too_far_behind) WHY="saved diagram too far behind" ;;
      *) WHY="no saved diagram" ;;
    esac
    BASE_LINE="Base: built from scratch (${WHY}), ${TOOK} · ${CHANGES}"
    ;;
  saved)
    BASE_LINE="Base: saved diagram of ${BASE_BRANCH} @${BASE_FROM:0:7} · ${CHANGES}"
    ;;
  committed | ancestor)
    if [ -z "$BASE_FROM" ] || [ -z "$CATCHUP" ]; then
      BASE_LINE="Base: saved diagram of ${BASE_BRANCH}, caught up, ${TOOK} · ${CHANGES}"
    elif [ "$CATCHUP" -gt 0 ]; then
      COMMIT_NOUN="commits"
      [ "$CATCHUP" != 1 ] || COMMIT_NOUN="commit"
      BASE_LINE="Base: caught up ${CATCHUP} ${COMMIT_NOUN} from ${BASE_BRANCH} @${BASE_FROM:0:7}, ${TOOK} · ${CHANGES}"
    elif [ "$BASE_SOURCE" = ancestor ]; then
      BASE_LINE="Base: caught up from ${BASE_BRANCH} @${BASE_FROM:0:7}, ${TOOK} · ${CHANGES}"
    else
      BASE_LINE="Base: saved diagram of ${BASE_BRANCH} @${BASE_FROM:0:7} · ${CHANGES}"
    fi
    ;;
esac
[ -z "$BASE_LINE" ] || printf '\n<sub>%s</sub>\n' "$BASE_LINE" >> "$BODY"
{
  printf '\n'
  cat "$DIAGRAM"
  printf '\n\n<sub>'
  if [ -n "$ARTIFACT_URL" ]; then
    printf '[download artifacts](%s) · ' "$ARTIFACT_URL"
  fi
  printf 'run [%s](%s)</sub>\n' "$GITHUB_RUN_ID" "$RUN_URL"
  # The machine-readable line: what a reader of the comment (the web platform's dashboard, an
  # agent) needs without parsing the prose or the diagram. An HTML comment renders as nothing.
  # Keep it one line, `key=value` pairs, values without spaces, so a regex over it stays trivial.
  BASE_KEYS=""
  if [ -n "$BASE_SOURCE" ]; then
    BASE_KEYS=" base=${BASE_SOURCE}"
    [ -z "${BASE_REASON:-}" ] || BASE_KEYS="${BASE_KEYS} base_reason=${BASE_REASON}"
    BASE_KEYS="${BASE_KEYS} base_seconds=${BASE_SECONDS:-} head_seconds=${HEAD_SECONDS:-}"
  fi
  printf '<!-- codeboarding: platform_url=%s changed=%s analysed_files_changed=%s head=%s%s -->\n' \
    "$PLATFORM_URL" "$N_CHANGED" "$ANALYSED_FILES_CHANGED" "${HEAD_SHA:-}" "$BASE_KEYS"
} >> "$BODY"
echo "path=$BODY" >> "$GITHUB_OUTPUT"
