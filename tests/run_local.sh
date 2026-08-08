#!/usr/bin/env bash
# Local test harness for the CodeBoarding Action.
#
# Three modes:
#   1) FAST review preview (no repo analysis):
#      tests/run_local.sh --base-json BASE.json --head-json HEAD.json
#   2) REVIEW LOCAL (full local pipeline):
#      tests/run_local.sh --repo /path/to/repo --base <base-ref> --head <head-ref>
#   3) REVIEW LOCAL against committed baseline only (if available):
#      tests/run_local.sh --repo /path/to/repo --base <base-ref> --head <head-ref> --depth 2
#
# Output:
#   diagram.md   Mermaid payload posted by the action
#   preview.html browser preview

set -euo pipefail

ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ACTION_DIR}/.cb-local"
DEPTH="1"
DIRECTION="LR"
OPEN="auto"
REPO="" BASE_REF="" HEAD_REF=""
BASE_JSON="" HEAD_JSON=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --base) BASE_REF="$2"; shift 2;;
    --head) HEAD_REF="$2"; shift 2;;
    --base-json) BASE_JSON="$2"; shift 2;;
    --head-json) HEAD_JSON="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --depth) DEPTH="$2"; shift 2;;
    --direction) DIRECTION="$2"; shift 2;;
    --no-open) OPEN="no"; shift;;
    -h|--help)
      sed -n '1,80p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$OUT"

parse_value() {
  local key="$1"
  local text="$2"
  printf '%s\n' "$text" | awk -F= -v key="$key" '$1 == key {print $2; exit}'
}

run_inc() {
  local checkout="$1"
  local out_dir="$2"

  python3 "$ACTION_DIR/scripts/analyze_repository.py" incremental \
    --checkout "$checkout" \
    --output-dir "$out_dir"
}

run_full() {
  local checkout="$1"
  local out_dir="$2"

  python3 "$ACTION_DIR/scripts/analyze_repository.py" full \
    --checkout "$checkout" \
    --output-dir "$out_dir" \
    --depth-level "$DEPTH"
}

if [ -n "$BASE_JSON" ] && [ -n "$HEAD_JSON" ]; then
  BASE_ANALYSIS="$BASE_JSON"
  HEAD_ANALYSIS="$HEAD_JSON"
else
  if [ -z "$REPO" ] || [ -z "$BASE_REF" ] || [ -z "$HEAD_REF" ]; then
    echo "Need either --base-json/--head-json, or --repo/--base/--head." >&2
    exit 2
  fi

  if [[ ! "$DEPTH" =~ ^[0-9]+$ ]]; then
    echo "depth must be an integer." >&2
    exit 2
  fi

  WORK="$OUT/work"
  rm -rf "$WORK"
  BASE_DIR="$WORK/base"
  HEAD_DIR="$WORK/head"
  BASE_STATE="$WORK/base-state"
  HEAD_STATE="$WORK/head-state"
  mkdir -p "$BASE_DIR" "$HEAD_DIR" "$BASE_STATE" "$HEAD_STATE"

  REPO="$(cd "$REPO" && pwd)"

  git -C "$REPO" rev-parse -q --verify "$BASE_REF^{commit}" >/dev/null 2>&1 || git -C "$REPO" fetch origin "$BASE_REF" --depth=1
  git -C "$REPO" rev-parse -q --verify "$HEAD_REF^{commit}" >/dev/null 2>&1 || git -C "$REPO" fetch origin "$HEAD_REF" --depth=1
  BASE_SHA="$(git -C "$REPO" rev-parse "$BASE_REF^{commit}")"
  HEAD_SHA="$(git -C "$REPO" rev-parse "$HEAD_REF^{commit}")"

  git -C "$REPO" worktree add --detach "$BASE_DIR" "$BASE_SHA" >/dev/null
  git -C "$REPO" worktree add --detach "$HEAD_DIR" "$HEAD_SHA" >/dev/null

  cleanup() {
    git -C "$REPO" worktree remove --force "$BASE_DIR" >/dev/null 2>&1 || true
    git -C "$REPO" worktree remove --force "$HEAD_DIR" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  }
  trap cleanup EXIT

  BASELINE_DEPTH=""
  if [ -f "$BASE_DIR/.codeboarding/analysis.json" ]; then
    BASELINE_DEPTH="$(python3 -c 'import json, sys; data = json.load(open(sys.argv[1])); print(data.get("metadata", {}).get("depth_level", ""))' "$BASE_DIR/.codeboarding/analysis.json" 2>/dev/null || true)"
  fi

  if [ -n "$BASELINE_DEPTH" ] && [[ "$BASELINE_DEPTH" =~ ^[0-9]+$ ]]; then
    DEPTH="$BASELINE_DEPTH"
  fi

  if [ -f "$BASE_DIR/.codeboarding/analysis.json" ]; then
    cp -a "$BASE_DIR/.codeboarding/." "$BASE_STATE/"
    BASE_OUTPUT="$(run_inc "$BASE_DIR" "$BASE_STATE")"
    BASE_MODE="$(parse_value analysis_mode "$BASE_OUTPUT")"
    BASE_NEED_FULL="$(parse_value requires_full_analysis "$BASE_OUTPUT")"
    BASE_PATH="$(parse_value analysis_path "$BASE_OUTPUT")"
    if [ "$BASE_MODE" != incremental ] || { [ "$BASE_NEED_FULL" != true ] && [ -z "$BASE_PATH" ]; }; then
      echo "::error::Could not parse base incremental output contract." >&2
      exit 1
    fi
  else
    BASE_NEED_FULL=true
  fi
  if [ "$BASE_NEED_FULL" = true ]; then
    BASE_OUTPUT="$(run_full "$BASE_DIR" "$BASE_STATE")"
    BASE_MODE="$(parse_value analysis_mode "$BASE_OUTPUT")"
    BASE_PATH="$(parse_value analysis_path "$BASE_OUTPUT")"
    if [ "$BASE_MODE" != full ] || [ -z "$BASE_PATH" ]; then
      echo "::error::Base full analysis did not produce analysis_path." >&2
      exit 1
    fi
  fi
  [ -f "$BASE_PATH" ] || { echo "::error::Missing generated base analysis.json." >&2; exit 1; }
  BASE_FOR_DIFF="$BASE_PATH"
  cp -a "$BASE_STATE/." "$HEAD_STATE/"

  HEAD_OUTPUT="$(run_inc "$HEAD_DIR" "$HEAD_STATE")"
  HEAD_MODE="$(parse_value analysis_mode "$HEAD_OUTPUT")"
  NEED_FULL="$(parse_value requires_full_analysis "$HEAD_OUTPUT")"
  HEAD_PATH="$(parse_value analysis_path "$HEAD_OUTPUT")"

  if [ "$HEAD_MODE" != incremental ] || { [ "$NEED_FULL" != true ] && [ -z "$HEAD_PATH" ]; }; then
    echo "::error::Could not parse head incremental output contract." >&2
    echo "$HEAD_OUTPUT"
    exit 1
  fi

  if [ "$NEED_FULL" = "true" ]; then
    HEAD_FULL_OUTPUT="$(run_full "$HEAD_DIR" "$HEAD_STATE")"
    HEAD_MODE="$(parse_value analysis_mode "$HEAD_FULL_OUTPUT")"
    HEAD_PATH="$(parse_value analysis_path "$HEAD_FULL_OUTPUT")"
    if [ "$HEAD_MODE" != "full" ] || [ -z "$HEAD_PATH" ]; then
      echo "::error::Could not parse head full output contract." >&2
      echo "$HEAD_FULL_OUTPUT"
      exit 1
    fi
  fi

  if [ ! -f "$HEAD_PATH" ]; then
    echo "::error::Missing generated head analysis.json." >&2
    exit 1
  fi

  BASE_ANALYSIS="$BASE_FOR_DIFF"
  HEAD_ANALYSIS="$HEAD_PATH"
fi

if [ -z "$BASE_ANALYSIS" ] || [ ! -f "$BASE_ANALYSIS" ]; then
  echo "::error::Missing review base analysis.json." >&2
  exit 1
fi
if [ -z "$HEAD_ANALYSIS" ] || [ ! -f "$HEAD_ANALYSIS" ]; then
  echo "::error::Missing review head analysis.json." >&2
  exit 1
fi

DIAGRAM_OUT="$OUT/diagram.md"
python3 "$ACTION_DIR/scripts/diff_to_mermaid.py" \
  --base "$BASE_ANALYSIS" \
  --head "$HEAD_ANALYSIS" \
  --out "$DIAGRAM_OUT" \
  --direction "$DIRECTION" \
  --render-depth 1

python3 - "$DIAGRAM_OUT" "$OUT/preview.html" <<'PY'
import html, sys
src, dst = sys.argv[1], sys.argv[2]
body = open(src, encoding="utf-8").read().strip()
lines = body.splitlines()
if lines and lines[0].startswith("```"):
  lines = lines[1:]
if lines and lines[-1].startswith("```"):
  lines = lines[:-1]
graph = html.escape("\n".join(lines))
open(dst, "w", encoding="utf-8").write(f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>CodeBoarding architecture diff</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;margin:2rem;color:#1f2328}}
.legend span{{margin-right:1.25rem;font-weight:600}}
.mermaid{{margin-top:1rem}}</style></head><body>
<h2>Architecture diff preview</h2>
<div class=\"legend\">
  <span style=\"color:#1f883d\">■ added</span>
  <span style=\"color:#bf8700\">■ modified</span>
  <span style=\"color:#cf222e\">■ deleted</span>
</div>
<pre class=\"mermaid\">\n{graph}\n</pre>
<script type=\"module\">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
mermaid.initialize({{ startOnLoad: true, securityLevel: 'strict' }});
</script></body></html>""")
print(f"  wrote {dst}")
PY

echo

echo "diagram : $DIAGRAM_OUT"
echo "preview : $OUT/preview.html"
if [ "$OPEN" != "no" ]; then
  if command -v open >/dev/null 2>&1; then
    open "$OUT/preview.html"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$OUT/preview.html"
  else
    echo "(open $OUT/preview.html in your browser)"
  fi
fi
