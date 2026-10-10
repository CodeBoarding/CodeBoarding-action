# Helpers for the directories that hold analysis state. Sourced, not run.

DEPTH_CAP="${DEPTH_CAP:-2}"
if [[ ! "$DEPTH_CAP" =~ ^[1-9][0-9]*$ ]]; then
  echo "::error::depth_cap must be a positive integer."
  exit 1
fi

# Metadata is only a compatibility check, never the source of configuration.
# Legacy baselines without a configured cap are rebuilt at the requested depth.
depth_cap_from() {
  local analysis="$1"
  [ -f "$analysis" ] || return 0
  python3 -c 'import json,sys
metadata = json.load(open(sys.argv[1])).get("metadata", {})
print(metadata.get("depth_cap", ""))' "$analysis" 2>/dev/null || true
}
# Whether $1 holds an analysis this run can continue from.
compatible_state() {
  [ "$(depth_cap_from "$1/analysis.json")" = "$DEPTH_CAP" ]
}

# What the user writes under .codeboarding/ belongs to the commit being analysed,
# not to whichever analysis seeded it.
USER_CONFIG=(.codeboardingignore health/health_config.json health/.healthignore)
keep_user_config() {
  local checkout="$1" state="$2" file
  mkdir -p "$state"
  for file in "${USER_CONFIG[@]}"; do
    rm -f "${state:?}/$file"
    [ ! -f "$checkout/.codeboarding/$file" ] || {
      mkdir -p "$(dirname "$state/$file")"
      cp "$checkout/.codeboarding/$file" "$state/$file"
    }
  done
}
# Replaces $2 with $1, keeping checkout $3's user configuration.
replace_state() {
  local from="$1" state="$2" config_from="$3"
  rm -rf "$state"
  mkdir -p "$(dirname "$state")"
  cp -a "$from" "$state"
  keep_user_config "$config_from" "$state"
}

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

# Records which state this analysis grew from, so a later run can tell whether
# it may continue from it.
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
