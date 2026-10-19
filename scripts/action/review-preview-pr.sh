#!/usr/bin/env bash
# Reviews the n-th pull request a setup PR asked to preview (see resolve-preview-prs.sh).
#
# The run has one analysis to start from: the commit the setup PR is compared against.
# Reviewing a pull request at its own merge base would need a second full analysis of an
# older commit, so instead its own changes (merge base to head, as GitHub's Files tab shows
# them) are applied to that commit and analysed incrementally from the analysis in hand.
# Only the pull request's own work differs between the two, so nobody else's commits are
# reported as its changes. A pull request that does not apply cleanly there is skipped.
#
# Usage: review-preview-pr.sh <n>. Outputs pr_number=, title= and artifact_dir= when a
# preview was built; nothing when there is no n-th entry or it was skipped.
set -euo pipefail
# shellcheck source=scripts/action/analysis-state.sh
source "$(dirname "$0")/analysis-state.sh"
n="$1"
dir="${RUNNER_TEMP:?}/codeboarding-preview"
entry="$dir/$n.json"
[ -f "$entry" ] || exit 0
IFS=$'\t' read -r pr head_sha pr_merge_base < <(jq -r '[.pr_number, .head_sha, .merge_base_sha] | @tsv' "$entry")
title="$(jq -r '.title' "$entry")"
base="$REVIEW_BASE_SHA"

fetch_commit "$REPOSITORY" "$base" >&2
fetch_commit "$REPOSITORY" "$pr_merge_base" >&2
fetch_commit "$REPOSITORY" "$head_sha" >&2
tree="$dir/tree-$n"
rm -rf "$tree"
git -C "$CHECKOUT_DIR" worktree add --detach "$tree" "$base" >/dev/null 2>&1
if ! git -C "$CHECKOUT_DIR" diff --binary "$pr_merge_base" "$head_sha" |
  git -C "$tree" apply --3way --whitespace=nowarn >/dev/null 2>&1; then
  echo "::notice::#$pr does not apply cleanly to ${base:0:7}, the commit this map describes; not previewing it."
  exit 0
fi

state="$dir/state-$n"
rm -rf "$state"
cp -a "$(dirname "$BASE_ANALYSIS_PATH")" "$state"
rm -f "$state/origin.json"
unset GIT_TOKEN
result="$("$ACTION_PATH/scripts/action/analyze.sh" head "$tree" "$state")"
analysis_path="$(awk -F= '$1 == "analysis_path" {print substr($0, index($0, "=") + 1); exit}' <<< "$result")"
analysis_mode="$(awk -F= '$1 == "analysis_mode" {print $2; exit}' <<< "$result")"

meta="$(python3 "$ACTION_PATH/scripts/diff_to_mermaid.py" --base "$BASE_ANALYSIS_PATH" --head "$analysis_path" \
  --out "$dir/diagram-$n.md" --direction LR --render-depth 1)"
analysed="$(jq -r '.analysed_files_changed // "unknown"' <<< "$meta")"

# Point at the same base graph the setup PR's review does, inlined when it does.
review_meta="$REVIEW_ARTIFACT_DIR/metadata.json"
inline=false
[ ! -f "$REVIEW_ARTIFACT_DIR/base_analysis.json" ] || inline=true
# The packaging step writes artifact_dir= to this step's outputs.
BASE_ARTIFACT_NAME="$(jq -r '.base_artifact // ""' "$review_meta")" \
  BASE_ARTIFACT_ID="$(jq -r '.base_artifact_id // ""' "$review_meta")" INLINE_BASE="$inline" \
  ARTIFACT_DIR="$dir/artifact-$n" ANALYSIS_PATH="$analysis_path" ANALYSIS_MODE="$analysis_mode" \
  BASE_SHA="$base" MERGE_BASE_SHA="$base" MERGE_BASE_RESOLVED=true HEAD_SHA="$head_sha" \
  PR_NUMBER="$pr" SEED_SOURCE=base CHAIN_DEPTH=1 ANALYSED_FILES_CHANGED="$analysed" \
  BASIS=preview SETUP_PR="$SETUP_PR" \
  "$ACTION_PATH/scripts/action/build-review-artifact.sh"
printf 'pr_number=%s\ntitle=%s\n' "$pr" "${title//$'\n'/ }" >> "$GITHUB_OUTPUT"
