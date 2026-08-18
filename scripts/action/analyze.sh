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
# Core resolves depth from depth_cap, falling back to depth_level for baselines
# predating it. Use the same value everywhere: a run that stopped short of its
# cap must not be read as a scope change, and rebuilding at the realized depth
# would ratchet the configured depth down every time a full run happens.
depth_cap_from() {
  local analysis="$1"
  [ -f "$analysis" ] || return 0
  python3 -c 'import json,sys
metadata = json.load(open(sys.argv[1])).get("metadata", {})
print(metadata.get("depth_cap", metadata.get("depth_level", "")))' "$analysis" 2>/dev/null || true
}

seed_state() {
  local checkout="$1" state="$2"
  mkdir -p "$state"
  [ ! -d "$checkout/.codeboarding" ] || cp -a "$checkout/.codeboarding/." "$state/"
}

# Records which state this analysis grew from, so a later run can report its
# provenance and the review artifact can answer "which base did we use".
origin_field() {
  local state="$1" field="$2"
  [ -f "$state/origin.json" ] || return 0
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' \
    "$state/origin.json" "$field" 2>/dev/null || true
}
write_origin() {
  local state="$1"
  python3 -c 'import json,os,sys
json.dump({
    "schema": 1,
    "pr_number": os.environ.get("PR_NUMBER", ""),
    "merge_base_sha": os.environ.get("REVIEW_BASE_SHA", ""),
    "head_sha": os.environ.get("REVIEW_HEAD_SHA", ""),
    "engine_version": os.environ.get("ENGINE_VERSION", ""),
    "cfg_hash": os.environ.get("CFG_HASH", ""),
    "seed_source": sys.argv[2],
    "chain_depth": int(sys.argv[3]),
    "base_digest": sys.argv[4],
}, open(sys.argv[1], "w"), indent=2)' "$state/origin.json" "$2" "$3" "$4"
}

# Two independently generated analyses of the same commit need not name the same
# components, so a head that grew from one base cannot be diffed against another.
analysis_digest() {
  local analysis="$1"
  [ -f "$analysis" ] || return 0
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$analysis" | cut -c1-16
  else
    shasum -a 256 "$analysis" | cut -c1-16
  fi
}

cache_out() {
  local state="$1" name="$2"
  [ -n "${CACHE_OUT_DIR:-}" ] || return 0
  mkdir -p "$CACHE_OUT_DIR"
  rm -rf "${CACHE_OUT_DIR:?}/$name"
  cp -a "$state" "$CACHE_OUT_DIR/$name"
}

analyze_sync() {
  local work="$RUNNER_TEMP/codeboarding-sync" state="$RUNNER_TEMP/codeboarding-sync/analysis"
  rm -rf "$work"
  seed_state "$CHECKOUT_DIR" "$state"
  local depth
  depth="$(depth_cap_from "$state/analysis.json")"
  depth="${depth:-2}"

  if [ "${FORCE_FULL,,}" = true ]; then
    full "$CHECKOUT_DIR" "$state" "$depth"
  else
    incremental "$CHECKOUT_DIR" "$state"
    if [ "$REQUIRES_FULL" = true ]; then
      full "$CHECKOUT_DIR" "$state" "$depth"
    fi
  fi
  # Sync already computes the state every review of this branch needs, so leave
  # a copy for them instead of making the first pull request recompute it.
  cache_out "$state" base
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

# The restored chain already matches this engine, config and merge base: the
# cache key pins all three. Depth is read from the baseline, so check it here.
chain_usable() {
  local base_analysis="$1" chain_cap base_cap
  [ "${SEED_MODE:-chain}" = chain ] || return 1
  [ -f "${CACHE_CHAIN_DIR:-}/analysis.json" ] || return 1
  chain_cap="$(depth_cap_from "$CACHE_CHAIN_DIR/analysis.json")"
  base_cap="$(depth_cap_from "$base_analysis")"
  if [ -n "$chain_cap" ] && [ -n "$base_cap" ] && [ "$chain_cap" != "$base_cap" ]; then
    echo "::notice::Analysis depth changed since the last run; re-seeding from the base analysis."
    return 1
  fi
  # The chain descends from one base graph and the diagram is drawn against
  # another only if the base was regenerated in between. Their components need
  # not match, so keeping the chain would report additions and removals for code
  # nobody touched.
  if [ "$(origin_field "$CACHE_CHAIN_DIR" base_digest)" != "$(analysis_digest "$base_analysis")" ]; then
    echo "::notice::The base analysis is not the one this pull request's cached analysis grew from; re-seeding from it."
    return 1
  fi
}

analyze_review() {
  local work="$RUNNER_TEMP/codeboarding-review"
  local base_checkout="$work/base" base_state="$work/base-state" head_state="$work/head-state"
  rm -rf "$work"
  mkdir -p "$work"

  # An exact base-cache hit is this merge base's own analysis, so it needs no
  # engine run at all. A prefix hit is some other commit's baseline: useful as a
  # warm seed, never usable as this comparison's baseline.
  local base_source=cache
  if [ "${CACHE_BASE_HIT:-}" = true ] && [ -f "${CACHE_BASE_DIR:-}/analysis.json" ]; then
    cp -a "$CACHE_BASE_DIR" "$base_state"
  else
    base_source=computed
    fetch_commit "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA"
    git -C "$CHECKOUT_DIR" worktree add --detach "$base_checkout" "$REVIEW_BASE_SHA" >/dev/null
    if [ -f "${CACHE_BASE_DIR:-}/analysis.json" ]; then
      cp -a "$CACHE_BASE_DIR" "$base_state"
    else
      seed_state "$base_checkout" "$base_state"
    fi
    local base_depth
    base_depth="$(depth_cap_from "$base_state/analysis.json")"
    incremental "$base_checkout" "$base_state"
    if [ "$REQUIRES_FULL" = true ]; then
      full "$base_checkout" "$base_state" "${base_depth:-2}"
    fi
  fi
  unset GIT_TOKEN

  local base_analysis="$base_state/analysis.json"
  [ -f "$base_analysis" ] || { echo "::error::Review baseline analysis is missing."; exit 1; }
  local depth
  depth="$(depth_cap_from "$base_analysis")"
  depth="${depth:-2}"

  # Seed the head from this pull request's own last analysis when there is one,
  # so the run only covers commits pushed since it.
  local seed_source=base chain_depth=1 previous_depth
  if chain_usable "$base_analysis"; then
    cp -a "$CACHE_CHAIN_DIR" "$head_state"
    seed_source=pr-chain
    previous_depth="$(origin_field "$head_state" chain_depth)"
    case "$previous_depth" in
      ''|*[!0-9]*) previous_depth=0 ;;
    esac
    chain_depth=$(( previous_depth + 1 ))
  else
    cp -a "$base_state" "$head_state"
  fi
  rm -f "$head_state/origin.json"

  if [ "${SEED_MODE:-chain}" = full ]; then
    full "$CHECKOUT_DIR" "$head_state" "$depth"
  else
    incremental "$CHECKOUT_DIR" "$head_state"
    if [ "$REQUIRES_FULL" = true ]; then
      full "$CHECKOUT_DIR" "$head_state" "$depth"
    fi
  fi

  write_origin "$head_state" "$seed_source" "$chain_depth" "$(analysis_digest "$base_analysis")"
  cache_out "$head_state" chain
  local save_base=false
  if [ "$base_source" = computed ]; then
    cache_out "$base_state" base
    save_base=true
  fi

  printf 'analysis_mode=%s\nanalysis_path=%s\nbase_analysis_path=%s\nseed_source=%s\nchain_depth=%s\nsave_base=%s\n' \
    "$ANALYSIS_MODE" "$ANALYSIS_PATH" "$base_analysis" "$seed_source" "$chain_depth" "$save_base" >> "$GITHUB_OUTPUT"
}

case "$ANALYSIS_KIND" in
  sync) analyze_sync ;;
  review) analyze_review ;;
  *) echo "::error::Unknown analysis kind: $ANALYSIS_KIND"; exit 1 ;;
esac
