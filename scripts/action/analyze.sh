#!/usr/bin/env bash
# Analyzes one checkout into a state directory: incrementally when the state holds
# a compatible analysis to continue from, in full otherwise or when Core asks for it.
#
# Usage: analyze.sh base|head|sync <checkout> <state>
# Prints analysis_mode= and analysis_path= on stdout; everything else goes to stderr.
set -euo pipefail
source "$(dirname "$0")/analysis-state.sh"
role="$1" checkout="$2" state="$3"
# Stdout is the result, so the error goes to stderr; the reason goes to the step's
# outputs for the review comment and the sync summary.
fail() { echo "::error::$1" >&2; echo "failure_reason=$1" >> "${GITHUB_OUTPUT:-/dev/null}"; exit 1; }

# Tags Core's own telemetry with this run and the analysis it belongs to, so its
# events can be read per run: a review with no base events reused its base, one
# with base events caught it up or built it in full.
run_id=""
[ -z "${GITHUB_RUN_ID:-}" ] || run_id="gh-$GITHUB_RUN_ID-${GITHUB_RUN_ATTEMPT:-1}-$role"

ANALYSIS_MODE="" REQUIRES_FULL="" ANALYSIS_PATH=""
run_core() {
  local output
  output="$(CODEBOARDING_RUN_ID="$run_id" python3 "$ACTION_PATH/scripts/analyze_repository.py" "$@" \
    --checkout "$checkout" --output-dir "$state")"
  ANALYSIS_MODE="$(awk -F= '$1 == "analysis_mode" {print $2; exit}' <<< "$output")"
  REQUIRES_FULL="$(awk -F= '$1 == "requires_full_analysis" {print $2; exit}' <<< "$output")"
  ANALYSIS_PATH="$(awk -F= '$1 == "analysis_path" {print substr($0, index($0, "=") + 1); exit}' <<< "$output")"
}

mkdir -p "$state"
if compatible_state "$state"; then
  run_core incremental
  if [ "$ANALYSIS_MODE" != incremental ] || { [ "$REQUIRES_FULL" != true ] && [ ! -f "$ANALYSIS_PATH" ]; }; then
    fail "Core returned an invalid incremental-analysis result."
  fi
  [ "$REQUIRES_FULL" != true ] || echo "::notice::Core cannot update the $role analysis incrementally; analyzing in full." >&2
fi
if [ "$ANALYSIS_MODE" != incremental ] || [ "$REQUIRES_FULL" = true ]; then
  run_core full --depth-cap "$DEPTH_CAP"
  if [ "$ANALYSIS_MODE" != full ] || [ ! -f "$ANALYSIS_PATH" ]; then
    fail "Core returned an invalid full-analysis result."
  fi
fi
printf 'analysis_mode=%s\nanalysis_path=%s\n' "$ANALYSIS_MODE" "$ANALYSIS_PATH"
