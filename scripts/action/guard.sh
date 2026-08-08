#!/usr/bin/env bash
set -euo pipefail
fail() { echo "::error::$1"; exit 1; }
skip() { echo "::notice::$1"; echo "skip=true" >> "$GITHUB_OUTPUT"; exit 0; }
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
    export GH_HOST="${GH_HOST#*://}"
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

case "$EVENT" in
  pull_request)
    pr_number="$EVENT_PR_NUMBER"
    base_sha="$PULL_BASE_SHA"
    head_sha="$PULL_HEAD_SHA"
    base_repo="$PULL_BASE_REPO"
    head_repo="$PULL_HEAD_REPO"
    ;;
  issue_comment)
    first_word="$(printf '%s' "$COMMENT_BODY" | tr -d '\r' | awk 'NR == 1 {print $1}')"
    [ "$first_word" = /codeboarding ] || skip "Comment is not a /codeboarding command."
    case "$AUTHOR_ASSOCIATION" in
      OWNER|MEMBER|COLLABORATOR) ;;
      *) skip "Only trusted collaborators may run /codeboarding." ;;
    esac
    [ -n "$ISSUE_PR_URL" ] || skip "The command was not posted on a pull request."
    export GH_HOST="${GH_HOST#*://}"
    pr_json="$(gh api "$ISSUE_PR_URL")"
    pr_number="$(jq -r '.number // empty' <<< "$pr_json")"
    base_sha="$(jq -r '.base.sha // empty' <<< "$pr_json")"
    head_sha="$(jq -r '.head.sha // empty' <<< "$pr_json")"
    base_repo="$(jq -r '.base.repo.full_name // empty' <<< "$pr_json")"
    head_repo="$(jq -r '.head.repo.full_name // empty' <<< "$pr_json")"
    ;;
  *) skip "Review mode ignores $EVENT events." ;;
esac

if [ -z "$pr_number" ] || [ -z "$base_sha" ] || [ -z "$head_sha" ] || [ -z "$base_repo" ] || [ -z "$head_repo" ]; then
  fail "Could not resolve the pull request base and head."
fi
[ "$head_repo" = "$base_repo" ] || [ "$EVENT" = issue_comment ] || skip "Fork pull requests require a trusted /codeboarding command."

comment_id=codeboarding-review
[ "$EVENT" != issue_comment ] || comment_id="codeboarding-review-${GITHUB_RUN_ID}"
{
  echo "pr_number=$pr_number"
  echo "base_sha=$base_sha"
  echo "head_sha=$head_sha"
  echo "base_repo=$base_repo"
  echo "head_repo=$head_repo"
  echo "checkout_repo=$head_repo"
  echo "checkout_ref=$head_sha"
  echo "comment_id=$comment_id"
} >> "$GITHUB_OUTPUT"
