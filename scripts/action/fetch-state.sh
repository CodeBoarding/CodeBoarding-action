#!/usr/bin/env bash
# Downloads the newest artifact with a given name into a directory.
# Best effort: a miss, an expired bundle or a token without actions:read all
# leave the directory absent, and the caller derives from the base instead.
set -euo pipefail
[ -n "${ARTIFACT_NAME:-}" ] || exit 0

api() { gh api -H 'Accept: application/vnd.github+json' "$@"; }
export GH_HOST="${GH_HOST#*://}"

# Names repeat across runs, so ask for this one and take the newest that has not
# expired AND was produced by a run on this repository's own code.
#
# That second condition is load-bearing. These names are predictable, and a pull
# request from a fork can add a workflow that uploads an artifact under one of
# them: its run is hosted here, so the artifact lands in this repository's store.
# Loading it would hand a fork's bytes to a pickle loader inside a run holding
# this repository's credentials. A run whose head repository differs from the
# repository it ran in is exactly that case, and is never read.
listing="$(api "repos/$REPOSITORY/actions/artifacts?name=$ARTIFACT_NAME&per_page=100" 2>/dev/null || true)"
selected="$(jq -r '[.artifacts[]?
  | select(.expired == false)
  | select(.workflow_run != null)
  | select(.workflow_run.head_repository_id == .workflow_run.repository_id)]
  | sort_by(.created_at) | last | .id // empty' <<< "${listing:-{\}}" 2>/dev/null || true)"
if [ -z "$selected" ]; then
  rejected="$(jq -r '[.artifacts[]? | select(.workflow_run.head_repository_id != .workflow_run.repository_id)] | length' <<< "${listing:-{\}}" 2>/dev/null || echo 0)"
  [ "${rejected:-0}" -eq 0 ] 2>/dev/null || echo "::warning::Ignored $rejected artifact(s) named $ARTIFACT_NAME produced by a run on code this repository does not control."
  echo "::notice::No stored $ARTIFACT_NAME to reuse; deriving from the base analysis."
  exit 0
fi

archive="$RUNNER_TEMP/$ARTIFACT_NAME.zip"
if ! api "repos/$REPOSITORY/actions/artifacts/$selected/zip" > "$archive" 2>/dev/null; then
  echo "::warning::Could not download $ARTIFACT_NAME. Grant 'actions: read' to reuse previous analyses; deriving from the base for now."
  rm -f "$archive"
  exit 0
fi

mkdir -p "$DEST"
# Which one was used, not just which name was asked for: two artifacts can share
# a name and disagree, since the engine is not deterministic and a sync run
# publishes bases too.
[ -z "${GITHUB_OUTPUT:-}" ] || echo "artifact_id=$selected" >> "$GITHUB_OUTPUT"
if ! unzip -qo "$archive" -d "$DEST"; then
  echo "::warning::$ARTIFACT_NAME could not be unpacked; deriving from the base analysis."
  rm -rf "$DEST"
fi
rm -f "$archive"
