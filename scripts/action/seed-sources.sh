# shellcheck shell=bash
# Where an analysis to continue from can come from. Sourced, not run.
#
# Each from_* function fills a state directory with a compatible analysis of a
# commit or one of its ancestors and returns 0, or leaves the directory alone and
# returns 1. SEED_SHA is the commit the seed describes, when the source knows it;
# a seed of the very commit being analyzed needs no engine run at all.
HERE="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=scripts/action/analysis-state.sh
source "$HERE/analysis-state.sh"
# shellcheck source=scripts/action/codeboarding-baseline.sh
source "$HERE/codeboarding-baseline.sh"
SEED_SHA=""
# Set by reuse_other_configuration; analyze.sh then goes straight to a full run.
export SEED_NAMES_ONLY=false

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

# The analysis committed under .codeboarding/ on the parent of $2, when $2 deleted
# it: the commit that moves a repository to the analysis branch does, and would
# otherwise cost the first sync a full analysis.
from_parent_committed_baseline() {
  local repository="$1" tip="$2" state="$3" checkout="$4" parent dest="$RUNNER_TEMP/codeboarding-parent-baseline"
  [ ! -e "$checkout/.codeboarding/analysis.json" ] && [ -n "$repository" ] || return 1
  fetch_commit "$repository" "$tip" 2 >&2 || return 1
  parent="$(git -C "$checkout" rev-parse -q --verify "$tip^")" || return 1
  git -C "$checkout" cat-file -e "$parent:.codeboarding/analysis.json" 2>/dev/null || return 1
  rm -rf "$dest"
  mkdir -p "$dest"
  git -C "$checkout" archive "$parent" .codeboarding | tar -x -C "$dest" || return 1
  if ! compatible_state "$dest/.codeboarding"; then
    echo "::notice::The analysis committed on ${parent:0:7} was made with another depth; not starting from it."
    return 1
  fi
  replace_state "$dest/.codeboarding" "$state" "$checkout"
  echo "::notice::Starting from the analysis committed on ${parent:0:7}, which ${tip:0:7} removed."
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
  # shellcheck disable=SC2034 # read by the scripts that source this file
  SEED_SHA="$ancestor"
  echo "::notice::Starting from the saved analysis of ${ancestor:0:7}."
}

# With no analysis to continue from, as after a release, the nearest base saved
# for $2 or an ancestor under any configuration still lends $3 what outlives a
# configuration: the component names in its analysis.json, which Core keeps for
# the components the full run continues, and its static analysis, which Core
# refuses itself when the cache version changed. Neither is engine state to
# continue from, so analyze.sh runs in full. $3 already holds the analysed
# commit's user configuration. Best effort.
reuse_other_configuration() {
  local repository="$1" tip="$2" state="$3" found seed="" source="" dest="$RUNNER_TEMP/codeboarding-other-configuration"
  [ -n "$tip" ] || return 0
  if [ -n "${CFG_HASH:-}" ] && CFG_HASH='*' restore_codeboarding_baseline "$repository" "$tip" "$dest"; then
    seed="$dest/.codeboarding" source="$BASELINE_SOURCE"
  elif [ "${ANCESTOR_LOOKUP:-false}" = true ] && [ -n "${REPOSITORY:-}" ]; then
    fetch_commit "$repository" "$tip" "$(( CATCHUP_BOUND + 1 ))" || true
    found="$(GH_TOKEN="${GIT_TOKEN:-}" GH_ENTERPRISE_TOKEN="${GIT_TOKEN:-}" TIP_SHA="$tip" DEST="$dest" \
      ANY_CONFIG=true INCLUDE_TIP=true CATCHUP_BOUND="$CATCHUP_BOUND" \
      "$HERE/find-ancestor-base.sh" || true)"
    seed="$dest" source="$(awk -F= '$1 == "ancestor_sha" {print $2; exit}' <<< "$found")"
  fi
  [ -n "$source" ] || return 0
  if cp "$seed/analysis.json" "$state/"; then
    SEED_NAMES_ONLY=true
    echo "::notice::Component names carry over from the analysis of ${source:0:7}, saved under another configuration."
  fi
  # Core keeps a cached file whatever today's ignore rules say, so a cache made
  # under other rules would keep newly ignored files and miss newly analysed ones.
  if { [ -e "$seed/.codeboardingignore" ] || [ -e "$state/.codeboardingignore" ]; } &&
    ! cmp -s "$seed/.codeboardingignore" "$state/.codeboardingignore"; then
    echo "::notice::The saved static analysis of ${source:0:7} was made with another .codeboardingignore; not reusing it."
    return 0
  fi
  if ! cp "$seed/static_analysis.pkl" "$seed/static_analysis.sha" "$state/" 2>/dev/null; then
    rm -f "$state/static_analysis.pkl" "$state/static_analysis.sha"
    return 0
  fi
  echo "::notice::Reusing the saved static analysis of ${source:0:7}."
}
