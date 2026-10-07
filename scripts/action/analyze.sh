#!/usr/bin/env bash
# Runs incremental/full Core analysis and outputs the selected analysis paths and mode.
set -euo pipefail
DEPTH_CAP="${DEPTH_CAP:-2}"
if [[ ! "$DEPTH_CAP" =~ ^[1-9][0-9]*$ ]]; then
  echo "::error::depth_cap must be a positive integer."
  exit 1
fi
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
    --checkout "$checkout" --output-dir "$output_dir" --depth-cap "$depth")"
  parse_output "$output"
  if [ "$ANALYSIS_MODE" != full ] || [ ! -f "$ANALYSIS_PATH" ]; then
    echo "::error::Invalid full-analysis result."
    exit 1
  fi
}
# Metadata is only a compatibility check, never the source of configuration.
# Legacy baselines without a configured cap are rebuilt at the requested depth.
depth_cap_from() {
  local analysis="$1"
  [ -f "$analysis" ] || return 0
  python3 -c 'import json,sys
metadata = json.load(open(sys.argv[1])).get("metadata", {})
print(metadata.get("depth_cap", ""))' "$analysis" 2>/dev/null || true
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
  # Run logs and lock files are the engine's scratch, not analysis state. Nobody
  # inflates them, and every fetch pays for them: they were a fifth of a bundle.
  # Health config stays, because a run seeded from this bundle reads it.
  rm -rf "$STAGE_DIR/$kind/logs"
  find "$STAGE_DIR/$kind" -name '*.lock' -delete
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

  if [ "${FORCE_FULL,,}" = true ] || [ "$(depth_cap_from "$state/analysis.json")" != "$DEPTH_CAP" ]; then
    full "$CHECKOUT_DIR" "$state" "$DEPTH_CAP"
  else
    incremental "$CHECKOUT_DIR" "$state"
    if [ "$REQUIRES_FULL" = true ]; then
      full "$CHECKOUT_DIR" "$state" "$DEPTH_CAP"
    fi
  fi
  # Sync already computes the graph every review of this branch compares against,
  # so publish it instead of making the first pull request recompute it.
  stage "$state" base
  printf 'analysis_mode=%s\nanalysis_path=%s\nanalysis_dir=%s\n' \
    "$ANALYSIS_MODE" "$ANALYSIS_PATH" "$state" >> "$GITHUB_OUTPUT"
}

# How far below the merge base this run looks for the commit a saved analysis
# describes. Past it, a catch-up count is reported as unknown.
CATCHUP_BOUND=100

# A depth above 1 also deepens a commit the shallow checkout already holds.
fetch_commit() {
  local repository="$1" sha="$2" depth="${3:-1}"
  if git -C "$CHECKOUT_DIR" cat-file -e "$sha^{commit}" 2>/dev/null; then
    [ "$depth" -gt 1 ] && [ "$(git -C "$CHECKOUT_DIR" rev-parse --is-shallow-repository)" = true ] || return 0
  fi
  local auth
  auth="$(printf 'x-access-token:%s' "${GIT_TOKEN:-}" | base64 -w0)"
  git -C "$CHECKOUT_DIR" -c "http.extraheader=AUTHORIZATION: basic $auth" fetch \
    "${GITHUB_SERVER_URL%/}/${repository}.git" "$sha" --depth="$depth"
}

# The commit a baseline committed at $1 describes, or empty when that cannot be
# told. Sync writes the baseline in a commit of its own on top of the analysed
# commit, pushed straight to the branch or merged in from its pull request's
# branch. A writer sync did not make (a squash, a rebase, a hand edit) says
# nothing about which commit was analysed.
baseline_commit() {
  local writer
  writer="$(baseline_writer "$1")"
  if [ -n "$writer" ] && git -C "$CHECKOUT_DIR" rev-parse -q --verify "$writer^2" >/dev/null; then
    writer="$(baseline_writer "$writer^2")"
  fi
  [ -n "$writer" ] && is_sync_commit "$writer" || return 0
  git -C "$CHECKOUT_DIR" rev-parse -q --verify "$writer^1" || true
}
# The newest first-parent commit at or below $1 that wrote the baseline.
baseline_writer() {
  local writer shallow
  writer="$(git -C "$CHECKOUT_DIR" log --first-parent -1 --format=%H "$1" -- .codeboarding/analysis.json 2>/dev/null || true)"
  [ -n "$writer" ] || return 0
  # A shallow boundary looks like it added every file, so it proves nothing.
  shallow="$(git -C "$CHECKOUT_DIR" rev-parse --git-path shallow)"
  case "$shallow" in /*) ;; *) shallow="$CHECKOUT_DIR/$shallow" ;; esac
  if [ -f "$shallow" ] && grep -qx "$writer" "$shallow"; then
    return 0
  fi
  echo "$writer"
}
is_sync_commit() {
  case "$(git -C "$CHECKOUT_DIR" log -1 --format=%ce "$1")" in
    'codeboarding-review[bot]@users.noreply.github.com' | 'codeboarding[bot]@users.noreply.github.com') ;;
    *) return 1 ;;
  esac
  [ -z "$(code_paths_changed "$1")" ]
}
# Against the first parent, so a merge counts as the change it brought in. The
# baseline and the attributes line sync may add are not code.
code_paths_changed() {
  local exclude=(-- . ':(exclude).codeboarding' ':(exclude).gitattributes')
  if git -C "$CHECKOUT_DIR" rev-parse -q --verify "$1^1" >/dev/null; then
    git -C "$CHECKOUT_DIR" diff --name-only "$1^1" "$1" "${exclude[@]}" 2>/dev/null || true
  else
    git -C "$CHECKOUT_DIR" diff-tree --root --no-commit-id --name-only -r "$1" "${exclude[@]}" 2>/dev/null || true
  fi
}
# First-parent commits from $1 to $2 that change code: a sync commit changes
# nothing the analysis reads, so it is nothing to catch up.
catchup_count() {
  local from="$1" to="$2" commit count=0
  for commit in $(git -C "$CHECKOUT_DIR" rev-list --first-parent "$from..$to" 2>/dev/null); do
    [ -z "$(code_paths_changed "$commit")" ] || count=$(( count + 1 ))
  done
  echo "$count"
}

# Rewrites the sticky progress comment while the base is built from scratch. A
# fork's read-only token makes every call fail, which costs nothing.
PROGRESS_PID=""
PROGRESS_STOP="${RUNNER_TEMP:-}/codeboarding-progress-stop"
progress() {
  GH_TOKEN="${GIT_TOKEN:-}" GH_ENTERPRISE_TOKEN="${GIT_TOKEN:-}" BASE_REASON="$base_reason" \
    PROGRESS_STOP_FILE="$PROGRESS_STOP" "$ACTION_PATH/scripts/action/post-progress.sh" "$@" >/dev/null 2>&1 || true
}
progress_start() {
  local started="$1"
  rm -f "$PROGRESS_STOP"
  progress base 0
  ( while sleep 60 && [ ! -e "$PROGRESS_STOP" ]; do progress base "$(( $(date +%s) - started ))"; done ) >/dev/null 2>&1 &
  PROGRESS_PID=$!
}
# Stops the ticker and waits for it, so no "still running" edit can land after the
# next one. Only its sleep is killed: an edit in flight finishes or, having seen
# the stop file, never starts.
progress_stop() {
  [ -n "$PROGRESS_PID" ] || return 0
  touch "$PROGRESS_STOP"
  pkill -x sleep -P "$PROGRESS_PID" 2>/dev/null || true
  wait "$PROGRESS_PID" 2>/dev/null || true
  PROGRESS_PID=""
}

# The artifact name pins configuration; verify the stored cap and lineage too.
warmstart_usable() {
  local base_analysis="$1" bundle_cap
  [ -f "${WARMSTART_DIR:-}/analysis.json" ] || return 1
  bundle_cap="$(depth_cap_from "$WARMSTART_DIR/analysis.json")"
  if [ "$bundle_cap" != "$DEPTH_CAP" ]; then
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
  # analyzed from whatever baseline the repository committed there. Each path
  # records how the base was obtained, for the comment and the review artifact.
  local base_started base_source=saved base_reason="" base_from_sha="" catchup_commits=""
  base_started="$(date +%s)"
  if [ "$(depth_cap_from "${BASE_DIR:-}/analysis.json")" = "$DEPTH_CAP" ]; then
    mkdir -p "$base_state"
    cp -a "$BASE_DIR/." "$base_state/"
    base_from_sha="$REVIEW_BASE_SHA" catchup_commits=0
  else
    # A bundle under this exact name that the run cannot use was made with another cap.
    [ ! -f "${BASE_DIR:-}/analysis.json" ] || base_reason=incompatible
    fetch_commit "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA"
    git -C "$CHECKOUT_DIR" worktree add --detach "$base_checkout" "$REVIEW_BASE_SHA" >/dev/null
    seed_state "$base_checkout" "$base_state"
    REQUIRES_FULL=true
    if [ "$(depth_cap_from "$base_state/analysis.json")" = "$DEPTH_CAP" ]; then
      incremental "$base_checkout" "$base_state"
      if [ "$REQUIRES_FULL" = true ]; then
        base_reason=incompatible
      else
        base_source=committed
        fetch_commit "$REVIEW_BASE_REPO" "$REVIEW_BASE_SHA" "$(( CATCHUP_BOUND + 1 ))" || true
        base_from_sha="$(baseline_commit "$REVIEW_BASE_SHA")"
        [ -z "$base_from_sha" ] || catchup_commits="$(catchup_count "$base_from_sha" "$REVIEW_BASE_SHA")"
      fi
    elif [ -f "$base_state/analysis.json" ]; then
      base_reason=incompatible
    fi
    if [ "$REQUIRES_FULL" = true ]; then
      base_source=computed
      base_reason="${base_reason:-no_baseline}"
      trap progress_stop EXIT
      progress_start "$base_started"
      full "$base_checkout" "$base_state" "$DEPTH_CAP"
      progress_stop
      progress head "$(( $(date +%s) - base_started ))"
    fi
  fi
  [ "$base_source" = computed ] || base_reason=""
  # The lookup in the step before this one is part of obtaining the base too.
  local base_seconds=$(( $(date +%s) - base_started + ${BASE_FETCH_SECONDS:-0} ))
  unset GIT_TOKEN

  local base_analysis="$base_state/analysis.json"
  [ -f "$base_analysis" ] || { echo "::error::Review baseline analysis is missing."; exit 1; }

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

  local head_started
  head_started="$(date +%s)"
  incremental "$CHECKOUT_DIR" "$head_state"
  if [ "$REQUIRES_FULL" = true ]; then
    full "$CHECKOUT_DIR" "$head_state" "$DEPTH_CAP"
  fi
  local head_seconds=$(( $(date +%s) - head_started ))

  write_origin "$head_state" "$seed_source" "$chain_depth" "$(analysis_digest "$base_analysis")"
  stage "$head_state" warmstart
  # Republishing a base graph that was only read back would store the same bytes
  # under the same name every run, so normally only a run that produced one
  # publishes it. The exception is lifetime: a review artifact references a base
  # by id for its whole retention, so one about to expire is renewed rather than
  # left dangling under a review that outlives it.
  local publish_base=false
  if [ "$base_source" != saved ] || [ "${RENEW_BASE:-false}" = true ]; then
    stage "$base_state" base
    publish_base=true
  fi

  printf 'analysis_mode=%s\nanalysis_path=%s\nbase_analysis_path=%s\nseed_source=%s\nchain_depth=%s\npublish_base=%s\n' \
    "$ANALYSIS_MODE" "$ANALYSIS_PATH" "$base_analysis" "$seed_source" "$chain_depth" "$publish_base" >> "$GITHUB_OUTPUT"
  printf 'base_source=%s\nbase_reason=%s\nbase_from_sha=%s\ncatchup_commits=%s\nbase_seconds=%s\nhead_seconds=%s\n' \
    "$base_source" "$base_reason" "$base_from_sha" "$catchup_commits" "$base_seconds" "$head_seconds" >> "$GITHUB_OUTPUT"
}

case "$ANALYSIS_KIND" in
  sync) analyze_sync ;;
  review) analyze_review ;;
  *) echo "::error::Unknown analysis kind: $ANALYSIS_KIND"; exit 1 ;;
esac
