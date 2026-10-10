#!/usr/bin/env bash
# Translates deprecated inputs into the current ones, and is the only place that
# knows they exist. Every later step reads this step's outputs, never the inputs
# themselves, so no other script carries backward-compatibility logic.
#
#   target_branch  -> synced_branch
#   sync_strategy  -> save_baseline_to   (push -> synced_branch, pull_request -> pull_request)
#
# Delete a mapping, and its input in action.yml, once no supported workflow sets it.
set -euo pipefail
fail() { echo "::error::$1"; exit 1; }

synced_branch="${SYNCED_BRANCH:-}"
if [ -n "${OLD_TARGET_BRANCH:-}" ]; then
  [ -z "$synced_branch" ] || fail "Set synced_branch only; target_branch is its deprecated name."
  echo "::warning::target_branch is deprecated; rename it to synced_branch."
  synced_branch="$OLD_TARGET_BRANCH"
fi

save_baseline_to="${SAVE_BASELINE_TO:-}"
if [ -n "${OLD_SYNC_STRATEGY:-}" ]; then
  # save_baseline_to has a default, so only a value other than it is a conflict.
  [ "$save_baseline_to" = synced_branch ] || fail "Set save_baseline_to only; sync_strategy is its deprecated name."
  case "$OLD_SYNC_STRATEGY" in
    push) save_baseline_to=synced_branch ;;
    pull_request) save_baseline_to=pull_request ;;
    *) fail "sync_strategy is deprecated and only accepts push or pull_request; use save_baseline_to." ;;
  esac
  echo "::warning::sync_strategy is deprecated; use save_baseline_to: $save_baseline_to."
fi

printf 'synced_branch=%s\nsave_baseline_to=%s\n' "$synced_branch" "$save_baseline_to" >> "$GITHUB_OUTPUT"
