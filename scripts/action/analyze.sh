#!/usr/bin/env bash
# Runs incremental/full Core analysis and outputs the selected analysis paths and mode.
set -euo pipefail
parse_output() {
  local output="$1"
  ANALYSIS_MODE="$(awk -F= '$1 == "analysis_mode" {print $2; exit}' <<< "$output")"
  REQUIRES_FULL="$(awk -F= '$1 == "requires_full_analysis" {print $2; exit}' <<< "$output")"
  ANALYSIS_PATH="$(awk -F= '$1 == "analysis_path" {print substr($0, index($0, "=") + 1); exit}' <<< "$output")"
}
incremental() {
  local checkout="$1" output_dir="$2" output
  output="$(python3 "$ACTION_PATH/scripts/analyze_repository.py" incremental \
    --checkout "$checkout" --output-dir "$output_dir")"
  parse_output "$output"
  if [ "$ANALYSIS_MODE" != incremental ] || { [ "$REQUIRES_FULL" != true ] && [ ! -f "$ANALYSIS_PATH" ]; }; then
    echo "::error::Invalid incremental-analysis result."
    exit 1
  fi
}
full() {
  local checkout="$1" output_dir="$2" depth="$3" output
  output="$(python3 "$ACTION_PATH/scripts/analyze_repository.py" full \
    --checkout "$checkout" --output-dir "$output_dir" --depth-level "$depth")"
  parse_output "$output"
  if [ "$ANALYSIS_MODE" != full ] || [ ! -f "$ANALYSIS_PATH" ]; then
    echo "::error::Invalid full-analysis result."
    exit 1
  fi
}
depth_from() {
  local analysis="$1"
  [ -f "$analysis" ] || return 0
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("metadata", {}).get("depth_level", ""))' \
    "$analysis" 2>/dev/null || true
}

seed_state() {
  local checkout="$1" state="$2"
  mkdir -p "$state"
  [ ! -d "$checkout/.codeboarding" ] || cp -a "$checkout/.codeboarding/." "$state/"
}

analyze_sync() {
  local work="$RUNNER_TEMP/codeboarding-sync" state="$RUNNER_TEMP/codeboarding-sync/analysis"
  rm -rf "$work"
  seed_state "$CHECKOUT_DIR" "$state"
  local depth
  depth="$(depth_from "$state/analysis.json")"
  depth="${depth:-2}"

  if [ "${FORCE_FULL,,}" = true ]; then
    full "$CHECKOUT_DIR" "$state" "$depth"
  else
    incremental "$CHECKOUT_DIR" "$state"
    if [ "$REQUIRES_FULL" = true ]; then
      full "$CHECKOUT_DIR" "$state" "$depth"
    fi
  fi
  printf 'analysis_mode=%s\nanalysis_path=%s\nanalysis_dir=%s\n' \
    "$ANALYSIS_MODE" "$ANALYSIS_PATH" "$state" >> "$GITHUB_OUTPUT"
}

fetch_commit() {
  local repository="$1" sha="$2"
  git -C "$CHECKOUT_DIR" cat-file -e "$sha^{commit}" 2>/dev/null && return 0
  local auth
  auth="$(printf 'x-access-token:%s' "$GIT_TOKEN" | base64 -w0)"
  git -C "$CHECKOUT_DIR" -c "http.extraheader=AUTHORIZATION: basic $auth" fetch \
    "${GITHUB_SERVER_URL%/}/${repository}.git" "$sha" --depth=1
}

analyze_review() {
  local work="$RUNNER_TEMP/codeboarding-review"
  local base_checkout="$work/base" base_state="$work/base-state" head_state="$work/head-state"
  rm -rf "$work"
  mkdir -p "$work"
  fetch_commit "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA"
  unset GIT_TOKEN
  git -C "$CHECKOUT_DIR" worktree add --detach "$base_checkout" "$REVIEW_BASE_SHA" >/dev/null
  seed_state "$base_checkout" "$base_state"

  local depth
  depth="$(depth_from "$base_state/analysis.json")"
  depth="${depth:-2}"
  incremental "$base_checkout" "$base_state"
  if [ "$REQUIRES_FULL" = true ]; then
    full "$base_checkout" "$base_state" "$depth"
  fi
  local base_analysis="$base_state/analysis.json"
  cp -a "$base_state" "$head_state"
  incremental "$CHECKOUT_DIR" "$head_state"

  if [ "$REQUIRES_FULL" = true ]; then
    full "$CHECKOUT_DIR" "$head_state" "$depth"
  fi

  printf 'analysis_mode=%s\nanalysis_path=%s\nbase_analysis_path=%s\n' \
    "$ANALYSIS_MODE" "$ANALYSIS_PATH" "$base_analysis" >> "$GITHUB_OUTPUT"
}

case "$ANALYSIS_KIND" in
  sync) analyze_sync ;;
  review) analyze_review ;;
  *) echo "::error::Unknown analysis kind: $ANALYSIS_KIND"; exit 1 ;;
esac
