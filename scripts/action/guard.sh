#!/usr/bin/env bash
# Validates the event and outputs the exact refs and metadata used by later steps.
set -euo pipefail
fail() { echo "::error::$1"; exit 1; }
skip() { echo "::notice::$1"; echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0; }
[ -z "${GH_HOST:-}" ] || export GH_HOST="${GH_HOST#*://}"
case "$MODE" in
  review|sync) ;;
  *) fail "mode must be review or sync." ;;
esac
printf 'mode=%s\nskip=false\nevent=%s\n' "$MODE" "$EVENT" >> "$GITHUB_OUTPUT"
if [ "$MODE" = sync ]; then
  case "$EVENT" in
    push|workflow_dispatch|schedule) ;;
    *) skip "Sync mode ignores $EVENT events." ;;
  esac
  [ "$REF_TYPE" != tag ] || skip "Sync mode ignores tag pushes."
  case "$SYNC_STRATEGY" in
    push|pull_request) ;;
    *) fail "sync_strategy must be push or pull_request." ;;
  esac
  case "$HEAD_AUTHOR_EMAIL" in
    codeboarding-review\[bot\]@users.noreply.github.com|codeboarding\[bot\]@users.noreply.github.com)
      [ "$EVENT" != push ] || skip "Ignoring CodeBoarding's own baseline commit."
      ;;
  esac
  target_branch="${TARGET_BRANCH_INPUT:-$REF_NAME}"
  [ -n "$target_branch" ] || fail "target_branch is required for this event."
  [ "$SYNC_STRATEGY" != pull_request ] || [ "$target_branch" != codeboarding/sync ] || fail "target_branch must differ from codeboarding/sync."
  sync_branch_start_sha=""
  if [ "$SYNC_STRATEGY" = pull_request ]; then
    sync_branch_start_sha="$(gh api "repos/$REPOSITORY/branches/codeboarding%2Fsync" --jq '.commit.sha' 2>/dev/null || true)"
  fi
  {
    echo "target_branch=$target_branch"
    echo "sync_branch_start_sha=$sync_branch_start_sha"
    echo "checkout_repo=$REPOSITORY"
    echo "checkout_ref=$target_branch"
  } >> "$GITHUB_OUTPUT"
  exit 0
fi

# Validated here rather than read straight from the input, so a typo fails the
# run with a reason instead of silently posting when the author asked for
# silence. The reaction on a /codeboarding comment is not a comment and stays:
# with posting off it is the only sign the command was picked up.
post_comment="${POST_COMMENT_INPUT:-true}"
case "$post_comment" in
  true|false) ;;
  *) fail "post_comment must be true or false." ;;
esac

seed_mode=chain
case "$EVENT" in
  pull_request|pull_request_target)
    pr_number="$EVENT_PR_NUMBER"
    base_sha="$PULL_BASE_SHA"
    head_sha="$PULL_HEAD_SHA"
    base_repo="$PULL_BASE_REPO"
    head_repo="$PULL_HEAD_REPO"
    base_ref="${PULL_BASE_REF:-}"
    ;;
  issue_comment)
    read -r first_word second_word <<< "$(printf '%s' "$COMMENT_BODY" | tr -d '\r' | awk 'NR == 1 {print $1, $2}')"
    [ "$first_word" = /codeboarding ] || skip "Comment is not a /codeboarding command."
    case "$AUTHOR_ASSOCIATION" in
      OWNER|MEMBER|COLLABORATOR) ;;
      *) skip "Only trusted collaborators may run /codeboarding." ;;
    esac
    [ -n "$ISSUE_PR_URL" ] || skip "The command was not posted on a pull request."
    # refresh ignores the pull request's own cached analysis and re-seeds from
    # the base; full additionally forces a from-scratch head analysis.
    case "$second_word" in
      "") ;;
      refresh|full) seed_mode="$second_word" ;;
      *) echo "::warning::Unknown /codeboarding argument '$second_word'; running the default incremental review." ;;
    esac
    pr_json="$(gh api "$ISSUE_PR_URL")"
    pr_number="$(jq -r '.number // empty' <<< "$pr_json")"
    base_sha="$(jq -r '.base.sha // empty' <<< "$pr_json")"
    head_sha="$(jq -r '.head.sha // empty' <<< "$pr_json")"
    base_repo="$(jq -r '.base.repo.full_name // empty' <<< "$pr_json")"
    head_repo="$(jq -r '.head.repo.full_name // empty' <<< "$pr_json")"
    base_ref="$(jq -r '.base.ref // empty' <<< "$pr_json")"
    ;;
  *) skip "Review mode ignores $EVENT events." ;;
esac

if [ -z "$pr_number" ] || [ -z "$base_sha" ] || [ -z "$head_sha" ] || [ -z "$base_repo" ] || [ -z "$head_repo" ]; then
  fail "Could not resolve the pull request base and head."
fi
[ "$head_repo" = "$base_repo" ] || [ "$EVENT" = issue_comment ] || skip "Fork pull requests require a trusted /codeboarding command."

# The event's base sha is the base branch tip, so it moves whenever anyone else
# pushes. Analyzing against it reports their commits as this pull request's
# changes. The merge base is what GitHub's own file diff uses, and it only moves
# when this pull request actually rebases or merges the base in.
merge_base_sha="$base_sha"
behind_by=0
basehead="$base_sha...$head_sha"
[ "$head_repo" = "$base_repo" ] || basehead="$base_sha...${head_repo%%/*}:$head_sha"
merge_base_resolved=false
for attempt in 1 2; do
  compare="$(gh api "repos/$base_repo/compare/$basehead" \
    --jq '[.merge_base_commit.sha // "", .behind_by // 0] | @tsv' 2>/dev/null || true)"
  # Split on the tab explicitly. Field splitting would drop an empty merge base
  # and shift the distance into its place, which reads as a valid commit.
  compare_merge_base="${compare%%$'\t'*}"
  compare_behind="${compare#*$'\t'}"
  # A transient failure of one API call should not decide how this pull request
  # is measured, so try once more before giving up on the merge base.
  [ -z "$compare_merge_base" ] && [ "$attempt" = 1 ] || break
  sleep 2
done
case "$compare_behind" in
  ''|*[!0-9]*) compare_behind=0 ;;
esac
# The distance only means anything alongside the merge base it was measured
# against, so take both or neither: reporting one while comparing against the
# tip would describe a comparison this run did not make.
if [ -n "$compare_merge_base" ]; then
  merge_base_resolved=true
  merge_base_sha="$compare_merge_base"
  behind_by="$compare_behind"
else
  # Falling back keeps reviews working through an API outage, but the result is
  # the comparison this change exists to avoid, so it is stated in the comment
  # rather than left in the log.
  echo "::warning::Could not resolve the merge base; comparing against the tip of $base_ref instead."
fi

comment_id=codeboarding-review
[ "$EVENT" != issue_comment ] || comment_id="codeboarding-review-${GITHUB_RUN_ID}"
is_fork=false
[ "$head_repo" = "$base_repo" ] || is_fork=true
{
  echo "pr_number=$pr_number"
  echo "base_sha=$base_sha"
  echo "merge_base_sha=$merge_base_sha"
  echo "merge_base_resolved=$merge_base_resolved"
  echo "behind_by=$behind_by"
  echo "base_ref=$base_ref"
  echo "head_sha=$head_sha"
  echo "base_repo=$base_repo"
  echo "head_repo=$head_repo"
  echo "checkout_repo=$head_repo"
  echo "checkout_ref=$head_sha"
  echo "comment_id=$comment_id"
  echo "seed_mode=$seed_mode"
  echo "post_comment=$post_comment"
  echo "is_fork=$is_fork"
} >> "$GITHUB_OUTPUT"
