#!/usr/bin/env bash
# Analyzes the pull request head. It continues from this pull request's last
# analysis when that grew from the same base graph, so a push only costs the
# commits pushed since; otherwise it starts from the base.
set -euo pipefail
source "$(dirname "$0")/analysis-state.sh"
base_analysis="$BASE_ANALYSIS_PATH"
state="$RUNNER_TEMP/codeboarding-review/head-state"
rm -rf "$state"

# The artifact name pins configuration; verify the stored cap and lineage too.
warmstart_usable() {
  [ -f "${WARMSTART_DIR:-}/analysis.json" ] || return 1
  if ! compatible_state "$WARMSTART_DIR"; then
    echo "::notice::Analysis depth changed since the last run; re-seeding from the base analysis."
    return 1
  fi
  # A head that grew from one base graph cannot be diffed against another: two
  # runs of the engine over one commit need not name components identically, so
  # keeping it would report additions and removals for code nobody touched.
  if [ "$(origin_field "$WARMSTART_DIR" base_digest)" != "$(analysis_digest "$base_analysis")" ]; then
    echo "::notice::The base analysis is not the one this pull request's stored analysis grew from; re-seeding from it."
    return 1
  fi
}

seed_source=base chain_depth=1
if warmstart_usable; then
  cp -a "$WARMSTART_DIR" "$state"
  seed_source=pr-chain
  previous_depth="$(origin_field "$state" chain_depth)"
  case "$previous_depth" in
    '' | *[!0-9]*) previous_depth=0 ;;
  esac
  chain_depth=$(( previous_depth + 1 ))
else
  cp -a "$(dirname "$base_analysis")" "$state"
fi
rm -f "$state/origin.json"

unset GIT_TOKEN
result="$("$ACTION_PATH/scripts/action/analyze.sh" head "$CHECKOUT_DIR" "$state")"
write_origin "$state" "$seed_source" "$chain_depth" "$(analysis_digest "$base_analysis")"
stage "$state" warmstart

{
  echo "$result"
  printf 'seed_source=%s\nchain_depth=%s\n' "$seed_source" "$chain_depth"
} >> "$GITHUB_OUTPUT"
