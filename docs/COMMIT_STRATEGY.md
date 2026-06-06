# Baseline & artifact commit strategy

What CodeBoarding writes into a repo, what we commit vs. cache, where, and how
today's choice keeps a future hosted-webview viewer possible without rework.

## The artifacts

The engine writes these under `.codeboarding/`:

| File | Type | Size | Purpose |
|---|---|---|---|
| `analysis.json` | JSON (text) | KB–low MB | The component graph — **the diagram source** |
| `health/health_report.json` | JSON (text) | KB | Health findings → **the warnings** |
| `static_analysis.pkl` | binary pickle | MB-scale | LSP/CFG cache → **warm-start** (re-LSP only changed files) |
| `static_analysis.sha` | text (1 line) | bytes | Tag recording the pkl's commit → the warm-start gate |

## Decision

**Commit (text, small, display-critical):**
- ✅ `analysis.json` — required for the extension (and later the webview) to **show the diagram instantly without regenerating** — i.e. without spending the user's API key. It's text and diffs meaningfully.
- ✅ `health/health_report.json` — required for warnings in the extension/webview. Small text.

**Do NOT commit (binary, bloat):**
- ❌ `static_analysis.pkl` — binary, MB-scale, noisy diffs, repo bloat. It is a *rebuildable speed cache*, not display data. Keep it in **`actions/cache` keyed by the base SHA** (or a backend). A cache miss just falls back to a cold (full) LSP pass — slower but correct, and the committed `analysis.json` still drives the diagram.
- `static_analysis.sha` — commit **only** if the pkl is kept reachable (cache/backend); on its own it's harmless but unused.

> **Principle:** version-control the *source-of-truth display data* (text, small); *cache* the *rebuildable speed artifacts* (binary, large). This is exactly what keeps the repo clean — the thing that bloats (`.pkl`) never enters git.

## Where to commit — two separate workflows

1. **CI/CD on `main` (the baseline keeper).** On push to `main`, regenerate and commit `analysis.json` + `health/health_report.json` to `main`. Keeps the baseline current so PRs diff against an accurate, up-to-date snapshot and the extension shows a real diagram on the default branch.

2. **The review action (PR).** **Comment-only by default** — no commits to contributors' branches (no churn, and it still works on fork PRs where the token is read-only). The PR comment leads users to the extension.
   - *Optional later:* commit the head `analysis.json` to the PR branch so opening the extension on that PR shows the exact head diagram. Deferred — it pushes a bot commit to the contributor's branch and can't run on fork PRs.

## Now vs. later

- **Now — extension-direct.** Committing `analysis.json` + `health_report.json` on `main` means a user who installs the extension and opens the repo sees the committed diagram + warnings **instantly, with no API key**. The PR comment's CTA points straight at the extension (install / open in editor).
- **Later — hosted webview.** The webview needs the **same** committed `analysis.json` (+ a diff + health). So committing now is **forward-compatible**: when the viewer is built, the data already exists at each commit — no migration, just a host layer that reads it.

## Warm-start tradeoff (the `.pkl`)

The warm-start needs the pkl **and** its `.sha`. When the review action has to generate a base analysis, it saves that generated base artifact directory in `actions/cache` keyed by base SHA / depth / engine ref, then seeds the head analysis from that directory. When a committed `analysis.json` already exists but no matching cache exists, the PR still diffs correctly but may run a cold LSP pass. This keeps the repo clean; the cache improves speed but is not required for correctness.

## Summary

| Artifact | Commit? | Where | Why |
|---|---|---|---|
| `analysis.json` | ✅ | `main` (CI/CD); PR branch optional/later | diagram source; powers extension now + webview later |
| `health_report.json` | ✅ | with `analysis.json` | warnings |
| `static_analysis.pkl` | ❌ | `actions/cache` (or backend), key = base SHA | binary speed cache; never bloat git |
| `static_analysis.sha` | ⚠️ optional | with the cached pkl | warm-start gate; useless without the pkl |
