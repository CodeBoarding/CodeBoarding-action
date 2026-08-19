#!/usr/bin/env bash
# Downloads the newest artifact with a given name into a directory.
# Best effort: a miss, an expired bundle or a token without actions:read all
# leave the directory absent, and the caller derives from the base instead.
set -euo pipefail
[ -n "${ARTIFACT_NAME:-}" ] || exit 0

api() { gh api -H 'Accept: application/vnd.github+json' "$@"; }
export GH_HOST="${GH_HOST#*://}"

# Names repeat across runs, so ask for this one and take the newest that has not
# expired. This is the whole lookup: no run id, no ref, no scope.
selected="$(api "repos/$REPOSITORY/actions/artifacts?name=$ARTIFACT_NAME&per_page=100" \
  --jq '[.artifacts[] | select(.expired == false)] | sort_by(.created_at) | last | .id // empty' 2>/dev/null || true)"
if [ -z "$selected" ]; then
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
if ! unzip -qo "$archive" -d "$DEST"; then
  echo "::warning::$ARTIFACT_NAME could not be unpacked; deriving from the base analysis."
  rm -rf "$DEST"
fi
rm -f "$archive"
