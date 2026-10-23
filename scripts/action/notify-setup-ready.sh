#!/usr/bin/env bash
# Tells the author of a CodeBoarding setup PR that its preview map is ready.
#
# The review comment is sticky: it is created when the run starts and edited when it
# ends, and GitHub sends no notification for an edit. A new comment does notify the
# PR's author, who for a setup PR is the person who asked for the map (the app opens
# it with their token), so this one comment is what turns "you can close this tab"
# into an email from GitHub. Failed runs need nothing here: GitHub already emails the
# person who triggered a failed run.
#
# When the setup PR asked for previews of other pull requests, this runs after the first
# one is ready (FIRST_PR) and links it, since a coloured pull request is the point; the
# rest of PREVIEW_PRS are named as still running. Posted once per PR, found again by its
# marker, so pushing new settings to the setup PR does not email again. Best effort throughout.
set -euo pipefail
[ "${HEAD_REF:-}" = "codeboarding/setup-action" ] || exit 0
[ -n "${PR_NUMBER:-}" ] && [ -n "${REPOSITORY:-}" ] || exit 0
export GH_HOST="${GH_HOST:-github.com}"
GH_HOST="${GH_HOST#*://}"

marker="<!-- codeboarding-setup-ready -->"
existing="$(gh api --paginate "repos/$REPOSITORY/issues/$PR_NUMBER/comments?per_page=100" \
  --jq ".[] | select((.body // \"\") | contains(\"$marker\")) | .id")"
[ -z "$existing" ] || exit 0

# A mention notifies even an author who unsubscribed from the thread. Bots are not mentioned.
greeting="Your"
case "${PR_AUTHOR:-}" in
  "" | *"[bot]") ;;
  *) greeting="@$PR_AUTHOR your" ;;
esac
if [ -n "${BASE_REF:-}" ]; then
  branch="\`$BASE_REF\`"
else
  branch="your default branch"
fi
link() { printf 'https://app.codeboarding.org/%s/pull/%s?utm_source=github&utm_medium=setup_ready&utm_campaign=gh_action' "$REPOSITORY" "$1"; }
if [ -n "${FIRST_PR:-}" ]; then
  headline="$greeting CodeBoarding map of $branch is ready, and #$FIRST_PR is reviewed on it."
  cta="**[Open #$FIRST_PR in CodeBoarding]($(link "$FIRST_PR"))**${FIRST_TITLE:+ · $FIRST_TITLE}"
  rest=""
  IFS=',' read -r -a all <<< "${PREVIEW_PRS:-}"
  for pr in "${all[@]}"; do
    if [ -n "$pr" ] && [ "$pr" != "$FIRST_PR" ]; then rest+="${rest:+, }#$pr"; fi
  done
  [ -z "$rest" ] || cta+=$'\n\n'"$rest follow in a few minutes. [Your map]($(link "$PR_NUMBER")) is open already."
else
  headline="$greeting CodeBoarding map of $branch is ready."
  cta="**[Open it in CodeBoarding]($(link "$PR_NUMBER"))**"
fi
body="$(printf '%s\n\n%s\n\n%s\n\n%s' \
  "$headline" \
  "$cta" \
  "This is a preview. Merge this PR to keep the map up to date and review every pull request." \
  "$marker")"
gh api -X POST "repos/$REPOSITORY/issues/$PR_NUMBER/comments" -f body="$body" >/dev/null
