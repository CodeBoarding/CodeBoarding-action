#!/usr/bin/env bash
# Derives the artifact names this run reads and writes.
set -euo pipefail

# Bump when the stored layout changes: no existing bundle matches afterwards, so
# runs re-derive from the base instead of restoring something they misread.
STATE_SCHEMA=v1

digest() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -c1-16
  else
    shasum -a 256 | cut -c1-16
  fi
}

# The pinned release is the source of truth. ENGINE_VERSION only overrides it
# where the package is not importable, such as tests.
engine_version="${ENGINE_VERSION:-}"
if [ -z "$engine_version" ]; then
  engine_version="$(python3 -c 'from importlib.metadata import version; print(version("codeboarding"))' 2>/dev/null || true)"
fi
if [ -z "$engine_version" ]; then
  echo "::notice::Could not resolve the installed CodeBoarding version; this run will not reuse or publish analysis state."
  exit 0
fi

# The name pins everything that decides what an analysis says, so a bundle is
# never restored into a run that would have produced something different.
ignore_digest=none
ignore_file="$CHECKOUT_DIR/.codeboarding/.codeboardingignore"
[ ! -f "$ignore_file" ] || ignore_digest="$(digest < "$ignore_file")"
model_digest="$(printf '%s\n%s\n%s\n%s\n' \
  "${LLM_PROVIDER:-}" "${MODEL:-}" "${AGENT_MODEL_INPUT:-}" "${PARSING_MODEL_INPUT:-}" | digest)"
cfg="$(printf '%s\n%s\n%s\n%s\n' \
  "$STATE_SCHEMA" "$engine_version" "$ignore_digest" "$model_digest" | digest)"

{
  echo "engine_version=$engine_version"
  echo "cfg_hash=$cfg"
} >> "$GITHUB_OUTPUT"

[ -n "${MERGE_BASE_SHA:-}" ] || exit 0
echo "base_name=codeboarding-base-$cfg-$MERGE_BASE_SHA" >> "$GITHUB_OUTPUT"

# A fork's analysis is never carried forward: untrusted code must not shape a
# pickle that a later run loads. Fork pull requests are reviewed on request and
# each review starts from the base.
[ "${IS_FORK:-false}" != true ] || exit 0
[ -n "${PR_NUMBER:-}" ] || exit 0
echo "warmstart_name=codeboarding-warmstart-$cfg-pr$PR_NUMBER" >> "$GITHUB_OUTPUT"
