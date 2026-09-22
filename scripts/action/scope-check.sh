#!/usr/bin/env bash
# Decides whether a review run can skip the engine: when none of the pull request's changed
# files is one the engine would analyse (docs, config, CI, tests it ignores, a language it
# does not read), the architecture cannot have moved, and the base analysis IS the head
# analysis. The run then publishes the base as the head, renders an empty diff, and says so.
#
# Outputs: changed_files, analysed_files, skip (true only when the shortcut applies), and on
# skip the analysis_path / base_analysis_path / analysis_mode the later steps read in place of
# the analysis step's. Never fails the job: a read that did not answer means "run as usual".
set -euo pipefail

changed=0
analysed=0
skip=false

emit() {
  printf 'changed_files=%s\nanalysed_files=%s\nskip=%s\n' "$changed" "$analysed" "$skip" >> "$GITHUB_OUTPUT"
}

# The checkout is the head commit alone, so the changed files come from the API, not git.
files="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files" --paginate --jq '.[].filename' 2>/dev/null)" || {
  echo "::notice::Could not list the pull request's files; analysing as usual."
  emit; exit 0
}
if [ -z "$files" ]; then
  echo "::notice::The pull request changes no files; analysing as usual."
  emit; exit 0
fi

# From the action's own directory, so the analysed repository's modules cannot shadow the
# engine's on sys.path (see the install step in action.yml).
counts="$(cd "$ACTION_PATH" && printf '%s\n' "$files" | python3 scripts/action/scope_check.py --repo-root "$CHECKOUT_DIR")" || {
  echo "::notice::Could not decide the analysed scope; analysing as usual."
  emit; exit 0
}
changed="$(printf '%s' "$counts" | jq -r '.changed')"
analysed="$(printf '%s' "$counts" | jq -r '.analysed')"
if [ "$changed" -eq 0 ] || [ "$analysed" -gt 0 ]; then
  emit; exit 0
fi

# Nothing analysed changed. The base graph must already exist for the shortcut: a published
# base from the fetch step, or the baseline the sync workflow committed. Computing one would
# be the run this exists to skip.
base=""
if [ -f "${BASE_DIR:-}/analysis.json" ]; then
  base="${BASE_DIR}/analysis.json"
elif [ -f "${CHECKOUT_DIR}/.codeboarding/analysis.json" ]; then
  base="${CHECKOUT_DIR}/.codeboarding/analysis.json"
fi
if [ -z "$base" ]; then
  echo "::notice::No analysed file changed, but no base analysis is at hand; analysing as usual."
  emit; exit 0
fi

work="${RUNNER_TEMP}/codeboarding-scope"
mkdir -p "$work"
cp "$base" "$work/analysis.json"
# The base's health report, when it sits beside the base, describes the head too.
if [ -f "$(dirname "$base")/health/health_report.json" ]; then
  mkdir -p "$work/health"
  cp "$(dirname "$base")/health/health_report.json" "$work/health/health_report.json"
fi
skip=true
echo "::notice::No analysed file changed (${changed} files, all outside the analysed scope); reusing the base analysis as the head."
emit
printf 'analysis_path=%s\nbase_analysis_path=%s\nanalysis_mode=unchanged\n' "$work/analysis.json" "$base" >> "$GITHUB_OUTPUT"
