#!/usr/bin/env bash
set -euo pipefail
BASE_ANALYSIS="$BASE_ANALYSIS_PATH"
HEAD_ANALYSIS="$HEAD_ANALYSIS_PATH"

if [ -z "$BASE_ANALYSIS" ] || [ ! -f "$BASE_ANALYSIS" ]; then
  echo "::error::Review baseline missing."
  exit 1
fi
if [ -z "$HEAD_ANALYSIS" ] || [ ! -f "$HEAD_ANALYSIS" ]; then
  echo "::error::Review head analysis missing."
  exit 1
fi

DIAGRAM_OUT="${RUNNER_TEMP}/diagram.md"
META="${RUNNER_TEMP}/diagram_meta.json"
DIFF="$(python3 "$ACTION_PATH/scripts/diff_to_mermaid.py" --base "$BASE_ANALYSIS" --head "$HEAD_ANALYSIS" --out "$DIAGRAM_OUT" --direction LR --render-depth 1)"
printf '%s' "$DIFF" > "$META"

N_CHANGED="$(jq -r '.n_changed' "$META")"
TRUNCATED="$(jq -r '.truncated | tostring' "$META")"
RENDERED="$(jq -r '.rendered | tostring' "$META")"
[ "$RENDERED" = true ] || { echo "::error::The architecture diff is too large to render."; exit 1; }
{
  echo "diagram_md=$DIAGRAM_OUT"
  echo "n_changed=$N_CHANGED"
  echo "truncated=$TRUNCATED"
} >> "$GITHUB_OUTPUT"
