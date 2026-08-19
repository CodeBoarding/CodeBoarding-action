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

# Lays out what the upload steps publish. The warm-start bundle is a complete
# working directory rather than the pickle alone, so restoring it needs one
# lookup and no correlating of two artifacts.
stage() {
  local state="$1" kind="$2"
  [ -n "${STAGE_DIR:-}" ] || return 0
  rm -rf "${STAGE_DIR:?}/$kind"
  mkdir -p "$STAGE_DIR"
  cp -a "$state" "$STAGE_DIR/$kind"
  # Say what this bundle is. Without it a base bundle is an analysis.json and
  # nothing else, which unpacks exactly like a head artifact and would be
  # rendered as one by a reader that fetched the wrong name. Written at staging
  # time rather than carried in the state directory, so a bundle can never
  # inherit the label of the one it was seeded from.
  python3 -c 'import json,os,sys
kind = sys.argv[2]
marker = {
    "kind": kind,
    "engine_version": os.environ.get("ENGINE_VERSION", ""),
    "cfg_hash": os.environ.get("CFG_HASH", ""),
    "merge_base_sha": os.environ.get("REVIEW_BASE_SHA", ""),
}
# A base describes one commit and is shared by every pull request that forks
# there, so the run that happened to compute it is not part of its identity.
if kind == "warmstart":
    marker["pr_number"] = os.environ.get("PR_NUMBER", "")
    marker["head_sha"] = os.environ.get("REVIEW_HEAD_SHA", "")
json.dump(marker, open(sys.argv[1], "w"), indent=2)' "$STAGE_DIR/$kind/metadata.json" "$kind"
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
  # Sync already computes the graph every review of this branch compares against,
  # so publish it instead of making the first pull request recompute it.
  stage "$state" base
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

# The artifact name pins the engine, the analysis scope and the models. Depth and
# lineage are read from the bundle itself, so they are checked here.
warmstart_usable() {
  local base_analysis="$1" bundle_cap base_cap
  [ "${SEED_MODE:-chain}" = chain ] || return 1
  [ -f "${WARMSTART_DIR:-}/analysis.json" ] || return 1
  bundle_cap="$(depth_cap_from "$WARMSTART_DIR/analysis.json")"
  base_cap="$(depth_cap_from "$base_analysis")"
  if [ -n "$bundle_cap" ] && [ -n "$base_cap" ] && [ "$bundle_cap" != "$base_cap" ]; then
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

analyze_review() {
  local work="$RUNNER_TEMP/codeboarding-review"
  local base_checkout="$work/base" base_state="$work/base-state" head_state="$work/head-state"
  rm -rf "$work"
  mkdir -p "$work"

  # A published base graph is this merge base's own analysis, named for it, so it
  # needs no engine run at all. Without one, the merge base is checked out and
  # analyzed from whatever baseline the repository committed there.
  local base_source=published
  if [ -f "${BASE_DIR:-}/analysis.json" ]; then
    mkdir -p "$base_state"
    cp -a "$BASE_DIR/." "$base_state/"
  else
    base_source=computed
    fetch_commit "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA"
    git -C "$CHECKOUT_DIR" worktree add --detach "$base_checkout" "$REVIEW_BASE_SHA" >/dev/null
    seed_state "$base_checkout" "$base_state"
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
  if warmstart_usable "$base_analysis"; then
    cp -a "$WARMSTART_DIR" "$head_state"
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
  stage "$head_state" warmstart
  # Republishing a base graph that was only read back would store the same bytes
  # under the same name every run, so normally only a run that produced one
  # publishes it. The exception is lifetime: a review artifact references a base
  # by id for its whole retention, so one about to expire is renewed rather than
  # left dangling under a review that outlives it.
  local publish_base=false
  if [ "$base_source" = computed ] || [ "${RENEW_BASE:-false}" = true ]; then
    stage "$base_state" base
    publish_base=true
  fi

  printf 'analysis_mode=%s\nanalysis_path=%s\nbase_analysis_path=%s\nseed_source=%s\nchain_depth=%s\npublish_base=%s\n' \
    "$ANALYSIS_MODE" "$ANALYSIS_PATH" "$base_analysis" "$seed_source" "$chain_depth" "$publish_base" >> "$GITHUB_OUTPUT"
}

case "$ANALYSIS_KIND" in
  sync) analyze_sync ;;
  review) analyze_review ;;
  *) echo "::error::Unknown analysis kind: $ANALYSIS_KIND"; exit 1 ;;
esac
