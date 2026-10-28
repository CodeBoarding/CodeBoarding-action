#!/usr/bin/env bash
# Reads which pull requests a CodeBoarding setup PR should preview, from a line the app
# writes into its description:
#
#   <!-- codeboarding:preview 412,409,401 -->
#
# Before the setup PR merges, its own run is the only one GitHub starts for us, so this is
# where a new user's open pull requests get their first review. The description is read
# now, after the map is built, not from the event payload, so a list changed while the map
# was building still counts. Each entry must be an open pull request from this repository
# into the same branch; anything else is skipped with a notice. At most MAX_PREVIEWS.
#
# Writes one JSON file per preview to $RUNNER_TEMP/codeboarding-preview/<n>.json and
# outputs count= and prs= (comma-separated, in order). Best effort throughout.
set -euo pipefail
out="${RUNNER_TEMP:?}/codeboarding-preview"
rm -rf "$out"
mkdir -p "$out"
count=0 prs=""
finish() { printf 'count=%s\nprs=%s\n' "$count" "$prs" >> "${GITHUB_OUTPUT:-/dev/null}"; exit 0; }
[ "${HEAD_REF:-}" = "codeboarding/setup-action" ] || finish
[ -n "${PR_NUMBER:-}" ] && [ -n "${REPOSITORY:-}" ] && [ -n "${BASE_REF:-}" ] || finish
export GH_HOST="${GH_HOST:-github.com}"
GH_HOST="${GH_HOST#*://}"
max="${MAX_PREVIEWS:-3}"

body="$(gh api "repos/$REPOSITORY/pulls/$PR_NUMBER" --jq '.body // ""' 2>/dev/null || true)"
line="$(grep -oE '<!-- codeboarding:preview [0-9, ]+ -->' <<< "$body" | head -n 1 || true)"
[ -n "$line" ] || finish

skip() { echo "::notice::Not previewing #$1: $2"; }
seen=" "
while IFS= read -r pr; do
  [ "$count" -lt "$max" ] || { skip "$pr" "at most $max pull requests are previewed."; continue; }
  [ "$pr" != "$PR_NUMBER" ] || continue
  case "$seen" in *" $pr "*) continue ;; esac
  seen+="$pr "
  if ! json="$(gh api "repos/$REPOSITORY/pulls/$pr" 2>/dev/null)"; then
    skip "$pr" "it could not be read."
    continue
  fi
  IFS=$'\t' read -r state head_repo base_ref base_sha head_sha < <(jq -r \
    '[.state, (.head.repo.full_name // ""), .base.ref, .base.sha, .head.sha] | @tsv' <<< "$json")
  [ "$state" = open ] || { skip "$pr" "it is not open."; continue; }
  [ "$head_repo" = "$REPOSITORY" ] || { skip "$pr" "it comes from a fork."; continue; }
  [ "$base_ref" = "$BASE_REF" ] || { skip "$pr" "it targets $base_ref, not $BASE_REF."; continue; }
  # The pull request's own changes are what it adds since its merge base, the same
  # comparison GitHub's Files tab makes.
  merge_base="$(gh api "repos/$REPOSITORY/compare/$base_sha...$head_sha" --jq '.merge_base_commit.sha // ""' 2>/dev/null || true)"
  [ -n "$merge_base" ] || { skip "$pr" "its merge base could not be resolved."; continue; }
  count=$(( count + 1 ))
  jq -n --arg pr "$pr" --arg head_sha "$head_sha" --arg merge_base "$merge_base" \
    --arg title "$(jq -r '.title // ""' <<< "$json")" \
    '{pr_number: $pr, head_sha: $head_sha, merge_base_sha: $merge_base, title: $title}' > "$out/$count.json"
  prs="${prs:+$prs,}$pr"
done < <(grep -oE '[0-9]+' <<< "${line#*preview}")
finish
