#!/usr/bin/env bash
# Copies Core's persisted artifacts into .codeboarding.
set -euo pipefail
output="$CHECKOUT_DIR/.codeboarding"
mkdir -p "$output"
# Core exposes one canonical manifest. Read from the action's directory, never the
# analyzed repository's, which may have a constants.py of its own.
manifest="$(cd "$ACTION_PATH" && python3 - <<'PY'
from constants import ANALYSIS_FILENAME, PERSISTED_ANALYSIS_ARTIFACT_FILENAMES as artifacts
print(ANALYSIS_FILENAME)
print(*artifacts, sep="\n")
PY
)" || { echo "::error::Could not read Core's persisted artifact manifest."; exit 1; }
[ -n "$manifest" ] || { echo "::error::Core's persisted artifact manifest is empty."; exit 1; }
# Read into an array without mapfile, which needs bash 4: macOS ships bash 3.2,
# so mapfile made this script, and its tests, unrunnable for local development.
manifest_entries=()
while IFS= read -r entry; do
  manifest_entries+=("$entry")
done <<< "$manifest"
required="${manifest_entries[0]}"
artifacts=("${manifest_entries[@]:1}")
[ -f "$ANALYSIS_DIR/$required" ] || { echo "::error::Core did not produce $required."; exit 1; }

installed=0
for name in "${artifacts[@]}"; do
  source="$ANALYSIS_DIR/$name"
  target="$output/$name"
  if [ -f "$source" ]; then
    cp "$source" "$target"
    installed=$((installed + 1))
  elif [ -e "$target" ]; then
    rm -f "$target"
  fi
  printf '%s\n' "$target"
done

# The engine writes a health report on every run, full or incremental. Install it
# beside the analysis so the extension and webview can read warnings without
# regenerating, and remove a stale one when a run produced none.
health_source="$ANALYSIS_DIR/health/health_report.json"
health_target="$output/health/health_report.json"
if [ -f "$health_source" ]; then
  mkdir -p "$output/health"
  cp "$health_source" "$health_target"
elif [ -e "$health_target" ]; then
  rm -f "$health_target"
fi
printf '%s\n' "$health_target"
echo "installed=$installed" >> "$GITHUB_OUTPUT"
