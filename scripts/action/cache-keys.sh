#!/usr/bin/env bash
# Derives the analysis cache identity and outputs its restore and save keys.
set -euo pipefail

# Bump when the cached state layout or its meaning changes: every existing entry
# stops matching, so runs re-seed from the base instead of reusing stale state.
CACHE_SCHEMA=v1

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
  echo "::notice::Could not resolve the installed CodeBoarding version; skipping analysis caching."
  exit 0
fi

# The key pins everything that decides what an analysis says: the engine, the
# analysis scope, and the models that produced it. Depth needs no hash: it is
# read from the baseline at the merge base, which the key already pins.
ignore_digest=none
ignore_file="$CHECKOUT_DIR/.codeboarding/.codeboardingignore"
[ ! -f "$ignore_file" ] || ignore_digest="$(digest < "$ignore_file")"
model_digest="$(printf '%s\n%s\n%s\n%s\n' \
  "${LLM_PROVIDER:-}" "${MODEL:-}" "${AGENT_MODEL_INPUT:-}" "${PARSING_MODEL_INPUT:-}" | digest)"
cfg_hash="$(printf '%s\n%s\n%s\n%s\n' \
  "$CACHE_SCHEMA" "$engine_version" "$ignore_digest" "$model_digest" | digest)"

base_key_prefix="cb-base-$CACHE_SCHEMA-$cfg_hash-"
{
  echo "engine_version=$engine_version"
  echo "cfg_hash=$cfg_hash"
  echo "base_key_prefix=$base_key_prefix"
  echo "base_restore_keys=$base_key_prefix"
} >> "$GITHUB_OUTPUT"

[ -n "${MERGE_BASE_SHA:-}" ] || exit 0
echo "base_key=$base_key_prefix$MERGE_BASE_SHA" >> "$GITHUB_OUTPUT"

[ -n "${PR_NUMBER:-}" ] && [ -n "${HEAD_SHA:-}" ] || exit 0
# State produced while analyzing a fork stays in its own namespace: a trusted
# run must never restore, and so never unpickle, state derived from code the
# repository does not control.
if [ "${IS_FORK:-false}" = true ]; then
  chain_prefix="cb-fork-$CACHE_SCHEMA-$cfg_hash-${HEAD_REPO//\//-}-pr$PR_NUMBER-mb$MERGE_BASE_SHA-"
else
  chain_prefix="cb-head-$CACHE_SCHEMA-$cfg_hash-pr$PR_NUMBER-mb$MERGE_BASE_SHA-"
fi
chain_key="$chain_prefix$HEAD_SHA"
# Cache entries are immutable, so a run asked to discard the previous analysis
# must not save under the key holding it: the save would be dropped and the next
# run would restore exactly the state the refresh existed to replace.
[ "${SEED_MODE:-chain}" = chain ] || \
  chain_key="$chain_key-$SEED_MODE${GITHUB_RUN_ID:-0}.${GITHUB_RUN_ATTEMPT:-1}"
{
  echo "chain_key=$chain_key"
  echo "chain_restore_keys=$chain_prefix"
} >> "$GITHUB_OUTPUT"
