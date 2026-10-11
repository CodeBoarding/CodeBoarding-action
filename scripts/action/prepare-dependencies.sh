#!/usr/bin/env bash
# Turns on Core's install of a JavaScript workspace's dependencies and points every package
# manager's download cache at one store, which the dependency cache keeps between runs.
#
# Why Core decides the rest: it checks the head and the merge-base worktree each on its own, and
# fetches the package manager and version each one pins. Never for a fork's pull request: its
# package manager would run with this job's credentials in reach.
#
# Output: lockfile=true when the head has a lockfile, so the cache steps have something to key on.
set -euo pipefail

if [ "${INSTALL_DEPENDENCIES:-true}" != true ]; then
  echo "Dependencies: not installed (install_dependencies is false)."
  exit 0
fi
if [ "${IS_FORK:-false}" = true ]; then
  echo "Dependencies: not installed (a fork's pull request)."
  exit 0
fi

mkdir -p "$STORE_DIR"
{
  echo "CODEBOARDING_INSTALL_DEPENDENCIES=1"
  echo "npm_config_cache=$STORE_DIR/npm"
  echo "npm_config_store_dir=$STORE_DIR/pnpm"
  echo "BUN_INSTALL_CACHE_DIR=$STORE_DIR/bun"
  echo "YARN_CACHE_FOLDER=$STORE_DIR/yarn"
  echo "YARN_GLOBAL_FOLDER=$STORE_DIR/yarn-berry"
} >> "$GITHUB_ENV"

for name in bun.lock bun.lockb pnpm-lock.yaml yarn.lock package-lock.json npm-shrinkwrap.json; do
  if [ -f "$CHECKOUT_DIR/$name" ]; then
    echo "lockfile=true" >> "$GITHUB_OUTPUT"
    break
  fi
done
echo "Dependencies: Core installs a JavaScript workspace's dependencies, without install scripts."
