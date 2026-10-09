#!/usr/bin/env bash
# Finds the nearest first-parent ancestor of TIP_SHA with a saved base analysis
# under this configuration and downloads it into DEST, so a run without an exact
# base can catch up from it instead of analyzing from scratch. Best effort.
#
# Prints key=value lines on stdout and everything else on stderr:
#   ancestor_sha      the commit whose analysis is now in DEST, or empty
#   other_cfg_at_tip  true when TIP_SHA has a saved analysis under another configuration
#
# Needs the tip's first-parent history in CHECKOUT_DIR, at least CATCHUP_BOUND deep.
set -euo pipefail
: "${REPOSITORY:?}" "${CFG_HASH:?}" "${TIP_SHA:?}" "${CHECKOUT_DIR:?}" "${DEST:?}"
BOUND="${CATCHUP_BOUND:-100}"
rm -rf "$DEST"

api() { gh api -H 'Accept: application/vnd.github+json' "$@"; }
export GH_HOST="${GH_HOST:-github.com}"
GH_HOST="${GH_HOST#*://}"

ancestor="" other_cfg=false
report() {
  printf 'ancestor_sha=%s\nother_cfg_at_tip=%s\n' "$ancestor" "$other_cfg"
}
trap report EXIT

# The merge base's first-parent history, nearest first. Distance 0 is the tip
# itself, which a review already looked up by its exact name; sync has no such
# lookup and passes INCLUDE_TIP=true.
walk="$(git -C "$CHECKOUT_DIR" rev-list --first-parent --max-count=$(( BOUND + 1 )) "$TIP_SHA" 2>/dev/null || true)"
[ "${INCLUDE_TIP:-false}" = true ] || walk="$(tail -n +2 <<< "$walk")"
nearest() {
  local commit
  for commit in $walk; do
    if grep -qx "$commit" <<< "$saved"; then
      echo "$commit"
      return 0
    fi
  done
}

# The artifact listing, newest first. Its name filter is exact, so a prefix needs
# the pages themselves; paging stops at the first page holding a saved ancestor.
# Bases are published as their commits are synced or reviewed, so the nearest one
# is nearly always the newest: one or two calls in practice, MAX_PAGES (50, 5,000
# artifacts) at worst, against GITHUB_TOKEN's 1,000 requests an hour per
# repository, and only on a run that would otherwise analyze from scratch. The
# provenance rule is fetch-state.sh's: only artifacts from a run on this
# repository's own code, since a fork's workflow can upload under any name and
# its bytes would reach a pickle loader.
trusted='.artifacts[]?
  | select(.expired == false)
  | select(.workflow_run != null)
  | select(.workflow_run.head_repository_id == .workflow_run.repository_id)
  | select(.name | startswith("codeboarding-base-"))
  | .name'
prefix="codeboarding-base-$CFG_HASH-"
saved="" page=1
while [ "$page" -le "${MAX_PAGES:-50}" ]; do
  if ! listing="$(api "repos/$REPOSITORY/actions/artifacts?per_page=100&page=$page" 2>/dev/null)"; then
    echo "::warning::Could not list artifacts in $REPOSITORY; not looking for an older saved analysis." >&2
    exit 0
  fi
  names="$(jq -r "$trusted" <<< "$listing" 2>/dev/null || true)"
  saved="$saved$(grep "^$prefix" <<< "$names" | cut -c$(( ${#prefix} + 1 ))- || true)"$'\n'
  if grep "^codeboarding-base-.*-$TIP_SHA\$" <<< "$names" | grep -vq "^$prefix"; then
    other_cfg=true
  fi
  ancestor="$(nearest)"
  [ -z "$ancestor" ] || break
  returned="$(jq -r '.artifacts | length' <<< "$listing" 2>/dev/null || echo 0)"
  [ "${returned:-0}" -eq 100 ] || break
  page=$(( page + 1 ))
done

[ -n "$ancestor" ] || exit 0

# Downloaded by its exact name, through the same checks as every other lookup.
if ! GITHUB_OUTPUT="" ARTIFACT_NAME="$prefix$ancestor" DEST="$DEST" \
  "$(dirname "$0")/fetch-state.sh" >&2 || [ ! -f "$DEST/analysis.json" ]; then
  ancestor=""
fi
