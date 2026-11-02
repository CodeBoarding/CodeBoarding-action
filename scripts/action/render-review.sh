#!/usr/bin/env bash
# Renders the base/head analysis diff and outputs its Mermaid path and change counts.
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
# The event guard has validated this before any analysis ran.
COMMENT_DEPTH="${COMMENT_DEPTH:-1}"
render() {
  python3 "$ACTION_PATH/scripts/diff_to_mermaid.py" --base "$BASE_ANALYSIS" --head "$HEAD_ANALYSIS" --out "$DIAGRAM_OUT" --direction LR --render-depth "$1"
}
DIFF="$(render "$COMMENT_DEPTH")"
# Drawing more levels is optional, so it never costs the review: a diagram too large to
# draw that deep, even changed-only, is drawn at the top level instead.
if [ "$COMMENT_DEPTH" != 1 ] && [ "$(jq -r '.rendered or .empty' <<< "$DIFF")" != true ]; then
  echo "::warning::The architecture diff is too large to draw at comment_depth $COMMENT_DEPTH; drawing the top level only."
  DIFF="$(render 1 | jq -c '.truncated = true')"
fi
printf '%s' "$DIFF" > "$META"

read -r N_CHANGED TRUNCATED RENDERED EMPTY ANALYSED_FILES_CHANGED < <(jq -r '[.n_changed, .truncated, .rendered, .empty, (.analysed_files_changed // "unknown")] | @tsv' "$META")
[ "$RENDERED" = true ] || [ "$EMPTY" = true ] || { echo "::error::The architecture diff is too large to render."; exit 1; }
{
  echo "diagram_md=$DIAGRAM_OUT"
  echo "n_changed=$N_CHANGED"
  echo "truncated=$TRUNCATED"
  echo "analysed_files_changed=$ANALYSED_FILES_CHANGED"
} >> "$GITHUB_OUTPUT"
