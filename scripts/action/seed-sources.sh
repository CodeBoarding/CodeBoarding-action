# Where an analysis to continue from can come from. Sourced, not run.
#
# Each from_* function fills a state directory with a compatible analysis of a
# commit or one of its ancestors and returns 0, or leaves the directory alone and
# returns 1. SEED_SHA is the commit the seed describes, when the source knows it;
# a seed of the very commit being analyzed needs no engine run at all.
HERE="$(dirname "${BASH_SOURCE[0]}")"
source "$HERE/analysis-state.sh"
source "$HERE/codeboarding-baseline.sh"
SEED_SHA=""

# The analysis sync committed under .codeboarding/ on the branch itself. Which
# commit it describes is not recorded, so it is always caught up.
from_committed_baseline() {
  local checkout="$1" state="$2"
  [ -f "$checkout/.codeboarding/analysis.json" ] || return 1
  if ! compatible_state "$checkout/.codeboarding"; then
    echo "::notice::The analysis committed on this branch was made with another depth; not starting from it."
    return 1
  fi
  replace_state "$checkout/.codeboarding" "$state" "$checkout"
  echo "::notice::Starting from the analysis committed on this branch."
}

# A codeboarding-base-<cfg>-<sha> artifact for the nearest first-parent ancestor
# of $2, or of $2 itself when $4 is true. A review already looked its tip up by
# exact name; sync has no such lookup.
from_ancestor_artifact() {
  local repository="$1" tip="$2" state="$3" include_tip="$4" config_from="$5" found ancestor dest="$RUNNER_TEMP/codeboarding-ancestor"
  [ "${ANCESTOR_LOOKUP:-false}" = true ] && [ -n "${CFG_HASH:-}" ] && [ -n "${REPOSITORY:-}" ] && [ -n "$tip" ] || return 1
  fetch_commit "$repository" "$tip" "$(( CATCHUP_BOUND + 1 ))" || true
  found="$(GH_TOKEN="${GIT_TOKEN:-}" GH_ENTERPRISE_TOKEN="${GIT_TOKEN:-}" TIP_SHA="$tip" DEST="$dest" \
    INCLUDE_TIP="$include_tip" CATCHUP_BOUND="$CATCHUP_BOUND" \
    "$HERE/find-ancestor-base.sh" || true)"
  ancestor="$(awk -F= '$1 == "ancestor_sha" {print $2; exit}' <<< "$found")"
  [ -n "$ancestor" ] || return 1
  if ! compatible_state "$dest"; then
    echo "::notice::The saved analysis of ${ancestor:0:7} was made with another depth; not starting from it."
    return 1
  fi
  replace_state "$dest" "$state" "$config_from"
  SEED_SHA="$ancestor"
  echo "::notice::Starting from the saved analysis of ${ancestor:0:7}."
}
