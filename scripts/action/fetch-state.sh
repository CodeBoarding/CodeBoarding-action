#!/usr/bin/env bash
# Downloads the newest artifact with a given name into a directory.
# Best effort: a miss, an expired bundle or a token without actions:read all
# leave the directory absent, and the caller derives from the base instead.
set -euo pipefail
[ -n "${ARTIFACT_NAME:-}" ] || exit 0

# Clear first, on every path. These destinations are fixed, so a second use of
# the action in one job would otherwise inherit the first one's files and treat
# them as this configuration's state.
rm -rf "${DEST:?}"

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
# Paginated, because rejected entries still occupy the page: a fork that
# repeatedly uploads under this name could otherwise push the trusted bundle off
# the first page and quietly deny reuse to everyone.
trusted='[.artifacts[]?
  | select(.expired == false)
  | select(.workflow_run != null)
  | select(.workflow_run.head_repository_id == .workflow_run.repository_id)]
  | sort_by(.created_at) | last'
selected="" expires="" rejected=0 page=1
while [ "$page" -le 10 ]; do
  listing="$(api "repos/$REPOSITORY/actions/artifacts?name=$ARTIFACT_NAME&per_page=100&page=$page" 2>/dev/null || true)"
  [ -n "$listing" ] || break
  returned="$(jq -r '.artifacts | length' <<< "$listing" 2>/dev/null || echo 0)"
  [ "${returned:-0}" -gt 0 ] || break
  rejected=$(( rejected + $(jq -r '[.artifacts[]? | select(.workflow_run.head_repository_id != .workflow_run.repository_id)] | length' <<< "$listing" 2>/dev/null || echo 0) ))
  selected="$(jq -r "$trusted | .id // empty" <<< "$listing" 2>/dev/null || true)"
  if [ -n "$selected" ]; then
    expires="$(jq -r "$trusted | .expires_at // empty" <<< "$listing" 2>/dev/null || true)"
    break
  fi
  [ "$returned" -eq 100 ] || break
  page=$(( page + 1 ))
done
if [ -z "$selected" ]; then
  [ "$rejected" -eq 0 ] || echo "::warning::Ignored $rejected artifact(s) named $ARTIFACT_NAME produced by a run on code this repository does not control."
  echo "::notice::No stored $ARTIFACT_NAME to reuse; deriving from the base analysis."
  exit 0
fi

# C2/C4: a review artifact references this one by id for its whole retention, so
# say when it would outlive what it points at and the caller can republish it.
if [ -n "${RENEW_WITHIN_DAYS:-}" ] && [ -n "$expires" ]; then
  renew="$(python3 -c 'import datetime,sys
expires = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
horizon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=int(sys.argv[2]))
print("true" if expires < horizon else "false")' "$expires" "$RENEW_WITHIN_DAYS" 2>/dev/null || echo false)"
  [ -z "${GITHUB_OUTPUT:-}" ] || echo "renew=$renew" >> "$GITHUB_OUTPUT"
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
  rm -rf "${DEST:?}"
fi
rm -f "$archive"
