# The analysis branch (codeboarding_analysis_branch, BASELINE_BRANCH here): an orphan branch holding
# one commit per sync, each with trailers naming the commit it analysed
# (CodeBoarding-Source) and the configuration it ran under (CodeBoarding-Config).
# Sync writes it; sync and review read it. Sourced, not run; needs analysis-state.sh.

# How far below a commit a reader looks for an entry describing it or an ancestor.
CATCHUP_BOUND="${CATCHUP_BOUND:-100}"
BASELINE_BRANCH_DEPTH="${BASELINE_BRANCH_DEPTH:-100}"

# ---- Reading ----

# Lists the branch's entries as "<branch commit> <source sha> <config>", newest
# first; an entry without a config has none to compare. Fetched without blobs
# into a scratch repository: the lookup needs messages, and a hundred pickles
# would cost more than it saves.
codeboarding_baseline_index() {
  local repository="$1" scratch="$RUNNER_TEMP/codeboarding-baseline-index.git" auth
  rm -rf "$scratch"
  git init -q --bare "$scratch"
  auth="$(printf 'x-access-token:%s' "${GIT_TOKEN:-}" | base64 -w0)"
  git -C "$scratch" -c "http.extraheader=AUTHORIZATION: basic $auth" fetch -q --filter=blob:none \
    --depth="$BASELINE_BRANCH_DEPTH" "${GITHUB_SERVER_URL%/}/${repository}.git" "refs/heads/$BASELINE_BRANCH" || return 1
  git -C "$scratch" log --format='%H %(trailers:key=CodeBoarding-Source,valueonly,separator=%x20) %(trailers:key=CodeBoarding-Config,valueonly,separator=%x20)' FETCH_HEAD |
    awk 'NF >= 2 {print $1, $2, (NF >= 3 ? $3 : "-")}'
}

# Seeds $3 from the branch's entry for $2, or for its nearest first-parent
# ancestor, made under this run's configuration: the trailer pins it the way the
# name pins a saved artifact, and without a configuration hash no entry is
# trusted. Keeps checkout $4's user configuration. Sets SEED_SHA to the commit
# the seed describes.
from_codeboarding_baseline() {
  local repository="$1" tip="$2" state="$3" config_from="$4" index commit entry="" scratch="$RUNNER_TEMP/codeboarding-baseline-restore"
  local auth status=0
  [ -n "${BASELINE_BRANCH:-}" ] || return 1
  [ -n "${CFG_HASH:-}" ] || return 1
  # A branch that does not exist yet is a miss: the first sync creates it. Any other
  # failure to read it is an error, not a reason to quietly analyze from scratch.
  auth="$(printf 'x-access-token:%s' "${GIT_TOKEN:-}" | base64 -w0)"
  git -c "http.extraheader=AUTHORIZATION: basic $auth" ls-remote --exit-code \
    "${GITHUB_SERVER_URL%/}/${repository}.git" "refs/heads/$BASELINE_BRANCH" >/dev/null || status=$?
  case "$status" in
    0) ;;
    2) echo "::notice::$BASELINE_BRANCH does not exist yet; the first sync creates it."; return 1 ;;
    *) echo "::error::Could not read $BASELINE_BRANCH from $repository. Check that github_token can read the repository."; exit 1 ;;
  esac
  index="$(codeboarding_baseline_index "$repository")" ||
    { echo "::error::Could not fetch $BASELINE_BRANCH from $repository."; exit 1; }
  if [ -z "$index" ]; then
    # A code branch, as when a review into another branch names the one that holds the analysis.
    echo "::warning::$BASELINE_BRANCH is not a CodeBoarding analysis branch, so its analysis is not read here."
    return 1
  fi
  fetch_commit "$repository" "$tip" "$(( CATCHUP_BOUND + 1 ))" ||
    { echo "::error::Could not fetch the history of ${tip:0:7} to look for its analysis."; exit 1; }
  for commit in $(git -C "$CHECKOUT_DIR" rev-list --first-parent --max-count=$(( CATCHUP_BOUND + 1 )) "$tip" 2>/dev/null); do
    entry="$(awk -v source="$commit" -v cfg="$CFG_HASH" '$2 == source && $3 == cfg {print $1; exit}' <<< "$index")"
    [ -z "$entry" ] || break
  done
  if [ -z "$entry" ]; then
    echo "::notice::$BASELINE_BRANCH has no analysis of ${tip:0:7} or its last $CATCHUP_BOUND ancestors made with this configuration."
    return 1
  fi
  # The entry is known to exist from here on, so failing to read it is an error.
  fetch_commit "$repository" "$entry" ||
    { echo "::error::Could not fetch the analysis ${entry:0:7} from $BASELINE_BRANCH."; exit 1; }
  rm -rf "$scratch"
  mkdir -p "$scratch"
  git -C "$CHECKOUT_DIR" archive "$entry" .codeboarding | tar -x -C "$scratch" ||
    { echo "::error::Could not extract the analysis ${entry:0:7} from $BASELINE_BRANCH."; exit 1; }
  # source.json is provenance, not engine state.
  rm -f "$scratch/.codeboarding/source.json"
  compatible_state "$scratch/.codeboarding" || return 1
  replace_state "$scratch/.codeboarding" "$state" "$config_from"
  SEED_SHA="$commit"
  echo "::notice::Starting from the analysis of ${commit:0:7} saved on $BASELINE_BRANCH."
}

# ---- Writing ----

# Saves the analysis installed by install-sync.sh as one fast-forward commit on
# BASELINE_BRANCH, never writing to the synced branch. Called by deliver-sync.sh,
# whose REMOTE, BASE_SHA and emit_result it uses; always exits.
save_to_codeboarding_baseline() {
  local branch="$BASELINE_BRANCH" tree="$RUNNER_TEMP/codeboarding-baseline-tree"
  local index="$RUNNER_TEMP/codeboarding-baseline-index" git_dir files new_tree tip parent commit now
  git_dir="$(git rev-parse --absolute-git-dir)"
  rm -rf "$tree" "$index"
  mkdir -p "$tree"
  CHECKOUT_DIR="$tree" "$ACTION_PATH/scripts/action/install-sync.sh" > /dev/null
  files="$(find "$tree/.codeboarding" -maxdepth 1 -type f | wc -l | tr -d ' ')"
  # Engine output is never edited; which commit it describes, and under which
  # configuration, lives here and in the commit's trailers only.
  python3 -c 'import datetime,json,os,sys
json.dump({
    "schema": 1,
    "synced_branch": os.environ["SYNCED_BRANCH"],
    "source_sha": sys.argv[2],
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "engine_version": os.environ.get("ENGINE_VERSION", ""),
    "config": os.environ.get("CFG_HASH", ""),
}, open(sys.argv[1], "w"), indent=2)' "$tree/.codeboarding/source.json" "$BASE_SHA"
  GIT_INDEX_FILE="$index" git --git-dir="$git_dir" --work-tree="$tree" -C "$tree" add -A -f .codeboarding
  new_tree="$(GIT_INDEX_FILE="$index" git --git-dir="$git_dir" write-tree)"
  git config user.name 'codeboarding-review[bot]'
  git config user.email 'codeboarding-review[bot]@users.noreply.github.com'
  # Readers trust only entries made under their own configuration.
  [ -n "${CFG_HASH:-}" ] || { echo "::error::No configuration hash to record with the analysis."; exit 1; }
  local trailers=(-m "CodeBoarding-Source: $BASE_SHA
CodeBoarding-Config: $CFG_HASH")

  # Two tries: a concurrent sync that moved the branch for an older commit is
  # built on top of once. A second move means a newer run is handling it.
  for _ in 1 2; do
    git fetch -q "$REMOTE" "$SYNCED_BRANCH"
    if [ "$(git rev-parse FETCH_HEAD)" != "$BASE_SHA" ]; then
      emit_result "$files" false "$BASE_SHA"
      echo "::notice::$SYNCED_BRANCH advanced during analysis; a newer run should update $branch."
      exit 0
    fi
    tip="" parent=()
    if [ -n "$(git ls-remote "$REMOTE" "refs/heads/$branch")" ]; then
      git fetch -q --depth=1 "$REMOTE" "refs/heads/$branch"
      # The parent is what was fetched, not what ls-remote saw: the branch may move in between.
      tip="$(git rev-parse FETCH_HEAD)"
      # Building on any other branch would leave it holding nothing but .codeboarding/.
      if [ -z "$(git log -1 --format='%(trailers:key=CodeBoarding-Source,valueonly)' "$tip" | tr -d '[:space:]')" ] ||
        [ "$(git ls-tree --name-only "$tip")" != .codeboarding ]; then
        echo "::error::$branch already exists and is not a CodeBoarding baseline branch, so sync will not write to it. Set codeboarding_analysis_branch to a branch name that is not in use, or to $SYNCED_BRANCH to commit the analysis there."
        exit 1
      fi
      parent=(-p "$tip")
      if [ "$(git log -1 --format='%(trailers:key=CodeBoarding-Source,valueonly)' "$tip" | tr -d '[:space:]')" = "$BASE_SHA" ] &&
        git diff --quiet -I '"generated_at"' -I '"timestamp"' "$tip" "$new_tree"; then
        emit_result "$files" false "$BASE_SHA"
        echo "::notice::$branch already holds this analysis of $SYNCED_BRANCH @${BASE_SHA:0:7}."
        exit 0
      fi
    fi
    commit="$(git commit-tree "$new_tree" ${parent[@]+"${parent[@]}"} \
      -m "chore(codeboarding): diagram of $SYNCED_BRANCH @${BASE_SHA:0:7}" "${trailers[@]}")"
    # Never forced: the parent is the tip just read, so this only ever fast-forwards.
    if git push -q "$REMOTE" "$commit:refs/heads/$branch"; then
      emit_result "$files" true "$BASE_SHA"
      exit 0
    fi
    now="$(git ls-remote "$REMOTE" "refs/heads/$branch" | awk '{print $1; exit}')"
    if [ "$now" = "$tip" ]; then
      echo "::error::GitHub refused the push to $branch, most likely because a branch rule protects it. Add the identity sync pushes with (the CodeBoarding app, or GitHub Actions for the default token) as a bypass actor for $branch in the repository's rulesets, and check that github_token may push to it."
      exit 1
    fi
  done
  emit_result "$files" false "$BASE_SHA"
  echo "::notice::Another sync keeps updating $branch; leaving it to that run."
  exit 0
}
