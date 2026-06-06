#!/usr/bin/env bash
#
# Local test harness for the CodeBoarding Mermaid architecture-diff action.
# Mirrors action.yml so you can iterate without waiting on a GitHub runner.
#
# Two modes:
#
#   FAST (no LLM, instant) — diff two existing analysis.json files and preview:
#     scripts/run_local.sh --base-json BASE.json --head-json HEAD.json
#
#   FULL pipeline (needs OPENROUTER_API_KEY) — run the engine on two refs of a
#   local repo, exactly like the action (committed-or-generated base, then
#   incremental head), then diff + preview:
#     export OPENROUTER_API_KEY=sk-or-...
#     scripts/run_local.sh --repo /path/to/repo --base <ref> --head <ref>
#
# Outputs (default ./.cb-local):
#   diagram.md    the ```mermaid block (what the action posts)
#   preview.html  opens in a browser and renders the colored diagram via mermaid.js
#
set -euo pipefail

ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${ENGINE:-$ACTION_DIR/../CodeBoarding}"
OUT="$ACTION_DIR/.cb-local"
DEPTH="1"
DIRECTION="LR"
CHANGED_ONLY=()
NO_EDGE_LABELS=()
RENDER_DEPTH=()
EXTRA=()
OPEN="auto"
REPO="" BASE_REF="" HEAD_REF="" BASE_JSON="" HEAD_JSON=""
# Empty by default: the engine then uses its own valid per-provider default.
# Override with a bare OpenRouter slug, e.g. AGENT_MODEL=anthropic/claude-sonnet-4
AGENT_MODEL="${AGENT_MODEL:-}"
PARSING_MODEL="${PARSING_MODEL:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --base) BASE_REF="$2"; shift 2;;
    --head) HEAD_REF="$2"; shift 2;;
    --base-json) BASE_JSON="$2"; shift 2;;
    --head-json) HEAD_JSON="$2"; shift 2;;
    --engine) ENGINE="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --depth) DEPTH="$2"; shift 2;;
    --direction) DIRECTION="$2"; shift 2;;
    --changed-only) CHANGED_ONLY=(--changed-only); shift;;
    --no-edge-labels) NO_EDGE_LABELS=(--no-edge-labels); shift;;
    --render-depth) RENDER_DEPTH=(--render-depth "$2"); shift 2;;
    --extra) read -r -a EXTRA <<< "$2"; shift 2;;   # raw args forwarded to diff_to_mermaid.py, e.g. --extra "--font-size 20 --node-padding 16"
    --no-open) OPEN="no"; shift;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

mkdir -p "$OUT"

run_engine() {
  ( cd "$ENGINE"
    export STATIC_ANALYSIS_CONFIG="$ENGINE/static_analysis_config.yml" \
           PROJECT_ROOT="$ENGINE" \
           DIAGRAM_DEPTH_LEVEL="$DEPTH" \
           CACHING_DOCUMENTATION="false" \
           ENABLE_MONITORING="false"
    # OPENROUTER_API_KEY is inherited from the environment (full mode requires it).
    # Pass the model only when set; empty -> engine's own valid per-provider default.
    if [ -n "$AGENT_MODEL" ]; then export AGENT_MODEL; fi
    if [ -n "$PARSING_MODEL" ]; then export PARSING_MODEL; fi
    uv run python "$ACTION_DIR/scripts/cb_engine.py" "$@" )
}

if [ -n "$BASE_JSON" ] && [ -n "$HEAD_JSON" ]; then
  echo "== Fast mode: diffing existing analyses (no engine run) =="
  BASE_ANALYSIS="$BASE_JSON"
  HEAD_ANALYSIS="$HEAD_JSON"
else
  if [ -z "$REPO" ] || [ -z "$BASE_REF" ] || [ -z "$HEAD_REF" ]; then
    echo "Need either --base-json/--head-json, or --repo/--base/--head." >&2; exit 2
  fi
  [ -d "$ENGINE" ] || { echo "Engine not found at $ENGINE (set --engine or \$ENGINE)." >&2; exit 2; }
  [ -n "${OPENROUTER_API_KEY:-}" ] || { echo "Export OPENROUTER_API_KEY for the full pipeline." >&2; exit 2; }
  REPO="$(cd "$REPO" && pwd)"
  BASE_DIR="$OUT/base"; HEAD_DIR="$OUT/head"
  rm -rf "$BASE_DIR" "$HEAD_DIR"; mkdir -p "$BASE_DIR" "$HEAD_DIR"

  echo "== Resolving base analysis at $BASE_REF =="
  if git -C "$REPO" show "$BASE_REF:.codeboarding/analysis.json" > "$BASE_DIR/analysis.json" 2>/dev/null; then
    echo "  using committed baseline"
  else
    rm -f "$BASE_DIR/analysis.json"
    echo "  no committed baseline; running FULL analysis on base (LLM)..."
    BASE_SRC="$OUT/base-src"
    git -C "$REPO" worktree remove --force "$BASE_SRC" 2>/dev/null || true
    git -C "$REPO" worktree prune
    rm -rf "$BASE_SRC"
    git -C "$REPO" worktree add --detach "$BASE_SRC" "$BASE_REF" >/dev/null
    run_engine base \
      --repo "$BASE_SRC" \
      --out "$BASE_DIR" \
      --name "$(basename "$REPO")" \
      --run-id local-base \
      --depth "$DEPTH" \
      --source-sha "$BASE_REF"
    git -C "$REPO" worktree remove --force "$BASE_SRC" >/dev/null 2>&1 || true
    [ -f "$BASE_DIR/analysis.json" ] || { echo "Base full analysis ran but analysis.json is missing." >&2; exit 1; }
  fi

  echo "== Analyzing head at $HEAD_REF (incremental from base) =="
  cp -a "$BASE_DIR"/. "$HEAD_DIR"/ 2>/dev/null || true
  run_engine head \
    --repo "$REPO" \
    --out "$HEAD_DIR" \
    --name "$(basename "$REPO")" \
    --run-id local-head \
    --depth "$DEPTH" \
    --base-ref "$BASE_REF" \
    --target-ref "$HEAD_REF" \
    --source-sha "$HEAD_REF"
  [ -f "$HEAD_DIR/analysis.json" ] || { echo "Head analysis ran but analysis.json is missing." >&2; exit 1; }
  BASE_ANALYSIS="$BASE_DIR/analysis.json"
  HEAD_ANALYSIS="$HEAD_DIR/analysis.json"
fi

echo "== Diff -> Mermaid =="
META="$(python3 "$ACTION_DIR/scripts/diff_to_mermaid.py" \
  --base "$BASE_ANALYSIS" --head "$HEAD_ANALYSIS" \
  --out "$OUT/diagram.md" --direction "$DIRECTION" \
  ${CHANGED_ONLY[@]+"${CHANGED_ONLY[@]}"} ${NO_EDGE_LABELS[@]+"${NO_EDGE_LABELS[@]}"} ${RENDER_DEPTH[@]+"${RENDER_DEPTH[@]}"} ${EXTRA[@]+"${EXTRA[@]}"})"
echo "  $META"

# Browser preview: render the (fence-stripped) mermaid via mermaid.js, strict mode
# to match GitHub. HTML-escape the body so labels with < > & stay valid.
python3 - "$OUT/diagram.md" "$OUT/preview.html" <<'PY'
import html, sys
src, dst = sys.argv[1], sys.argv[2]
body = open(src, encoding="utf-8").read().strip()
lines = body.splitlines()
if lines and lines[0].startswith("```"): lines = lines[1:]
if lines and lines[-1].startswith("```"): lines = lines[:-1]
graph = html.escape("\n".join(lines))
open(dst, "w", encoding="utf-8").write(f"""<!doctype html><html><head><meta charset="utf-8">
<title>CodeBoarding architecture diff</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;margin:2rem;color:#1f2328}}
.legend span{{margin-right:1.25rem;font-weight:600}}
.mermaid{{margin-top:1rem}}</style></head><body>
<h2>Architecture diff preview</h2>
<div class="legend">
  <span style="color:#1f883d">&#9632; added</span>
  <span style="color:#bf8700">&#9632; modified</span>
  <span style="color:#cf222e">&#9632; deleted</span>
</div>
<pre class="mermaid">
{graph}
</pre>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
mermaid.initialize({{ startOnLoad: true, securityLevel: 'strict' }});
</script></body></html>""")
print(f"  wrote {dst}")
PY

echo
echo "diagram : $OUT/diagram.md"
echo "preview : $OUT/preview.html"
if [ "$OPEN" != "no" ]; then
  if command -v open >/dev/null 2>&1; then open "$OUT/preview.html";
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$OUT/preview.html";
  else echo "(open $OUT/preview.html in your browser)"; fi
fi
